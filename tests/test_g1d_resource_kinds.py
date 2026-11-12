"""G1d：资源种类（network / mcp / subtask）与外发许可门。

判据来自 PRD §5.2.3：

> 资源使用带种类的描述：文件、网络目标、MCP 工具、记忆作用域、子任务各自规范化
> 与校验。无 workspace 的研究任务合法，URL 不伪装成文件路径；多个目录必须明确
> 目标绑定。

> 状态变化、数据外发、计费、可重试性分开表达。网络 GET / 搜索不是无成本、无外发
> 的本地纯读。

> 私有内容未经外发许可不发包。

分成三组，分别对应三个**不同的**问题：

1. **规范化**：同一个目标只能有一种写法。否则"批准了 A、实际发到 B"这类凭据
   混淆就有落脚点。
2. **许可**：外发恒需人工批准（没有自动路径）、MCP 与 exec 同级、委派能力缺失
   必须 fail-closed、种类混用一律拒。
3. **执行侧**：没有执行器的种类在派发前拒绝，绝不回退成"当成文件路径试试"。
"""

from __future__ import annotations

import dataclasses

import pytest

from tianshu.dispatcher import (
    EXECUTABLE_KINDS,
    DispatchDenied,
    resolve_scheme_resource,
)
from tianshu.events import EventLog
from tianshu.permissions import (
    ACTION_DELEGATE,
    ACTION_EXEC,
    ACTION_FETCH,
    ACTION_READ,
    APPROVAL_ACTIONS,
    AUTO_ACTIONS,
    DECISION_APPROVE,
    DECISION_AUTO,
    SUPPORTED_ACTIONS,
    ActionRequest,
    ApprovalStore,
    PolicyEngine,
)
from tianshu.resources import (
    KIND_FILE,
    KIND_MCP,
    KIND_MEMORY,
    KIND_NETWORK,
    KIND_SUBTASK,
    RESOURCE_KINDS,
    ResourceError,
    ResourceRef,
    parse_resource,
)
from tianshu.tools import SPECS
from tianshu.workspace import WorkspaceBinding

POLICY = PolicyEngine()


def _bindings(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return [WorkspaceBinding(str(ws))]


# ---------------------------------------------------------------- 0 封闭集


def test_resource_kinds_are_a_closed_set():
    assert RESOURCE_KINDS == {
        KIND_FILE,
        KIND_MEMORY,
        KIND_NETWORK,
        KIND_MCP,
        KIND_SUBTASK,
    }
    # 执行器：file / memory / **network** / **subtask**。网络是 G2 网络阶段
    # 补的；委派是 G2 子代理 D1 补的（D1 只排队，不执行子 Run）。MCP 仍然没有
    # 执行器，仍在派发前拒绝。这点在执行侧也会被核（防两边各说一套）。
    assert EXECUTABLE_KINDS == {KIND_FILE, KIND_MEMORY, KIND_NETWORK, KIND_SUBTASK}
    # 动作集合也扩了，但**没有**把外发/委派放进动作级自动放行：
    # - `fetch` 恒需审批；
    # - `delegate` 的形状按工具判（spawn 审批 / cancel 自动），见
    #   `tests/test_g2_delegation_contract.py` 与 PolicyEngine._classify_subtask。
    assert {ACTION_FETCH, ACTION_DELEGATE} <= SUPPORTED_ACTIONS
    assert ACTION_FETCH in APPROVAL_ACTIONS
    assert ACTION_FETCH not in AUTO_ACTIONS
    assert ACTION_DELEGATE not in AUTO_ACTIONS
    assert ACTION_DELEGATE not in APPROVAL_ACTIONS, (
        "delegate 不是动作级一律审批：cancel 自动放行；动作级会逼出假审批"
    )


def test_a_plain_url_is_still_not_a_resource():
    """`https://...` 不能被当成网络资源：本项目的网络资源有自己的 scheme。

    G1a 已经拒绝"URL 伪装成文件路径"。这条证明它**也没有**被顺手放宽成网络资源：
    放宽就等于让 `parse_resource` 接受两种写法指向同一个目标。
    """
    for raw in ("https://example.com/a", "http://example.com/a", "file:///etc/passwd"):
        with pytest.raises(ResourceError):
            parse_resource(raw)


# ---------------------------------------------------------------- 1 规范化


def test_network_target_is_normalized_to_one_form():
    # 大小写、默认端口、`.`/`..`、query 位置：四种写法折叠成同一个 URI
    same = [
        "network://https/example.com/a/b",
        "network://https/ExAmPle.COM:443/a/b",
        "network://https/example.com/a/./b",
        "network://https/example.com/x/../a/b",
        "network://https/Example.com/a/c/../b",
    ]
    uris = {parse_resource(raw).uri for raw in same}
    assert uris == {"network://https/example.com/a/b"}, uris

    target = parse_resource("network://https/example.com/a/b?q=1").network_target
    assert target.host == "example.com"
    assert target.port is None, "默认端口必须省略，否则同一目标有两种摘要"
    assert target.origin == "https://example.com"
    assert target.path == "/a/b"
    assert target.query == "q=1"

    # 非默认端口保留；http 的 80 同样省略
    assert parse_resource("network://https/h:8443/x").uri == "network://https/h:8443/x"
    assert parse_resource("network://http/h:80/x").uri == "network://http/h/x"


def test_network_target_idna_and_path_decoding():
    # 主机做 IDNA，路径做百分号解码：两种写法必须落到同一个 URI
    assert parse_resource("network://https/héllo/x").uri == "network://https/xn--hllo-bpa/x"
    assert parse_resource("network://https/h/%E4%B8%AD").uri == parse_resource(
        "network://https/h/中"
    ).uri


def test_network_target_rejects_confusing_forms():
    cases = {
        "network://https/user:pass@h/x": "userinfo",
        "network://https/h/x#frag": "fragment",
        "network://ftp/h/x": "传输方式",
        "network://https/h/../../x": "逃出根",
        "network://https/h/%2e%2e/x": "逃出根",
        "network://https/h/a%2Fb": "非法字符",  # 解码后是分隔符
        "network://https/h/a%00b": "非法字符",
        "network://https/%65xample.com/x": "百分号编码",
        "network://https//x": "主机",
        "network://https/h:abc/x": "端口",
        "network://https/h:70000/x": "端口",
    }
    for raw, needle in cases.items():
        with pytest.raises(ResourceError) as excinfo:
            parse_resource(raw)
        assert needle in str(excinfo.value), (raw, str(excinfo.value))


def test_mcp_and_subtask_shapes():
    assert parse_resource("mcp://files/read_file").mcp_target.server == "files"
    assert parse_resource("mcp://files/read_file").uri == "mcp://files/read_file"
    for raw in ("mcp://files", "mcp://files/", "mcp:///tool", "mcp://Fi/les/x",
                "mcp://files/读文件"):
        with pytest.raises(ResourceError):
            parse_resource(raw)

    assert parse_resource("subtask://task-1").subtask_id == "task-1"
    for raw in ("subtask://", "subtask://a/b", "subtask://有中文"):
        with pytest.raises(ResourceError):
            parse_resource(raw)


def test_resource_ref_only_accepts_canonical_values():
    """写进 `resources` 的值会进审批摘要：非规范形状等于同一目标两种摘要。"""
    with pytest.raises(ResourceError):
        ResourceRef(KIND_NETWORK, "network://https/ExAmPle.com:443/a")  # 未规范化
    with pytest.raises(ResourceError):
        ResourceRef(KIND_MCP, "mcp://files")
    with pytest.raises(ResourceError):
        ResourceRef(KIND_SUBTASK, "subtask://")
    with pytest.raises(ResourceError):
        _ = parse_resource("network://https/h/x").memory_scope


# ---------------------------------------------------------------- 2 许可


def test_outbound_always_needs_approval(tmp_path):
    decision = POLICY.classify(
        ACTION_FETCH, ["network://https/example.com/a"], _bindings(tmp_path)
    )
    assert decision.kind == DECISION_APPROVE
    assert decision.rule_id == "c4.outbound"
    assert decision.needs_approval, "外发没有自动路径"
    assert "example.com" in decision.reason and "/a" in decision.reason

    # 即便目标是一个"看起来公开"的页面也一样：公开与否不是本地能判定的东西
    assert POLICY.classify(
        ACTION_FETCH, ["network://https/example.com/public"], _bindings(tmp_path)
    ).needs_approval


def test_network_action_must_be_fetch(tmp_path):
    decision = POLICY.classify(
        ACTION_READ, ["network://https/example.com/a"], _bindings(tmp_path)
    )
    assert decision.is_denied and decision.rule_id == "c4.action_resource_mismatch"
    # 文件资源配 fetch 同样是错配，而不是"未覆盖"（资源放在绑定目录内，
    # 这样判定的就只可能是种类/动作不匹配，而不是"目录外"）
    file_decision = POLICY.classify(
        ACTION_FETCH, [str(tmp_path / "ws" / "a.md")], _bindings(tmp_path)
    )
    assert file_decision.is_denied
    assert file_decision.rule_id == "c4.action_resource_mismatch"


def test_mcp_is_exec_level(tmp_path):
    decision = POLICY.classify(ACTION_EXEC, ["mcp://files/read_file"], _bindings(tmp_path))
    assert decision.kind == DECISION_APPROVE
    assert decision.rule_id == "c4.mcp_call"
    assert "不可信外部程序" in decision.reason
    mismatch = POLICY.classify(
        ACTION_READ, ["mcp://files/read_file"], _bindings(tmp_path)
    )
    assert mismatch.is_denied and mismatch.rule_id == "c4.action_resource_mismatch"


def test_subtask_decision_depends_on_tool_and_action(tmp_path):
    """G2 子代理 D1 起委派有执行器：判定改成按**工具**区分，不再恒拒。

    旧口径（D1 之前）是"有 `subtask://` 就恒拒 `c4.subtask_unavailable`"；新
    口径是"分类器只回答决策形状"：spawn 需审批、cancel 自动放行、read 自动，
    其它动作/工具是动作-资源不匹配。`c4.subtask_unavailable` 退役为保留未使用。
    """
    spawn = POLICY.classify(
        ACTION_DELEGATE,
        ["subtask://task-1"],
        _bindings(tmp_path),
        tool_id="subtask.spawn",
    )
    assert spawn.kind == DECISION_APPROVE
    assert spawn.rule_id == "c4.subtask_spawn"
    cancel = POLICY.classify(
        ACTION_DELEGATE,
        ["subtask://task-1"],
        _bindings(tmp_path),
        tool_id="subtask.cancel",
    )
    assert cancel.kind == DECISION_AUTO
    assert cancel.rule_id == "c4.subtask_cancel"
    status = POLICY.classify(
        ACTION_READ,
        ["subtask://task-1"],
        _bindings(tmp_path),
        tool_id="subtask.status",
    )
    assert status.kind == DECISION_AUTO
    assert status.rule_id == "c4.auto"
    # 没有工具身份的 delegate（例如有人把它塞进别的工具）：动作-资源不匹配，
    # 不是"看起来有 subtrask 资源就放行"。
    bare = POLICY.classify(
        ACTION_DELEGATE, ["subtask://task-1"], _bindings(tmp_path)
    )
    assert bare.is_denied
    assert bare.rule_id == "c4.action_resource_mismatch"
    mismatch = POLICY.classify(ACTION_EXEC, ["subtask://task-1"], _bindings(tmp_path))
    assert mismatch.is_denied
    assert mismatch.rule_id == "c4.action_resource_mismatch"


def test_mixed_resource_kinds_are_denied(tmp_path):
    decision = POLICY.classify(
        ACTION_READ,
        [str(tmp_path / "a.md"), "network://https/example.com/a"],
        _bindings(tmp_path),
    )
    assert decision.is_denied and decision.rule_id == "c4.mixed_resource_kinds"
    multi_network = POLICY.classify(
        ACTION_FETCH,
        ["network://https/a.com/x", "network://https/b.com/y"],
        _bindings(tmp_path),
    )
    assert multi_network.is_denied
    assert multi_network.rule_id == "c4.multi_resource"


# ---------------------------------------------------------------- 3 摘要绑定


def _request(resources, action=ACTION_FETCH, **overrides) -> ActionRequest:
    base = dict(
        request_id="req_1",
        task_id="task_1",
        run_id="run_1",
        job_id="job_1",
        claim_epoch=1,
        owner_instance_id="inst_1",
        goal_revision=1,
        lease_epoch=1,
        call_id="call_1",
        attempt_id=1,
        tool_id="create_report",
        tool_version="1.0",
        schema_hash="schema_abc",
        action=action,
        resources=tuple(resources),
        args_hash="args_abc",
        description_hash=SPECS["create_report"].description_digest(),
    )
    base.update(overrides)
    return ActionRequest(**base)


def test_approval_binds_the_normalized_network_target(tmp_path):
    """换 host / 换 path 都必须换摘要——否则一张批准能发到另一个地方。"""
    log = EventLog(tmp_path / "db.sqlite")
    try:
        approvals = ApprovalStore(log)
        original = _request(("network://https/example.com/a",))
        decision = POLICY.classify(
            ACTION_FETCH, list(original.resources), _bindings(tmp_path)
        )
        approvals.request(original, decision)

        # 同一个目标的非规范写法：派发层会先把它规范化，所以摘要一致
        same = _request(
            ("network://https/EXAMPLE.com:443/a",)
        )
        assert parse_resource(same.resources[0]).uri == original.resources[0]
        assert dataclasses.replace(
            original, resources=(parse_resource(same.resources[0]).uri,)
        ).action_digest() == original.action_digest()

        # 换主机 → 摘要必须变，旧批准不再匹配
        moved = _request(("network://https/evil.example/a",))
        assert moved.action_digest() != original.action_digest()
        assert approvals.find_for(moved) is None, "换主机后旧批准必须失效"
        # 换路径同样
        other_path = _request(("network://https/example.com/b",))
        assert approvals.find_for(other_path) is None
    finally:
        log.close()


# ---------------------------------------------------------------- 4 执行侧


def test_kinds_without_an_executor_are_refused_before_dispatch():
    """解析得出来 ≠ 能执行。没有执行器的种类在派发前拒绝，不回退成文件路径。

    网络在 G2 网络阶段有了执行器（`network.fetch`），所以它不再走这条拒绝；
    但它**不是**自动放行——任务级白名单（`outbound_allow`）与逐次人工批准
    仍然是两道门，见 `tests/test_outbound_allow.py` 与
    `tests/test_network_fetch.py`。委派在 G2 子代理 D1 有了执行器
    （`subtask.spawn` 等），同样不是自动放行：任务级 `max_subtasks` 声明、
    人工批准与数量/局部限额都还在前面。

    **MCP 是现在唯一没有执行器的种类**：解析得出来、许可判定能做，但派发前拒绝。
    """
    with pytest.raises(DispatchDenied) as excinfo:
        resolve_scheme_resource("mcp://files/read_file")
    assert excinfo.value.rule_id == "dispatch.resource_kind_unsupported"
    assert KIND_MCP in str(excinfo.value)

    # 有执行器的种类照常返回（file 是裸绝对路径，不走这条）
    ref = resolve_scheme_resource("memory://user/local")
    assert ref.kind == KIND_MEMORY
    ref = resolve_scheme_resource("network://https/example.com/a")
    assert ref.kind == KIND_NETWORK
    ref = resolve_scheme_resource("subtask://task-1")
    assert ref.kind == KIND_SUBTASK
    assert ref.subtask_id == "task-1"
    with pytest.raises(DispatchDenied) as excinfo:
        resolve_scheme_resource("https://example.com/a")
    assert excinfo.value.rule_id == "c4.unsupported"


def test_file_tools_never_accept_a_network_target(tmp_path):
    """文件工具的路径参数拒绝 scheme 形状（G1a 的口径），网络 URI 不能搭便车。"""
    from tianshu.errors import ToolArgumentError
    from tianshu.tools import execute
    from tianshu.workspace import Workspace

    ws = Workspace(tmp_path)
    for tool_id in ("read_text", "list_files"):
        with pytest.raises(ToolArgumentError):
            execute(ws, tool_id, {"path": "network://https/example.com/a"})

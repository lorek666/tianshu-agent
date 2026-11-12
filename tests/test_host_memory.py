"""控制面记忆端点（N6 / F10 §9）：**真的起 daemon、真的连 IPC**。

这一组测试要证明的不是"函数能调用"，而是 M5 的三进程语义在记忆上成立：

1. **身份口径（按事实读，不是授权边界）**：这些 RPC 是**人机界面入口**（UI / CLI），
   与模型工具面是两条路——`memory.list` 不是工具，其余同名工具走 `AgentLoop` →
   `Dispatcher` → `MemoryExecutor`（按 `allowed` 过滤）。**但它们挡不住模型**：v1 没有
   进程级沙箱，`run_command` 能以同 uid 起子进程读 `run/authkey`、连 IPC、跑
   `tianshu memory` 并自己落审批。端点权限只在**跨本地用户**这层有意义（POSIX
   `run/` 0700 + socket 0600；**Windows 命名管道 ACL 未加固且未验证**）。所以本文件
   测的是"控制面能看见/能改、且写操作走 dispatcher"，**不是**"模型不可达"。
2. **看得见**：`memory.list` / `memory.get` 必须给出六项判据（`scope` / `origin` /
   `revision` / `status` / `source_refs` / `proof_count`）——缺一项，人就无法判断
   "是谁的、凭什么、改过没有、还算不算数"。
3. **改得动，但只能按规矩改**：`edit` / `forget` / `confirm` / `promote` 全部走
   dispatcher（事件里有 `tool.intent` / `tool.result`），审批当场落库并一次性消费，
   不是"控制面直接改投影"。**负控**：把端点改成直接调 `projection` 改状态 → 对应用例
   必须红（`test_edit_rpc_writes_intent_and_result_events` 断言的就是这条）。
4. **共享是移动**：`promote` 到 workspace 后原作用域不再召回，新行是候选。

正文一律纯文本：`memory.get` 返回的 `content` 就是库里的字符串（测试里断言它含
`<b>` 时原样返回——后端不做任何"安全 HTML"转换，转义是 UI 的事）。
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from tianshu import daemon, ipc
from tianshu.events import EventLog
from tianshu.loop import Limits
from tianshu.model import ScriptedModel
from tianshu.tools import tool_manifest

#: 控制面的 9 个记忆 RPC（`memory.list` 不是模型工具；其余同名但路径不同）。
#: 这个集合必须与 `ControlPlane._methods` 里的记忆方法**逐一对应**：漏一个就意味着
#: "某个能力根本没注册"，而注册表覆盖断言是唯一能发现它的地方。
CONTROL_PLANE_METHODS = {
    "memory.list",
    "memory.search",
    "memory.get",
    "memory.remember",
    "memory.confirm",
    "memory.edit",
    "memory.forget",
    "memory.promote",
    "memory.erase",
}
#: F10 §9 的六项判据（`memory_id` 之外必须一个不少）。
REQUIRED_FIELDS = {"scope", "origin", "revision", "status", "source_refs", "proof_count"}


def _short_root(tmp_path: Path) -> tuple[Path, bool]:
    """unix socket 路径上限 100 字节：太长就退到系统临时目录（同 test_daemon）。"""
    candidate = tmp_path / "data"
    try:
        ipc.endpoint_path(candidate)
        return candidate, True
    except ipc.ProtocolError:
        return Path(tempfile.mkdtemp(prefix="tsm-", dir=tempfile.gettempdir())), False


class Daemon:
    """在子线程里 `daemon.serve()`，并提供一条已握手的 IPC 连接。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self._run, name="test-host-memory", daemon=True)

    def _run(self) -> None:
        try:
            daemon.serve(
                self.root,
                model=ScriptedModel([]),
                limits=Limits(max_turns=1),
                driver_interval_s=0.02,
                on_ready=self.ready.set,
                stop_event=self.stop,
            )
        except BaseException as exc:  # noqa: BLE001 - 测试要把启动失败带回来
            self.errors.append(exc)
        finally:
            self.ready.set()

    def start(self, timeout: float = 20.0) -> Daemon:
        self.thread.start()
        assert self.ready.wait(timeout), "daemon 没有在超时内就绪"
        if self.errors:
            raise self.errors[0]
        return self

    def stop_and_wait(self, timeout: float = 10.0) -> bool:
        self.stop.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()


@pytest.fixture()
def daemon_root(tmp_path):
    root, inside_tmp = _short_root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    yield root
    if not inside_tmp:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def live(daemon_root):
    """起一个真 daemon，给一条真 IPC 连接；用例结束关掉。"""
    harness = Daemon(daemon_root).start()
    client = ipc.Client(daemon_root, timeout=5)
    client.connect()
    try:
        yield client, daemon_root
    finally:
        client.close()
        harness.stop_and_wait()


def _events(root: Path) -> list:
    """另开一条只读连接把事件读回来。

    断言的是"真的落了 intent/result"，不是"RPC 返回了 ok"——控制面端点如果直接
    改投影，这里就会缺事件。
    """
    log = EventLog(root / "events.db")
    try:
        return log.read_after(0)
    finally:
        log.close()


def _event_types(root: Path) -> list[str]:
    return [event.type for event in _events(root)]


# --------------------------------------------------------------------------- 看得见


def test_memory_list_has_no_model_tool_counterpart() -> None:
    """`memory.list` 只在控制面 RPC 里：模型没有"列全库"的能力。

    其余方法名（`memory.get` / `edit` / `forget` …）与模型工具**同名但不同路**：
    模型那条走 `AgentLoop` → `Dispatcher` → `MemoryExecutor`（按作用域装备、按
    `allowed` 过滤）；人这条路走 daemon 的 `ControlPlane`（本机人身份）。名字相同
    不代表权限相同——真正决定权限的是"走哪条路"。
    """
    names = {tool["name"] for tool in tool_manifest()}
    assert "memory.list" not in names, "模型不该有列出全库记忆的工具"


def test_all_nine_control_plane_methods_are_registered(tmp_path) -> None:
    """9 个记忆 RPC 都注册在控制面上：少一个 UI 就有一块功能是死的。"""
    from support import stack_for

    from tianshu.control import ControlError, ControlPlane

    log = EventLog(tmp_path / "events.db")
    try:
        stack = stack_for(log, data_root=tmp_path)
        plane = ControlPlane(stack, model=ScriptedModel([]), limits=Limits(max_turns=1))
        for method in sorted(CONTROL_PLANE_METHODS):
            try:
                plane.handle(method, {})
            except ControlError as error:
                # 参数不对是允许的；"未知方法"就说明它根本没注册。
                assert error.code != "unknown_method", method
    finally:
        log.close()


def test_list_and_get_return_the_six_judgement_fields(live) -> None:
    """`memory.list` / `memory.get` 给出六项判据，正文是纯文本原样返回。"""
    client, _root = live
    content = "<b>报告</b>用中文写；见 https://example.com"
    result = client.call(
        "memory.remember", {"kind": "preference", "content": content}
    )
    assert result["status"] == "succeeded", result
    memory_id = result["output"]["memory_id"]

    listed = client.call("memory.list", {})
    assert listed["items"], "刚记住的记忆应该出现在列表里"
    item = next(entry for entry in listed["items"] if entry["memory_id"] == memory_id)
    assert REQUIRED_FIELDS <= set(item), REQUIRED_FIELDS - set(item)
    assert item["origin"] == "user_explicit"
    assert item["status"] == "active"
    assert item["scope"] == "user"
    assert item["content"] is None, "列表不带正文（按需 get）"

    got = client.call("memory.get", {"memory_id": memory_id})["memory"]
    assert REQUIRED_FIELDS <= set(got)
    assert got["content"] == content, "正文必须逐字节原样返回，不做 HTML 转义"
    assert got["content_state"] == "available"


def test_default_scope_is_user_and_other_scopes_must_be_named(live) -> None:
    """默认只看 `user`；要看 task 作用域必须显式给 scope——与 CLI 同口径。"""
    client, _root = live
    task_id = "task_n6"
    result = client.call(
        "memory.remember",
        {
            "kind": "fact",
            "content": "任务私有的临时结论",
            "scope": "task",
            "scope_id": task_id,
        },
    )
    assert result["status"] == "succeeded", result
    task_memory = result["output"]["memory_id"]

    default_items = client.call("memory.list", {})["items"]
    assert task_memory not in {item["memory_id"] for item in default_items}
    scoped = client.call(
        "memory.list", {"scope": "task", "scope_id": task_id}
    )["items"]
    assert task_memory in {item["memory_id"] for item in scoped}
    # 缺 scope_id 是明确的参数错误，不是"悄悄按默认作用域"。
    with pytest.raises(ipc.RemoteError) as error:
        client.call("memory.list", {"scope": "task"})
    assert error.value.code == "bad_params"


# --------------------------------------------------------------------------- 检索


def test_memory_search_rpc_returns_capability_labels(live) -> None:
    """M1-a：`memory.search` 走 `retrieve()`，能力标注与投影一致（不是 FTS-only）。

    为什么必须断言 `semantic` / `degraded` / `fused`：人在 UI 上检索时，空结果要能
    分清"没有这条记忆"与"这次只有词面召回"。用投影的 `search()`（FTS-only）实现
    时这三个键会退化成 `False` / `None` / `False`，与真实能力不符——这条会红。
    """
    client, _root = live
    client.call(
        "memory.remember",
        {"kind": "preference", "content": "报告用中文写，先列结论"},
    )
    result = client.call("memory.search", {"query": "报告"})
    # 能力标注必须**存在且是三个独立字段**，不能压成一句 found/not-found。
    assert set(result) >= {"items", "semantic", "degraded", "fused", "query", "scopes"}
    assert result["query"] == "报告"
    assert isinstance(result["semantic"], bool)
    assert isinstance(result["fused"], bool)
    # 本机没有真 embedding：语义路如实为 false，并给出原因（不许装成"有语义"）。
    assert result["semantic"] is False
    assert result["degraded"], "没有语义能力时必须给出降级原因"
    assert result["scopes"] == ["memory://user/local"]
    assert result["items"], "刚记住的记忆应当被词面检索命中"
    item = result["items"][0]
    # 条目形状与模型侧同口径：六项判据都在，且带排序解释。
    assert REQUIRED_FIELDS <= set(item), REQUIRED_FIELDS - set(item)
    assert item["content"], "检索带正文（列表才刻意不带）"
    assert "score" in item and item["score_kind"] in {"origin", "rrf"}


def test_memory_search_rpc_is_scope_filtered(live) -> None:
    """M1-b：作用域过滤生效——未授权作用域的命中不出现，`scopes` 回显与实际一致。

    口径按既有控制面语义读（`ControlPlane._memory_allowed`）：**默认只有 `user`**；
    显式给 `scope` + `scope_id` 是**追加**一个可见作用域（人永远看得见自己的 user
    记忆），不是替换。所以这里钉两件事：① 默认检索不串到 task 作用域；② 指名
    task A 之后，**task B 的私有记忆仍然不出现**（这才是"过滤生效"）。
    """
    client, _root = live
    user_id = client.call(
        "memory.remember", {"kind": "preference", "content": "周报用中文"}
    )["output"]["memory_id"]
    task_a = client.call(
        "memory.remember",
        {"kind": "fact", "content": "周报用中文写，结论先行", "scope": "task",
         "scope_id": "task_a"},
    )["output"]["memory_id"]
    task_b = client.call(
        "memory.remember",
        {"kind": "fact", "content": "周报里不要写结论", "scope": "task",
         "scope_id": "task_b"},
    )["output"]["memory_id"]

    default = client.call("memory.search", {"query": "周报"})
    assert default["scopes"] == ["memory://user/local"]
    assert user_id in {item["memory_id"] for item in default["items"]}
    assert {task_a, task_b} & {item["memory_id"] for item in default["items"]} == set(), (
        "默认只看 user：task 作用域的私有记忆不许漏进来"
    )

    scoped = client.call(
        "memory.search", {"query": "周报", "scope": "task", "scope_id": "task_a"}
    )
    assert scoped["scopes"] == ["memory://user/local", "memory://task/task_a"]
    hits = {item["memory_id"] for item in scoped["items"]}
    assert task_a in hits and user_id in hits, "人永远看得见自己的 user 记忆"
    assert task_b not in hits, "指名 task_a 不代表能看 task_b"

    only_b = client.call(
        "memory.search", {"query": "周报", "scope": "task", "scope_id": "task_b"}
    )
    assert task_a not in {item["memory_id"] for item in only_b["items"]}


def test_memory_search_rpc_empty_is_not_an_error(live) -> None:
    """M1-c：空结果与能力降级是**不同形状**——空结果不是错误，降级原因照样给。"""
    client, _root = live
    result = client.call("memory.search", {"query": "库里根本没有这条"})
    assert result["items"] == []
    assert result["semantic"] is False and result["degraded"]
    assert result["scopes"] == ["memory://user/local"]


def test_memory_search_rpc_param_errors(live) -> None:
    """M1-d：参数错误面统一是 `bad_params`，不是 500、也不是静默空结果。"""
    client, _root = live
    for params in (
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 42},
        {"query": "报告", "limit": 0},
        {"query": "报告", "limit": -1},
        {"query": "报告", "limit": "5"},
        {"query": "报告", "limit": True},
        {"query": "报告", "scope": "task"},  # 缺 scope_id
        {"query": "报告", "scope": "nope"},
    ):
        with pytest.raises(ipc.RemoteError) as error:
            client.call("memory.search", params)
        assert error.value.code == "bad_params", params
    # 超过上限按上限夹，不报错（与模型侧同口径）。
    clamped = client.call("memory.search", {"query": "报告", "limit": 99})
    assert clamped["limit"] == 5


# --------------------------------------------------------------------------- 改得动


def test_edit_rpc_writes_intent_and_result_events(live) -> None:
    """`memory.edit` 走 dispatcher：事件里有 intent/result，旧行退役、新行生效。

    这条同时是**负控的锚点**：如果哪天有人把端点改成直接改 projection，事件日志里
    就不会有 `tool.intent`/`tool.result`，这条会红——这正是"控制面不许偷写存储"。
    """
    client, root = live
    original = client.call(
        "memory.remember", {"kind": "preference", "content": "报告用中文写"}
    )["output"]["memory_id"]

    edited = client.call(
        "memory.edit",
        {"memory_id": original, "revision": 1, "content": "报告用中文写，先列结论"},
    )
    assert edited["status"] == "succeeded", edited
    assert edited["effect_state"] == "applied"
    new_id = edited["output"]["memory_id"]
    assert new_id != original
    assert edited["memory"]["content"] == "报告用中文写，先列结论"
    assert edited["memory"]["origin"] == "user_explicit"

    old = client.call("memory.get", {"memory_id": original})["memory"]
    assert old["status"] == "superseded", "旧版必须保留可查（不是删掉）"
    assert old["content"] == "报告用中文写"

    # 事件必须**是 edit 的**：只断言"有 intent/result"会被 remember 的旧事件掩盖，
    # 端点直接改投影（不经 dispatcher）时这里就查不到 `tool_id == memory.edit`。
    events = _events(root)
    edit_intents = [
        event.payload
        for event in events
        if event.type == "tool.intent" and event.payload.get("tool_id") == "memory.edit"
    ]
    edit_results = [
        event.payload
        for event in events
        if event.type == "tool.result" and event.payload.get("tool_id") == "memory.edit"
    ]
    assert edit_intents, "edit 必须留下 tool.intent（run 级证据，不是端点返回的 ok）"
    assert edit_results and edit_results[-1]["status"] == "succeeded"
    superseded_output = edit_results[-1]["output"]
    assert superseded_output["supersedes"] == original
    assert superseded_output["memory_id"] == new_id
    # 审批一致性：写操作在控制面由人当场签发，但审批仍落库、仍一次性消费。
    approvals = [
        event.payload for event in _events(root) if event.type == "approval.decided"
    ]
    assert approvals, "编辑应当留下一条已决定的审批"
    assert approvals[-1].get("decision") == "approved"


def test_forget_moves_state_through_the_dispatcher(live) -> None:
    """`memory.forget` 变 tombstone，且留在事件日志里（不是从投影里抹掉）。"""
    client, root = live
    remembered = client.call(
        "memory.remember", {"kind": "fact", "content": "周五下午不排会"}
    )
    memory_id = remembered["output"]["memory_id"]
    forgotten = client.call(
        "memory.forget", {"memory_id": memory_id, "revision": 1}
    )
    assert forgotten["status"] == "succeeded", forgotten
    assert forgotten["memory"]["status"] == "forgotten"
    listed = client.call("memory.list", {"status": "forgotten"})["items"]
    assert memory_id in {item["memory_id"] for item in listed}
    types = _event_types(root)
    assert types.count("tool.result") >= 2, "remember + forget 各一条 result"


def test_confirm_on_an_already_active_memory_is_rejected(live) -> None:
    """`memory.remember` 直接写 active：对它 confirm 是明确的拒绝，不是空操作。

    confirm 的语义是"候选 → 活动"。给一条已经活动的记忆再确认，应当报清楚
    "当前 status 不是 candidate"，并且**一个字节都不改**（effect_state
    `none_applied`）——不假装成功，也不静默改状态。
    """
    client, _root = live
    memory_id = client.call(
        "memory.remember", {"kind": "fact", "content": "结论：先做备份"}
    )["output"]["memory_id"]
    rejected = client.call("memory.confirm", {"memory_id": memory_id, "revision": 1})
    assert rejected["status"] != "succeeded"
    assert rejected["effect_state"] == "none_applied"
    assert "只能确认候选记忆" in rejected["output"]["error"]
    still = client.call("memory.get", {"memory_id": memory_id})["memory"]
    assert still["status"] == "active" and still["revision"] == 1


def test_promote_moves_the_memory_and_old_scope_stops_recalling(live, tmp_path) -> None:
    """`memory.promote` 是**移动**：原作用域退役、目标作用域是候选、再 confirm 才召回。"""
    client, _root = live
    workspace = tmp_path / "ws_n6"
    workspace.mkdir()
    remembered = client.call(
        "memory.remember", {"kind": "preference", "content": "本仓库的提交信息用中文"}
    )
    memory_id = remembered["output"]["memory_id"]
    promoted = client.call(
        "memory.promote",
        {
            "memory_id": memory_id,
            "revision": 1,
            "scope": "workspace",
            "scope_id": str(workspace),
        },
    )
    assert promoted["status"] == "succeeded", promoted
    new_id = promoted["output"]["memory_id"]
    assert new_id != memory_id

    original = client.call("memory.get", {"memory_id": memory_id})["memory"]
    assert original["status"] == "superseded", "原行必须退役，避免双份真相"
    moved = client.call(
        "memory.get",
        {
            "memory_id": new_id,
            "scope": "workspace",
            "scope_id": str(workspace),
        },
    )["memory"]
    assert moved["scope"] == "workspace"
    assert moved["status"] == "candidate", "新行是候选：共享不等于自动召回"
    assert memory_id in moved["source_refs"], "来源必须指回原行"
    # 只看 user 作用域时，旧行不在召回范围、新行也不在（候选且换作用域）。
    user_items = client.call("memory.list", {"status": "active"})["items"]
    assert memory_id not in {item["memory_id"] for item in user_items}


# --------------------------------------------------------------------------- 错误面


def test_unknown_memory_and_bad_params_are_explicit(live) -> None:
    """不存在 / 参数不对必须给可判的错误码，不静默返回空。"""
    client, _root = live
    with pytest.raises(ipc.RemoteError) as missing:
        client.call("memory.get", {"memory_id": "mem_does_not_exist"})
    assert missing.value.code == "not_found"

    with pytest.raises(ipc.RemoteError) as no_content:
        client.call("memory.remember", {"kind": "fact"})
    assert no_content.value.code == "bad_params"

    with pytest.raises(ipc.RemoteError) as unknown_method:
        client.call("memory.does_not_exist", {})
    assert unknown_method.value.code == "unknown_method"


def test_stale_revision_is_refused_before_dispatch(live) -> None:
    """人看到的是哪一版就写哪一版：revision 过期立即 conflict，不先派发再拒。"""
    client, _root = live
    memory_id = client.call(
        "memory.remember", {"kind": "fact", "content": "结论：先做备份"}
    )["output"]["memory_id"]
    edited = client.call(
        "memory.edit", {"memory_id": memory_id, "revision": 1, "content": "结论：先做演练"}
    )
    new_id = edited["output"]["memory_id"]
    current = edited["memory"]["revision"]
    # 传一个**不是当前版本**的 revision：控制面应当立刻 conflict，而不是先派发。
    with pytest.raises(ipc.RemoteError) as stale:
        client.call(
            "memory.edit",
            {"memory_id": new_id, "revision": current + 1, "content": "结论：先做割接"},
        )
    assert stale.value.code == "conflict"
    assert (current + 1) != current

def test_task_scope_edit_and_forget_return_the_updated_view(live) -> None:
    """task / workspace 作用域的写操作也要把**写后的视图**带回来。

    `_memory_after_call` 曾固定只按 `user` 作用域读，于是非 user 记忆的 edit / forget
    会"状态成功、`memory` 是 null"——UI 拿不到"现在算什么"（复审抓到的功能缺口）。
    """
    client, _root = live
    task_id = "task_n6_view"
    remembered = client.call(
        "memory.remember",
        {
            "kind": "fact",
            "content": "任务里的临时结论：先做备份",
            "scope": "task",
            "scope_id": task_id,
        },
    )
    memory_id = remembered["output"]["memory_id"]
    edited = client.call(
        "memory.edit",
        {
            "memory_id": memory_id,
            "revision": 1,
            "content": "任务里的临时结论：先做演练",
            "scope": "task",
            "scope_id": task_id,
        },
    )
    assert edited["status"] == "succeeded", edited
    assert edited["memory"] is not None, "task 作用域的编辑必须带回写后视图"
    assert edited["memory"]["scope"] == "task"
    assert edited["memory"]["scope_id"] == task_id
    assert edited["memory"]["content"] == "任务里的临时结论：先做演练"

    forgotten = client.call(
        "memory.forget",
        {
            "memory_id": edited["memory"]["memory_id"],
            "revision": edited["memory"]["revision"],
            "scope": "task",
            "scope_id": task_id,
        },
    )
    assert forgotten["memory"] is not None, "task 作用域的忘记必须带回写后视图"
    assert forgotten["memory"]["status"] == "forgotten"


def test_promote_from_workspace_to_user_uses_source_scope(live, tmp_path) -> None:
    """`promote` 的源作用域与目标作用域是两组参数：workspace → user 必须能看见源行。

    原来两者共用 `scope`/`scope_id`，于是"从 workspace 提到 user"永远 `not_found`
    （目标 user 的可见集合里没有 workspace 行）；从 task 提同理（复审抓到的功能缺口）。
    """
    client, _root = live
    workspace = tmp_path / "ws_n6_promote"
    workspace.mkdir()
    remembered = client.call(
        "memory.remember",
        {
            "kind": "preference",
            "content": "这个仓库的提交信息用中文",
            "scope": "workspace",
            "scope_id": str(workspace),
        },
    )
    memory_id = remembered["output"]["memory_id"]
    promoted = client.call(
        "memory.promote",
        {
            "memory_id": memory_id,
            "revision": 1,
            "source_scope": "workspace",
            "source_scope_id": str(workspace),
            "scope": "user",
            "scope_id": "local",
        },
    )
    assert promoted["status"] == "succeeded", promoted
    assert promoted["memory"] is not None
    assert promoted["memory"]["scope"] == "user"
    assert promoted["memory"]["status"] == "candidate"
    assert memory_id in promoted["memory"]["source_refs"]
    original = client.call(
        "memory.get",
        {
            "memory_id": memory_id,
            "scope": "workspace",
            "scope_id": str(workspace),
        },
    )["memory"]
    assert original["status"] == "superseded"


def test_erase_rpc_removes_the_body_copy_and_reports_it(live) -> None:
    """`memory.erase` 也在控制面：删除正文副本 + 标 erased_at，回执可核对。

    界面上"删除"有两档：`forget` 是停召回（tombstone），`erase` 才删副本与索引。
    """
    client, _root = live
    memory_id = client.call(
        "memory.remember", {"kind": "fact", "content": "这条会被彻底删掉"}
    )["output"]["memory_id"]
    forgotten = client.call("memory.forget", {"memory_id": memory_id, "revision": 1})
    assert forgotten["status"] == "succeeded", forgotten
    erased = client.call(
        "memory.erase", {"memory_id": memory_id, "revision": forgotten["memory"]["revision"]}
    )
    assert erased["status"] == "succeeded", erased
    assert erased["output"]["erasure"]["deleted"] is True
    assert erased["memory"] is not None
    assert erased["memory"]["status"] == "forgotten"
    assert erased["memory"]["erased_at"] is not None
    assert erased["memory"]["content"] is None
    assert erased["memory"]["content_state"] == "unavailable"
    got = client.call("memory.get", {"memory_id": memory_id})["memory"]
    assert got["content"] is None, "擦除后正文必须不可用（不回落到最新版本）"


def test_host_rpc_proxies_memory_endpoints_with_the_ui_token(live, tmp_path) -> None:
    """真 Host 的 `POST /rpc` 能把记忆端点代理给 daemon（带 UI token）。

    前面所有用例都直连 IPC——那证明的是控制面本身；这一条补的是**UI 实际走的那层**：
    浏览器 → Host（校验 Origin/token）→ IPC → `ControlPlane`。Host 的 token/路由/proxy
    一旦坏掉，直连 IPC 的用例仍会全绿（复审指出的缺口）。
    """
    import json as _json
    import socket as _socket

    from tianshu import host

    client, root = live
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True, exist_ok=True)
    (ui / "index.html").write_bytes(
        b'<!doctype html><html><head><meta name="tianshu-token" '
        b'content="__TIANSHU_TOKEN__"></head><body>x</body></html>'
    )
    (ui / "assets" / "app.js").write_bytes(b"console.log('hi')")
    bridge = host.UiBridge(
        host.BridgeConfig(data_root=root, ui_dir=ui, token="tok-n6", port=0)
    )
    bridge.serve_background()
    port = bridge.port
    try:
        def rpc(method: str, params: dict) -> dict:
            body = _json.dumps({"method": method, "params": params}).encode("utf-8")
            with _socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                head = [
                    "POST /rpc HTTP/1.1",
                    f"Host: 127.0.0.1:{port}",
                    "X-Tianshu-Token: tok-n6",
                    f"Content-Length: {len(body)}",
                    "Connection: close",
                ]
                sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body)
                raw = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
            payload = _json.loads(raw.partition(b"\r\n\r\n")[2].decode("utf-8"))
            assert payload["ok"], payload
            return payload["result"]

        remembered = rpc("memory.remember", {"kind": "preference", "content": "UI 也要能记住"})
        assert remembered["status"] == "succeeded", remembered
        memory_id = remembered["output"]["memory_id"]
        listed = rpc("memory.list", {})
        assert memory_id in {item["memory_id"] for item in listed["items"]}
        got = rpc("memory.get", {"memory_id": memory_id})
        assert got["memory"]["content"] == "UI 也要能记住"
        # 没有 token 的请求必须被 Host 拒（记忆端点不能成为未授权读取口）。
        status, payload = _unauthed_rpc(port, "memory.list", {})
        assert status == 401, (status, payload)
    finally:
        bridge.stop()
        client.close()


def _unauthed_rpc(port: int, method: str, params: dict) -> tuple[int, bytes]:
    import json as _json
    import socket as _socket

    body = _json.dumps({"method": method, "params": params}).encode("utf-8")
    with _socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        head = [
            "POST /rpc HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body)
        raw = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
    head_bytes, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head_bytes.split(b"\r\n")[0].split(b" ")[1])
    return status, payload

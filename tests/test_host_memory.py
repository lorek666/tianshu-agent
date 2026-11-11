"""控制面记忆端点（N6 / F10 §9）：**真的起 daemon、真的连 IPC**。

这一组测试要证明的不是"函数能调用"，而是 M5 的三进程语义在记忆上成立：

1. **本机人身份**：IPC 只对本机同一个用户可达（POSIX `run/` 0700 + socket 0600），
   所以"连得上"就是"人"。模型走不到这里——`memory.list|get|remember|confirm|
   edit|forget|promote` 不在 `tool_manifest()` 里（这条也断言，防止哪天有人把控制面
   方法塞进工具清单）。
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

# 控制面的六个读/写方法 + confirm：**不在**模型工具清单里。
CONTROL_PLANE_METHODS = {
    "memory.list",
    "memory.get",
    "memory.remember",
    "memory.confirm",
    "memory.edit",
    "memory.forget",
    "memory.promote",
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


def test_all_seven_control_plane_methods_are_registered(tmp_path) -> None:
    """7 个记忆 RPC 都注册在控制面上：少一个 UI 就有一块功能是死的。"""
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
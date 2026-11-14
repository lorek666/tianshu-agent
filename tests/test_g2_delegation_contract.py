"""G2 子代理 D1：合同（`task.created.max_subtasks` + 事件/规则/工具 id 镜像）。

判据（规格 §3 D1-c1 / D1-c11）：

- `task.created` 必填 `max_subtasks`（0–4）；旧事件（缺键）按 v0 读成"不能委派"；
  非法值拒绝，且不会留下半个任务。
- 契约三处同改：`event_schema.CURRENT_SCHEMAS` / `schemas/c3/event.json`、
  `rules.RULE_IDS` / `schemas/c4/rules.json`、`tools.SPECS` / `_TOOL_ID`。
  数字以**实施时实测**为准：事件 43、规则 68、工具 23。

为什么先测合同：D1 之前 `task.created` 里没有委派能力的字段，任何"这个任务准不准
派子任务"的判定都只能靠进程内默认值——崩溃恢复后就没有第二份证据了。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tianshu import event_schema
from tianshu.events import EVENT_TYPES, EventLog
from tianshu.rules import (
    DECISION_RULE_IDS,
    DECLARED_UNUSED_RULE_IDS,
    REJECTION_RULE_IDS,
    RULE_IDS,
)
from tianshu.tasks import SUBTASK_MAX_PER_ROOT_TASK, TaskStore
from tianshu.tools import SPECS

REPO = Path(__file__).resolve().parents[1]
C3 = REPO / "schemas" / "c3" / "event.json"
C4 = REPO / "schemas" / "c4" / "rules.json"
M2_TASK = REPO / "schemas" / "m2" / "task.json"

#: 实施时实测的契约增量（不是规格里的预估数字）。
#:
#: 规则数说明：D1 分支基线的 `main` 是 59；本分支 +9（§2.7 的 8 条 + §2.3 额外
#: 要求的 `dispatch.subtask_rejected`）；合并 `main` 上的 G2 MCP 阶段（+6）之后
#: 是 74；对象级阻断工单 +1（`dispatch.mcp_object_blocked`）= 75；低磁盘准入
#: 再显式登记三项空间/资格拒绝 = 78。D1 自己的增量与规格预估的差异写进 PR
#: body 对账。
#:
#: 事件类型 43 → 45：对象级阻断新增 `recovery.object_blocked` /
#: `recovery.object_unblocked`（MCP 后续工单 §5-8）。
EXPECTED_EVENT_TYPES = 45
EXPECTED_TOOL_IDS = 23
EXPECTED_RULE_IDS = 78


def _stack(tmp_path: Path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    from support import stack_for

    return log, stack_for(log, data_root=tmp_path / "data")


def _task_created_events(log: EventLog, task_id: str) -> list:
    return [
        event
        for event in log.read_run("", verify=False)
        if event.type == "task.created" and event.payload.get("task_id") == task_id
    ]


# ---------------------------------------------------------------- D1-c1


def test_task_max_subtasks_contract(tmp_path):
    """`max_subtasks` 是必填能力声明：0–4 接受、越界拒绝、事件恒写。

    参数化的五个合法值覆盖"不委派"到"最多 4 个"；四个非法值各代表一类错误
    （负数 / 越上限 / 字符串 / 布尔）。布尔被单独挑出来是因为 `isinstance(True, int)`
    为真——不显式排除就会让 `True` 冒充 1。
    """
    log, stack = _stack(tmp_path)
    try:
        for value in range(SUBTASK_MAX_PER_ROOT_TASK + 1):
            task = stack.tasks.create_task(f"任务-{value}", max_subtasks=value)
            assert task.max_subtasks == value
            assert task.depth == 0
            assert task.parent_task_id is None
            (event,) = _task_created_events(log, task.task_id)
            # 新事件恒写这个键（0 也写）：缺键会被读成 v0，而"没写"与
            # "明确不允许"必须是两件事。
            assert event.payload["max_subtasks"] == value
            assert event.payload_version == 1
        for bad in (-1, 5, "2", 2.5, True):
            with pytest.raises(Exception, match="max_subtasks"):
                stack.tasks.create_task("坏声明", max_subtasks=bad)  # type: ignore[arg-type]
        # 非法值不会留下半个任务：拒绝必须发生在 INSERT 之前。
        rows = log.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        assert rows == SUBTASK_MAX_PER_ROOT_TASK + 1
    finally:
        log.close()


def test_legacy_task_created_without_max_subtasks_reads_as_v0(tmp_path):
    """旧事件缺 `max_subtasks`：按 v0 合法，旧库补列后读成 0（不能凭空获得能力）。

    两件事一起测，因为它们必须同时成立：历史日志不能因为新增必填键就变非法；
    旧库升级（补列）不能把"没声明"读成"可以委派"。
    """
    log, stack = _stack(tmp_path)
    try:
        task = stack.tasks.create_task("旧库里的任务")
        legacy_payload = {
            "task_id": "task_legacy000001",
            "goal": "旧任务",
            "goal_revision": 1,
            "lease_epoch": 1,
            "workspaces": [],
            "exec_policy": None,
            "control": False,
            "memory_scopes": [],
        }
        # v1 拒绝（缺必填），v0 接受——这就是"旧事件仍然合法"的机器证明。
        assert event_schema.payload_errors("task.created", legacy_payload, 1)
        assert event_schema.payload_errors("task.created", legacy_payload, 0) == []
        log.append(
            "", "task.created", legacy_payload, task_id=task.task_id, legacy_write=True
        )
        legacy = next(
            event
            for event in log.read_run("", verify=False)
            if event.type == "task.created"
            and "max_subtasks" not in event.payload
        )
        assert legacy.payload_version == 0
    finally:
        log.close()

    # 旧库没有这一列：打开时补列，旧行读成 depth=0 / max_subtasks=0。
    conn = sqlite3.connect(tmp_path / "events.db")
    conn.execute("ALTER TABLE tasks DROP COLUMN max_subtasks")
    conn.execute("ALTER TABLE tasks DROP COLUMN depth")
    conn.commit()
    conn.close()
    log2 = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        store = TaskStore(log2)
        old = store.get_task(task.task_id)
        assert old is not None
        assert old.max_subtasks == 0, "补列不能顺便补出一种能力"
        assert old.depth == 0
    finally:
        log2.close()


def test_task_created_schema_declares_the_parent_chain(tmp_path):
    """子任务的父链字段与 `max_subtasks` 同时进契约（可选父链 + 必填能力）。

    只测"能写进去"不够：还要证明**非子任务不许带父链**（`TaskStore` 拒绝），
    否则"谁派的"会出现两个答案。
    """
    props = event_schema.CURRENT_SCHEMAS["task.created"]["properties"]
    assert props["max_subtasks"] == {"type": "integer", "enum": [0, 1, 2, 3, 4]}
    for key in ("parent_task_id", "parent_run_id", "subtask_id"):
        assert props[key]["type"] == "string"
    assert props["depth"] == {"type": "integer", "enum": [0, 1]}
    assert "max_subtasks" in event_schema.CURRENT_SCHEMAS["task.created"]["required"]

    log, stack = _stack(tmp_path)
    try:
        with pytest.raises(Exception, match="父链"):
            stack.tasks.create_task(
                "根任务不能有父", parent_task_id="task_x", depth=0
            )
        with pytest.raises(Exception, match="parent_task_id"):
            stack.tasks.create_task("子任务缺父链", depth=1)
        with pytest.raises(Exception, match="depth"):
            stack.tasks.create_task("深度 2", depth=2)
    finally:
        log.close()


# ---------------------------------------------------------------- D1-c11


def test_event_schema_mirror():
    """事件类型 45 + 逐类型契约与 `schemas/c3/event.json` 逐字一致。

    事件类型是封闭集合，"多了两个类型"必须在代码、schema 文件、PRD §5.3.2
    三处同时成立；这里把前两处绑死（PRD 由 `test_rule_ids` 之外的文档对账覆盖）。
    """
    assert len(EVENT_TYPES) == EXPECTED_EVENT_TYPES
    for name in ("subtask.spawned", "subtask.finished"):
        assert name in EVENT_TYPES
        assert name in event_schema.CURRENT_SCHEMAS

    raw = C3.read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert set(doc["$defs"]["payload"]) == set(EVENT_TYPES)
    assert doc["$defs"]["payload"] == event_schema.CURRENT_SCHEMAS
    assert doc["$defs"]["legacyPayload"] == event_schema.LEGACY_SCHEMAS
    assert doc["properties"]["type"]["enum"] == sorted(EVENT_TYPES)
    # byte-exact 往返：schema 文件必须是"代码 dump 出来的那一份"，否则
    # `json.dumps(doc, indent=2, ensure_ascii=False) + "\n"` 会漂移出无意义的 diff。
    assert json.dumps(doc, indent=2, ensure_ascii=False) + "\n" == raw


def test_tool_id_enum():
    """工具 id 23：`SPECS`、事件 schema 的 `_TOOL_ID`、C4 规则表三处一致。

    `_TOOL_ID` 出现在 `tool.intent` / `tool.result` / `approval.requested` 等
    payload 里，是**写入前**的封闭枚举：少一个 id 会让新工具的结果写不进日志。
    """
    assert len(SPECS) == EXPECTED_TOOL_IDS
    for tool_id in (
        "subtask.spawn",
        "subtask.status",
        "subtask.result",
        "subtask.cancel",
    ):
        assert tool_id in SPECS
    tool_enum = event_schema._TOOL_ID["enum"]
    assert len(tool_enum) == EXPECTED_TOOL_IDS
    assert set(tool_enum) == set(SPECS)
    assert len(set(tool_enum)) == len(tool_enum), "工具 id 不能重复"

    # 规则 id 78（D1 75 + 低磁盘资格/空间未知/空间不足 3 条）。规格 §2.7 对
    # D1 增量的预估是 8 条，差的那一条是 §2.3 要求的
    # `dispatch.subtask_rejected`。
    assert len(RULE_IDS) == EXPECTED_RULE_IDS
    raw = C4.read_text(encoding="utf-8")
    doc = json.loads(raw)
    assert doc["properties"]["rule_id"]["enum"] == sorted(RULE_IDS)
    assert set(doc["x-declared-unused"]) == set(DECLARED_UNUSED_RULE_IDS)
    assert set(doc["x-decision-rule-ids"]) == set(DECISION_RULE_IDS)
    assert set(doc["x-rule-descriptions"]) == set(RULE_IDS)
    assert json.dumps(doc, indent=2, ensure_ascii=False) + "\n" == raw
    # 三分法仍然覆盖全集且互不相交（新增 9 条不能破坏这条不变量）。
    assert REJECTION_RULE_IDS | DECISION_RULE_IDS | DECLARED_UNUSED_RULE_IDS == (
        RULE_IDS
    )
    assert REJECTION_RULE_IDS.isdisjoint(DECISION_RULE_IDS)
    assert REJECTION_RULE_IDS.isdisjoint(DECLARED_UNUSED_RULE_IDS)
    assert DECISION_RULE_IDS.isdisjoint(DECLARED_UNUSED_RULE_IDS)
    # 委派的两条判定必须是"判定结果"而不是拒绝：spawn 审批、cancel 自动。
    assert {"c4.subtask_spawn", "c4.subtask_cancel"} <= DECISION_RULE_IDS
    # 委派退役编号仍是"保留未使用"；D2 起 `dispatch.parent_fenced` 有了产生
    # 路径（子 Run 派发前核验父目标版本），必须从保留集合里移出。
    assert "c4.subtask_unavailable" in DECLARED_UNUSED_RULE_IDS
    assert "dispatch.parent_fenced" in RULE_IDS
    assert "dispatch.parent_fenced" not in DECLARED_UNUSED_RULE_IDS
    assert "dispatch.parent_fenced" in REJECTION_RULE_IDS
    assert {
        "dispatch.run_effects_disallowed",
        "dispatch.disk_space_low",
        "dispatch.disk_space_unknown",
    } <= REJECTION_RULE_IDS
    # `task.created` 的必填键也进 task 契约镜像（M2）。
    task_doc = json.loads(M2_TASK.read_text(encoding="utf-8"))
    assert "max_subtasks" in task_doc["properties"]
    assert task_doc["properties"]["max_subtasks"]["enum"] == [0, 1, 2, 3, 4]

"""C4 规则编号的封闭契约（`payload.rule_id`）。

这一组测试盯四件事：

1. **不能撒谎**：`RULE_IDS` 里每个编号都必须真能写出去，且与
   `schemas/c4/rules.json` 的 enum 完全一致（同一条测试同时校验两者，防漂移）。
2. **不能漏**：每个编号要么有真实产生路径，要么被显式标成
   `DECLARED_UNUSED_RULE_IDS`（照 C3 处理 `job.lost` 的先例：保留、标注、不删）。
3. **不能混淆**：这些是规则编号，**不是事件类型**，两个集合必须不相交。
4. **写错要炸在写入之前**：未登记的编号是代码 bug，必须在产生副作用前抛错，
   而不是先落一条看不懂的日志。
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from support import effects_admission

from tianshu.dispatcher import Dispatcher, build_request
from tianshu.events import EVENT_TYPES, EventLog
from tianshu.jobs import JobStore
from tianshu.leases import LeaseStore
from tianshu.permissions import (
    SUPPORTED_ACTIONS,
    ApprovalStore,
    GrantStore,
    PolicyEngine,
)
from tianshu.rules import (
    DECISION_RULE_IDS,
    DECLARED_UNUSED_RULE_IDS,
    REJECTION_RULE_IDS,
    RULE_GROUPS,
    RULE_IDS,
    RuleIdError,
    check_rule_id,
)
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, TaskStore
from tianshu.workspace import WorkspaceBinding

REPO = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO / "schemas" / "c4" / "rules.json"
PRD_PATH = REPO / "docs" / "PRD-v1.md"


# ---------------------------------------------------------------- 夹具


@dataclass
class Env:
    ws: Path
    log: EventLog
    leases: LeaseStore
    claims: JobStore
    tasks: TaskStore
    approvals: ApprovalStore
    grants: GrantStore
    dispatcher: Dispatcher

    def new_run(self):
        task = self.tasks.create_task("整理目录里的笔记", [{"root_path": str(self.ws)}])
        run = self.tasks.start_run(task.task_id)
        self.leases.acquire([self.ws], run.run_id)
        job = self.claims.enqueue(run.run_id)
        claim = self.claims.claim(job.job_id)
        self.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        self.dispatcher._snapshots.init_run(run.run_id)
        return task, run, claim

    def request(self, task, run, claim, *, tool_id, target, args, call_id):
        return build_request(
            task=task,
            run=run,
            claim=claim,
            call_id=call_id,
            tool_id=tool_id,
            target=target,
            args=args,
        )


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.md").write_text("原始内容\n", encoding="utf-8")
    data = tmp_path / "authority"
    data.mkdir()

    log = EventLog(tmp_path / "events.db")
    leases = LeaseStore(log)
    claims = JobStore(log)
    snapshots = SnapshotStore(tmp_path / "artifacts")
    tasks = TaskStore(log, leases=leases, claims=claims, data_root=data,
        disk_admission=effects_admission(log, snapshots=snapshots,
            memory=None, contents=None, data_root=data))
    approvals = ApprovalStore(log)
    grants = GrantStore(log)
    dispatcher = Dispatcher(
        log,
        tasks,
        leases,
        PolicyEngine(),
        approvals,
        grants=grants,
        claims=claims,
        snapshots=snapshots,
        data_root=data,
    )
    e = Env(ws, log, leases, claims, tasks, approvals, grants, dispatcher)
    try:
        yield e
    finally:
        log.close()


def _types(log: EventLog, run_id: str) -> list[str]:
    return [event.type for event in log.read_run(run_id)]


# ---------------------------------------------------------------- 1. 与 schema 绑定


def test_schema_enum_matches_rule_ids():
    """同一条测试同时校验两边：改一处忘另一处会红。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    enum = schema["properties"]["rule_id"]["enum"]
    assert set(enum) == set(RULE_IDS)
    # enum 是排序的，读 diff 时能一眼看出加了什么
    assert enum == sorted(RULE_IDS)
    # 每个编号都要有语义说明，不许只登记不解释
    assert set(schema["x-rule-descriptions"]) == set(RULE_IDS)
    # 三个附加清单也必须与代码一致
    assert set(schema["x-declared-unused"]) == set(DECLARED_UNUSED_RULE_IDS)
    assert set(schema["x-decision-rule-ids"]) == set(DECISION_RULE_IDS)


# ---------------------------------------------------------------- 2. 封闭性与可写性


def test_all_registered_ids_are_writable():
    """登记过的编号必须都能写出去——否则注册表在替不存在的路径作证。"""
    for rule_id in RULE_IDS:
        assert check_rule_id(rule_id) == rule_id


def test_unregistered_id_is_rejected_as_a_code_bug():
    for bogus in (
        "dispatch.not_a_rule",
        "c4.typo",
        "dispatch.lease_lost ",  # 尾随空格也是写错
        "DISPATCH.LEASE_LOST",  # 大小写也是写错
        "",
    ):
        with pytest.raises(RuleIdError) as excinfo:
            check_rule_id(bogus)
        message = str(excinfo.value)
        assert "代码 bug" in message
        assert repr(bogus) in message


def test_rule_ids_are_not_event_types():
    """规则编号不是事件类型：两个集合不相交，事件类型名也不许混进来。"""
    assert RULE_IDS.isdisjoint(EVENT_TYPES)
    for event_type in (
        "dispatch.rejected",
        "dispatch.requested",
        "policy.denied",
        "dispatch.admitted",
    ):
        assert event_type not in RULE_IDS
        assert event_type in EVENT_TYPES


def test_groups_partition_the_registry():
    """分组必须恰好划分全集：漏一个、重一个都算文档在骗人。"""
    members: list[str] = []
    for group in RULE_GROUPS.values():
        members.extend(group)
    assert len(members) == len(set(members)), "同一个编号出现在多个分组里"
    assert set(members) == set(RULE_IDS)


def test_rejection_and_decision_are_disjoint_and_cover():
    assert REJECTION_RULE_IDS.isdisjoint(DECISION_RULE_IDS)
    assert REJECTION_RULE_IDS.isdisjoint(DECLARED_UNUSED_RULE_IDS)
    assert DECISION_RULE_IDS.isdisjoint(DECLARED_UNUSED_RULE_IDS)
    assert REJECTION_RULE_IDS | DECISION_RULE_IDS | DECLARED_UNUSED_RULE_IDS == RULE_IDS


# ---------------------------------------------------------------- 3. 每个编号的去处


def _producer_hits() -> set[str]:
    """扫源码里以 rule_id 身份出现的字面量（含各类辅助函数的位置参数）。"""
    index = {
        "DiskAdmissionError": 0,  # (rule_id, reason)，创建前拒绝不另造 Run/事件
        "DispatchDenied": 1,  # (reason, rule_id)
        "PolicyDecision": 2,  # (kind, reason, rule_id)
        "_reject": 3,  # (request, args, reason, rule_id, *, event)
        "_reject_runtime": 2,  # (request, exc, rule_id)
        "_fail_unknown": 4,  # (request, dispatch_id, target, exc, rule_id)
    }
    consts: dict[str, str] = {}
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in sorted((REPO / "src" / "tianshu").glob("*.py"))
        if path.name != "rules.py"
    }
    for src in sources.values():
        for node in ast.parse(src).body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value

    hits: set[str] = set()

    def note(arg: ast.expr) -> None:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            hits.add(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in consts:
            hits.add(consts[arg.id])

    def note_dict_values(dict_node: ast.Dict) -> None:
        for value in dict_node.values:
            note(value)

    for src in sources.values():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                position = index.get(name)
                if position is not None and len(node.args) > position:
                    note(node.args[position])
                for keyword in node.keywords:
                    if keyword.arg == "rule_id":
                        note(keyword.value)
                # `{异常类型: "dispatch.xxx"}.get(type(exc), "dispatch.refused")`
                # ——映射表与 .get 的兜底默认值都是真实产生路径。
                if name == "get" and len(node.args) >= 2:
                    note(node.args[1])
                    if isinstance(node.func, ast.Attribute) and isinstance(
                        node.func.value, ast.Dict
                    ):
                        note_dict_values(node.func.value)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "rule_id"
                for target in node.targets
            ):
                # 固定拒绝异常的类属性也是真实 rule_id（例如 RunEffectsDisallowed）。
                note(node.value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "rule_id":
                        note(value)
            # 前置条件翻译函数的 return 也是产生路径。
            elif isinstance(node, ast.FunctionDef) and node.name == "precondition_rule_id":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return):
                        note(sub.value)
    return hits & set(RULE_IDS)


def test_every_id_has_a_producer_or_is_declared_unused():
    producers = _producer_hits()
    with_producer = set(RULE_IDS) - set(DECLARED_UNUSED_RULE_IDS)
    missing = sorted(with_producer - producers)
    assert not missing, f"这些编号登记了却没有任何产生路径：{missing}"
    # 保留未使用的两个编号，性质不同，分别断言：
    # - `dispatch.denied` 只是默认参数，连语法上的产生点都没有；
    # - `c4.unhandled` 语法上有产生点（classify 末尾兜底），但行为上够不到——
    #   那条由 test_reserved_ids_really_have_no_reachable_path 证明。
    assert "dispatch.denied" not in producers, (
        "dispatch.denied 已被用作产生路径，应移出 DECLARED_UNUSED_RULE_IDS"
    )
    unexpected = (set(DECLARED_UNUSED_RULE_IDS) - {"c4.unhandled"}) & producers
    assert not unexpected, (
        "这些编号已被用作产生路径，应从 DECLARED_UNUSED_RULE_IDS 移出："
        f"{sorted(unexpected)}"
    )


def test_reserved_ids_really_have_no_reachable_path():
    """两个保留编号各自证明一次"够不到"，而不是只写在注释里。"""
    # dispatch.denied 是 DispatchDenied 的默认参数：确认没有任何调用方省略它。
    dispatcher_src = (REPO / "src" / "tianshu" / "dispatcher.py").read_text(
        encoding="utf-8"
    )
    for node in ast.walk(ast.parse(dispatcher_src)):
        call = node
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "DispatchDenied":
            given = len(call.args) + sum(
                1 for keyword in call.keywords if keyword.arg == "rule_id"
            )
            assert given >= 2, (
                f"dispatcher.py:{call.lineno} 用了默认 rule_id——"
                "dispatch.denied 已经有产生路径了，应移出保留清单"
            )

    # c4.unhandled 是 classify() 的末尾兜底：四个受支持动作都必须被前面的分支接住。
    engine = PolicyEngine()
    bindings = [WorkspaceBinding("/ws")]
    for action in sorted(SUPPORTED_ACTIONS):
        decision = engine.classify(action, ["/ws/a.md"], bindings)
        assert decision.rule_id != "c4.unhandled", (
            f"动作 {action} 落到了 c4.unhandled 兜底——它不再是"
            "'保留未使用'，应移出保留清单并补一条真实场景测试"
        )


# ---------------------------------------------------------------- 4. 文档绑定


def _prd_section() -> str:
    text = PRD_PATH.read_text(encoding="utf-8")
    start = text.index("### 5.4.1")
    end = text.index("### 5.5 ")
    return text[start:end]


def test_prd_documents_every_id():
    """每个编号都要在 PRD §5.4.1 的表格里有自己的一行——否则用户无处可查。"""
    section = _prd_section()
    missing = sorted(
        rule_id for rule_id in RULE_IDS if f"`{rule_id}`" not in section
    )
    assert not missing, f"PRD §5.4.1 缺这些编号的说明：{missing}"
    # 表头必须有"用户该怎么做"这一列：只说"被拒了"等于没说
    assert "用户该怎么做" in section


# ---------------------------------------------------------------- 5. 写入路径校验


def test_reject_path_refuses_unregistered_id(env):
    """直接打写入入口：未登记编号必须在写任何东西之前炸掉。"""
    task, run, claim = env.new_run()
    request = env.request(
        task,
        run,
        claim,
        tool_id="modify_text",
        target=env.ws / "note.md",
        args={"path": "note.md", "content": "x"},
        call_id="c1",
    )
    before = len(env.log.read_run(run.run_id))
    with pytest.raises(RuleIdError):
        env.dispatcher._reject(
            request, {}, "测试", "dispatch.bogus", event="dispatch.rejected"
        )
    assert len(env.log.read_run(run.run_id)) == before, (
        "校验必须在写事件之前，一个事件都不该多出来"
    )


def test_prepare_refuses_unregistered_id_from_policy(env):
    """策略返回了未登记编号——同样要炸在写入之前。"""

    class BogusPolicy:
        def classify(self, *args, **kwargs):
            class Decision:
                kind = "deny"
                is_denied = True
                is_auto = False
                reason = "假策略"
                rule_id = "c4.bogus_rule"

            return Decision()

    task, run, claim = env.new_run()
    env.dispatcher._policy = BogusPolicy()
    args = {"name": "report.md", "content": "内容"}
    request = env.request(
        task,
        run,
        claim,
        tool_id="create_report",
        target=env.ws / "report.md",
        args=args,
        call_id="c2",
    )
    before = len(env.log.read_run(run.run_id))
    with pytest.raises(RuleIdError):
        env.dispatcher.prepare(request, claim, args=args)
    assert len(env.log.read_run(run.run_id)) == before


def test_real_rejection_records_a_registered_rule_id(env):
    """真实场景：把目标指向目录 → 事件里记的 rule_id 必须在注册表内。"""
    task, run, claim = env.new_run()
    request = env.request(
        task,
        run,
        claim,
        tool_id="modify_text",
        target=env.ws,
        args={"path": ".", "content": "x"},
        call_id="c3",
    )
    decision = env.dispatcher.prepare(
        request, claim, args={"path": ".", "content": "x"}
    )
    assert decision.is_rejected, decision

    rejected = [
        event
        for event in env.log.read_run(run.run_id)
        if event.type == "dispatch.rejected"
    ]
    assert rejected, "应当留下一条 dispatch.rejected"
    recorded = rejected[-1].payload["rule_id"]
    assert recorded in RULE_IDS
    assert recorded == "dispatch.target_is_directory"
    assert recorded in REJECTION_RULE_IDS


def test_decision_reasons_are_registered_too(env):
    """放行/需审批的编号也会写进 payload，不能只校验拒绝那一半。"""
    task, run, claim = env.new_run()
    args = {"name": "report.md", "content": "内容"}
    request = env.request(
        task,
        run,
        claim,
        tool_id="create_report",
        target=env.ws / "report.md",
        args=args,
        call_id="c4",
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.rule_id in RULE_IDS
    assert decision.rule_id in DECISION_RULE_IDS
    requested = [
        event
        for event in env.log.read_run(run.run_id)
        if event.type == "dispatch.requested"
    ]
    assert requested and requested[-1].payload["rule_id"] in RULE_IDS


def test_policy_decisions_stay_inside_the_registry():
    """策略引擎给出的每个判定编号都必须在注册表内（含需审批那两条）。"""
    engine = PolicyEngine()
    bindings = [WorkspaceBinding("/ws")]
    for action in sorted(SUPPORTED_ACTIONS):
        decision = engine.classify(action, ["/ws/a.md"], bindings)
        assert decision.rule_id in RULE_IDS, decision.rule_id

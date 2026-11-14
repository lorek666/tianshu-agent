"""issue #17 / PR G：证据键补 workspace 根，undo 不再跨目录写错。

三件必须成立的事，每件都有一个"看起来差不多、其实已经破了"的反例：

1. **证据键 = (workspace 根, 相对路径)**。一个 Run 可以绑多个 workspace，
   两个 workspace 下的 `a.md` 是两条证据，不是一条。
2. **undo 只写证据指向的目录**。撤销 wsA 的变更时，wsB 下的同名文件一个
   字节都不许动；旧格式（清单里没有 root）必须**拒绝**，不许拿传入的
   workspace 去猜——写错目录比撤销失败严重得多。
3. **凭据能独立核对**。`verify_receipts` 要真的读盘：磁盘文件被改坏、写前
   blob 被改坏都必须报出来；旧格式凭据必须报"核对不了"，不能默认通过。

两个后端各跑一遍（`local_fs` / `memfs`）：新逻辑不能只在一个后端上成立。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from support import effects_admission

from tianshu import sandbox
from tianshu.cli import main
from tianshu.dispatcher import Dispatcher, build_request
from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.guard import blocked_workspaces, ensure_not_blocked
from tianshu.jobs import JobStore
from tianshu.leases import LeaseStore
from tianshu.permissions import (
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyEngine,
)
from tianshu.receipt import receipts_of, verify_receipts
from tianshu.recovery import recover_run
from tianshu.sandbox import MemBackend
from tianshu.snapshot import MANIFEST_VERSION, SnapshotStore
from tianshu.tasks import RUN_RUNNING, TaskStore
from tianshu.workspace import Workspace

BACKENDS = ["local_fs", "memfs"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 夹具


@dataclass
class Env:
    tmp: Path
    backend: Any
    roots: list[Path]
    data: Path
    log: EventLog
    leases: LeaseStore
    claims: JobStore
    tasks: TaskStore
    approvals: ApprovalStore
    grants: GrantStore
    snapshots: SnapshotStore
    dispatcher: Dispatcher

    # ---- 文件：一律走当前后端，memfs 下一个字节都不会落到宿主盘 ----
    def path(self, index: int, rel: str) -> Path:
        return Workspace(self.roots[index]).resolve(rel)

    def write(self, index: int, rel: str, text: str) -> None:
        self.backend.write_bytes_atomic(self.path(index, rel), text.encode("utf-8"))

    def read(self, index: int, rel: str) -> bytes:
        return self.backend.read_bytes(self.path(index, rel))[0]

    def exists(self, index: int, rel: str) -> bool:
        return self.backend.exists(self.path(index, rel))

    def guard(self) -> None:
        for root in self.roots:
            ensure_not_blocked(self.log, root)

    @property
    def db(self) -> Path:
        return self.tmp / "events.db"


@pytest.fixture(params=BACKENDS)
def env(request, tmp_path):
    if request.param == "local_fs":
        backend = sandbox.configure_backend("local_fs")
        base = tmp_path / "ws"
        base.mkdir()
        roots = []
        for name in ("a", "b"):
            (base / name).mkdir()
            roots.append((base / name).resolve())
    else:
        backend = sandbox.configure_backend(MemBackend(namespace=tmp_path / "mem"))
        base = (tmp_path / "mem" / "ws").resolve()
        roots = [(base / name).resolve() for name in ("a", "b")]
        for root in roots:
            backend.require_root(root)

    data = tmp_path / "authority"
    data.mkdir()
    log = EventLog(tmp_path / "events.db")
    leases = LeaseStore(log)
    claims = JobStore(log)
    snapshots = SnapshotStore(data / "artifacts")
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
    built = Env(
        tmp=tmp_path,
        backend=backend,
        roots=roots,
        data=data,
        log=log,
        leases=leases,
        claims=claims,
        tasks=tasks,
        approvals=approvals,
        grants=grants,
        snapshots=snapshots,
        dispatcher=dispatcher,
    )
    try:
        yield built
    finally:
        log.close()


def _new_run(env: Env):
    """一个绑两个 workspace 的 Run。这才是"同名文件会撞键"的前提。"""
    task = env.tasks.create_task(
        "两个目录一起改", [{"root_path": str(r)} for r in env.roots]
    )
    run = env.tasks.start_run(task.task_id)
    env.leases.acquire(list(env.roots), run.run_id)
    job = env.claims.enqueue(run.run_id)
    claim = env.claims.claim(job.job_id)
    env.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
    env.snapshots.init_run(run.run_id)
    return task, run, claim


def _modify(
    env: Env, task, run, claim, index: int, content: str, call_id: str
) -> Any:
    """经真实派发路径改 `roots[index]/a.md`。

    目标是**绝对路径**：这是多 workspace 任务里唯一能指明"改哪一个 a.md"
    的方式（相对路径会被解析到第一个可写绑定）。
    """
    target = env.path(index, "a.md")
    args = {"path": "a.md", "content": content}
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=call_id,
        tool_id="modify_text",
        target=target,
        args=args,
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    if decision.is_waiting:  # 覆盖写要人批：这里替人按下批准
        env.approvals.decide(
            decision.approval_id,
            decision="approved",
            actor=HumanIdentity.local_cli(),
        )
    return env.dispatcher.dispatch(decision.request_id, claim)


def _undo(env: Env, run_id: str, *, scope: Path | None = None):
    return env.snapshots.undo(
        run_id,
        Workspace(scope) if scope is not None else None,
        guard=env.guard,
        allow_direct_write=True,
    )


def _write_legacy_manifest(store: SnapshotStore, run_id: str, entries: list[dict]) -> None:
    """手工造一份 v2 清单：只有相对路径，没有 workspace 根（旧格式）。"""
    (store.manifest_dir / f"{run_id}.json").write_text(
        json.dumps({"version": 2, "changes": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _legacy_entry(store: SnapshotStore, rel: str, before: str, after: str) -> dict:
    digest = _sha(before)
    blob = store.blob_dir / digest
    blob.write_bytes(before.encode("utf-8"))
    return {
        "call_id": "c_legacy",
        "path": rel,
        "before_state": digest,
        "expected_after": _sha(after),
        "blob_ref": str(blob),
    }


# ---------------------------------------------------------------- 1. 证据键


def test_manifest_records_a_root_per_change(env):
    """两个 workspace 下的同名文件必须落成两条带根的独立证据。"""
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)
    assert _modify(env, task, run, claim, 0, "A改", "cA").effect_state == "applied"
    assert _modify(env, task, run, claim, 1, "B改", "cB").effect_state == "applied"

    changes = env.snapshots.load(run.run_id)
    assert [(Path(c.root), c.path) for c in changes] == [
        (env.roots[0], "a.md"),
        (env.roots[1], "a.md"),
    ], "相对路径相同，但根不同：这是两条证据"
    assert {c.before_state for c in changes} == {_sha("A原"), _sha("B原")}, \
        "每条证据的写前内容必须是各自那个目录的"

    raw = json.loads(
        env.snapshots.manifest_path(run.run_id).read_text(encoding="utf-8")
    )
    assert raw["version"] == MANIFEST_VERSION == 3
    assert [Path(c["root"]) for c in raw["changes"]] == env.roots


def test_same_relative_path_in_one_root_is_still_one_key(env):
    """同一目录下的同名路径仍然按 call_id 各自成条（不能因为加了根就合并）。"""
    env.write(0, "a.md", "v0")
    task, run, claim = _new_run(env)
    assert _modify(env, task, run, claim, 0, "v1", "c1").effect_state == "applied"
    assert _modify(env, task, run, claim, 0, "v2", "c2").effect_state == "applied"

    changes = env.snapshots.load(run.run_id)
    assert [c.call_id for c in changes] == ["c1", "c2"]
    assert {c.key for c in changes} == {(str(env.roots[0]), "a.md")}


# ---------------------------------------------------------------- 2. undo 定向


def test_undo_one_workspace_leaves_the_other_untouched(env):
    """核心回归：wsA/a.md 与 wsB/a.md 各改一次，撤销 wsA 时 wsB 一个字节都不动。"""
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)
    assert _modify(env, task, run, claim, 0, "A改", "cA").effect_state == "applied"
    assert _modify(env, task, run, claim, 1, "B改", "cB").effect_state == "applied"

    other_before = env.read(1, "a.md")
    assert other_before == "B改".encode()

    report = _undo(env, run.run_id, scope=env.roots[0])

    assert report.ok, report.problems
    assert report.restored == ["a.md"]
    assert [(Path(e["root"]), e["path"]) for e in report.entries] == [
        (env.roots[0], "a.md")
    ]
    assert env.read(0, "a.md") == "A原".encode(), "wsA 必须还原"
    assert env.read(1, "a.md") == other_before, "wsB 下的同名文件不许被碰"


def test_undo_without_scope_restores_each_workspace_into_its_own_root(env):
    """不给范围时撤全部，但每条仍然写回**它自己的**根，不写进第一个根。"""
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)
    _modify(env, task, run, claim, 0, "A改", "cA")
    _modify(env, task, run, claim, 1, "B改", "cB")

    report = _undo(env, run.run_id)

    assert report.ok, report.problems
    assert {(Path(e["root"]), e["path"]) for e in report.entries} == {
        (env.roots[0], "a.md"),
        (env.roots[1], "a.md"),
    }
    assert env.read(0, "a.md") == "A原".encode()
    assert env.read(1, "a.md") == "B原".encode()


def test_conflict_in_one_workspace_blocks_the_whole_undo(env):
    """任一文件冲突就整体不执行——跨 workspace 也一样，不许撤一半。"""
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)
    _modify(env, task, run, claim, 0, "A改", "cA")
    _modify(env, task, run, claim, 1, "B改", "cB")

    env.write(1, "a.md", "用户在 wsB 手写的内容")  # 冲突在第二个目录

    report = _undo(env, run.run_id)

    assert not report.ok
    assert report.conflicts, "必须报告冲突"
    assert str(env.path(1, "a.md")) in report.conflicts[0]
    assert env.read(0, "a.md") == "A改".encode(), "有冲突时 wsA 也不能被还原"
    assert env.read(1, "a.md") == "用户在 wsB 手写的内容".encode()


def test_undo_scope_without_changes_refuses_without_writing(env):
    """范围里没有变更就明确拒绝；不能"顺便"把别的目录也撤了。"""
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)
    _modify(env, task, run, claim, 0, "A改", "cA")

    report = _undo(env, run.run_id, scope=env.roots[1])

    assert not report.ok
    assert any("没有可撤销的变更" in p for p in report.problems)
    assert env.read(0, "a.md") == "A改".encode(), "范围不符时不许动任何文件"


# ---------------------------------------------------------------- 3. 旧格式：拒绝优于猜测


def test_legacy_manifest_without_root_is_refused(env):
    """v2 清单（没有 root）一律拒绝：不知道写的是哪个目录，就不许写。"""
    env.write(0, "a.md", "A原")
    entry = _legacy_entry(env.snapshots, "a.md", "A原", "A改")
    _write_legacy_manifest(env.snapshots, "run_legacy", [entry])
    env.write(0, "a.md", "A改")

    report = _undo(env, "run_legacy", scope=env.roots[0])

    assert not report.ok
    assert any("旧格式" in p for p in report.problems), report.problems
    assert env.read(0, "a.md") == "A改".encode(), "拒绝时一个字节都不能动"


def test_legacy_manifest_is_refused_even_without_scope(env):
    """不给 --workspace 时同样拒绝——不猜"大概就是那个目录"。"""
    env.write(0, "a.md", "A原")
    entry = _legacy_entry(env.snapshots, "a.md", "A原", "A改")
    _write_legacy_manifest(env.snapshots, "run_legacy", [entry])
    env.write(0, "a.md", "A改")

    report = _undo(env, "run_legacy")

    assert not report.ok
    assert any("旧格式" in p for p in report.problems)
    assert env.read(0, "a.md") == "A改".encode()


def test_cli_refuses_legacy_manifest_instead_of_guessing(env, capsys):
    """CLI 也不猜：退出码 2，并说清"为什么拒绝、可以怎么处置"。"""
    env.write(0, "a.md", "A原")
    entry = _legacy_entry(env.snapshots, "a.md", "A原", "A改")
    _write_legacy_manifest(env.snapshots, "run_legacy", [entry])
    env.write(0, "a.md", "A改")

    rc = main([
        "undo", "--run-id", "run_legacy",
        # 故意指向**另一个** workspace：旧实现会照着它去写，正好写错目录
        "--workspace", str(env.roots[1]),
        "--db", str(env.db), "--data-root", str(env.data), "--approve",
    ])

    captured = capsys.readouterr()
    assert rc == 2, captured.out + captured.err
    assert "旧格式" in captured.err
    assert "不猜" in captured.err
    # 传进来的 --workspace 必须被明确忽略（不是悄悄拿来用）
    assert str(env.roots[1]) in captured.err, "要说清可以按目录逐个 recover 核对"
    assert "已忽略 --workspace" in captured.err
    assert env.read(0, "a.md") == "A改".encode()
    assert not env.exists(1, "a.md"), "更不许在传进来的 workspace 里造文件"


def test_future_manifest_version_is_refused_not_misread(env):
    """版本更高的清单不按旧语义解释（字段可能是另一种意思）。"""
    path = env.snapshots.manifest_dir / "run_future.json"
    path.write_text(
        json.dumps({"version": MANIFEST_VERSION + 1, "changes": []}),
        encoding="utf-8",
    )
    with pytest.raises(TianshuError):
        env.snapshots.load("run_future")


def test_record_preimage_requires_a_root(tmp_path):
    """写入证据没有根就直接炸：不许生成"不知道写在哪个目录"的证据。"""
    store = SnapshotStore(tmp_path / "artifacts")
    with pytest.raises(TypeError):
        store.record_preimage("r1", "c1", "a.md", None, _sha("x"))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "rel",
    ["../a.md", "/etc/passwd", "a\\b.md", "a/../../b.md", ""],
)
def test_record_preimage_refuses_unsafe_relative_paths(tmp_path, rel):
    """manifest 是撤销的落盘依据：里面不许存能指到根外面的路径。"""
    store = SnapshotStore(tmp_path / "artifacts")
    with pytest.raises(TianshuError):
        store.record_preimage(
            "r1", "c1", rel, None, _sha("x"), root=tmp_path / "ws"
        )


def test_undo_refuses_when_evidence_root_is_gone(tmp_path):
    """证据里的根不见了：拒绝，而不是换个目录写。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    log = EventLog(tmp_path / "e.db")
    store = SnapshotStore(tmp_path / "artifacts")
    store.record_preimage(
        "r1", "c1", "a.md", "原".encode(), _sha("新"), root=ws
    )
    (ws / "a.md").write_text("新", encoding="utf-8")
    ws.rename(tmp_path / "ws_moved")  # 目录被用户搬走

    report = store.undo(
        "r1", guard=lambda: None, allow_direct_write=True
    )

    assert not report.ok
    assert any("不可用" in p for p in report.problems), report.problems
    assert (tmp_path / "ws_moved" / "a.md").read_text(encoding="utf-8") == "新"
    log.close()


# ---------------------------------------------------------------- 4. 凭据核对磁盘


def test_verify_receipts_passes_for_a_clean_run(env):
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)
    _modify(env, task, run, claim, 0, "A改", "cA")
    _modify(env, task, run, claim, 1, "B改", "cB")

    receipts = receipts_of(env.log, run.run_id)
    assert len(receipts) == 2
    assert [Path(r["root"]) for r in receipts] == env.roots
    assert [r["action"] for r in receipts] == ["overwrite", "overwrite"]
    assert verify_receipts(env.log, run.run_id) == []


def test_verify_receipts_detects_a_file_edited_behind_its_back(env):
    """改坏磁盘上的文件后，verify 必须发现（旧实现只看日志，发现不了）。"""
    env.write(0, "a.md", "A原")
    task, run, claim = _new_run(env)
    _modify(env, task, run, claim, 0, "A改", "cA")
    assert verify_receipts(env.log, run.run_id) == []

    env.write(0, "a.md", "有人在 Agent 写完后又改了")

    problems = verify_receipts(env.log, run.run_id)
    assert problems, "磁盘被改动必须被校验出来"
    assert any("与磁盘不一致" in p for p in problems), problems
    assert str(env.path(0, "a.md")) in problems[0]


def test_verify_receipts_detects_a_corrupted_snapshot_blob(env):
    """写前 blob 被改坏 → 证据链断了，要报出来（磁盘本身还对）。"""
    env.write(0, "a.md", "A原")
    task, run, claim = _new_run(env)
    _modify(env, task, run, claim, 0, "A改", "cA")

    change = env.snapshots.load(run.run_id)[0]
    assert change.blob_ref
    Path(change.blob_ref).write_bytes(b"tampered")

    problems = verify_receipts(env.log, run.run_id)
    assert any("blob" in p for p in problems), problems


def test_verify_receipts_attributes_tampering_to_the_right_workspace(env):
    """两条同名证据必须各认各的目录：只报被改的那个 workspace。"""
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)
    _modify(env, task, run, claim, 0, "A改", "cA")
    _modify(env, task, run, claim, 1, "B改", "cB")
    assert verify_receipts(env.log, run.run_id) == []

    env.write(1, "a.md", "wsB 被人改了")

    problems = verify_receipts(env.log, run.run_id)
    assert len(problems) == 1, problems
    assert str(env.path(1, "a.md")) in problems[0]
    assert str(env.path(0, "a.md")) not in problems[0], "wsA 没被改，不该被牵连"


def test_verify_receipts_flags_legacy_receipts_as_unverifiable(env):
    """旧格式凭据（没有 root）要明确报"核对不了"，不能默认通过。"""
    env.write(0, "a.md", "A原")
    task, run, claim = _new_run(env)
    _modify(env, task, run, claim, 0, "A改", "cA")

    env.log.append(
        run.run_id,
        "tool.result",
        {
            "call_id": "c_old",
            "tool_id": "create_report",
            "status": "succeeded",
            "effect_state": "applied",
            "output": {"path": "old.md", "sha256": _sha("旧产物")},
        },
        actor_kind="tool",
        idem_key="c_old",
    )
    env.log.append(
        run.run_id,
        "receipt",
        {
            "receipt_id": "rcpt_legacy",
            "run_id": run.run_id,
            "call_id": "c_old",
            "tool_id": "create_report",
            "action": "create_report",  # 旧实现拿 tool_id 当动作
            "args_hash": "x",
            "resource": "old.md",  # 只有相对路径
            "effect_state": "applied",
            "before_state": "absent",
            "after_state": _sha("旧产物"),
            "backend": env.backend.name,
            "evidence": ["tool.result:succeeded"],  # 合成串
            "issued_at": 0,
            "issuer": "tianshu/0.1",
        },
        actor_kind="core",
        idem_key="rcpt_legacy",
        legacy_write=True,
    )

    problems = verify_receipts(env.log, run.run_id)
    assert any("旧格式" in p and "rcpt_legacy" in p for p in problems), problems


def test_a_database_with_old_style_events_reopens(tmp_path):
    """老库升级：旧形状的事件与凭据原样可读，但不会被当成"已核对"。

    本 PR **没有改任何数据库 schema**（不加列、不加索引），所以旧库不需要迁移；
    需要证明的是"读了旧记录之后，新语义不会把它误当成可信证据"。M2 验收 §5
    第 18 条记的是构造 `JobStore` 时索引迁移炸掉那类问题——这里顺带把"旧库能
    直接重开并装配整套权威存储"钉成回归。
    """
    from tianshu.loop import build_stack

    db = tmp_path / "events.db"
    log = EventLog(db)
    log.append(
        "run_old",
        "tool.result",
        {
            "call_id": "c_old",
            "tool_id": "create_report",
            "status": "succeeded",
            "effect_state": "applied",
            "output": {"path": "old.md", "sha256": _sha("旧产物")},
        },
        actor_kind="tool",
        idem_key="c_old",
    )
    log.append(
        "run_old",
        "receipt",
        {
            "receipt_id": "rcpt_old",
            "run_id": "run_old",
            "call_id": "c_old",
            "tool_id": "create_report",
            "action": "create_report",
            "args_hash": "x",
            "resource": "old.md",
            "effect_state": "applied",
            "before_state": "absent",
            "after_state": _sha("旧产物"),
            "backend": "local_fs",
            "evidence": ["tool.result:succeeded"],
            "issued_at": 0,
            "issuer": "tianshu/0.1",
        },
        actor_kind="core",
        idem_key="rcpt_old",
        legacy_write=True,
    )
    log.close()

    reopened = EventLog(db)
    stack = build_stack(reopened, data_root=tmp_path / "authority")
    try:
        assert [e.type for e in reopened.read_run("run_old")] == [
            "tool.result",
            "receipt",
        ]
        assert receipts_of(reopened, "run_old")[0]["action"] == "create_report"
        problems = verify_receipts(reopened, "run_old")
        assert any("旧格式" in p and "rcpt_old" in p for p in problems), problems
        # 旧清单（v2）也能读，但 root 是空的——撤销会拒绝（见上文用例）
        _write_legacy_manifest(
            stack.snapshots, "run_old", [_legacy_entry(stack.snapshots, "old.md", "旧", "旧产物")]
        )
        assert [c.root for c in stack.snapshots.load("run_old")] == [None]
    finally:
        reopened.close()


# ---------------------------------------------------------------- 5. 恢复也按证据的根看盘


def _intent(env: Env, run_id: str, call_id: str, rel: str = "a.md") -> None:
    env.log.append(
        run_id,
        "tool.intent",
        {
            "call_id": call_id,
            "tool_id": "modify_text",
            "args": {"path": rel, "content": "x"},
            "args_hash": _sha("args"),
        },
        actor_kind="core",
        legacy_write=True,
    )


def test_recovery_uses_the_root_recorded_in_the_evidence(env):
    """恢复只看证据指向的目录：调用方传进来的那个目录不能替它做主。"""
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)

    # 手工造一个"崩在写入之后"的窗口：intent 在、result 与凭据不在
    _intent(env, run.run_id, "cB")
    env.snapshots.record_preimage(
        run.run_id, "cB", "a.md", "B原".encode(), _sha("B改"), root=env.roots[1]
    )
    env.write(1, "a.md", "B改")

    # 故意把**另一个** workspace 当 --workspace 传进去
    report = recover_run(env.log, run.run_id, env.roots[0], env.snapshots)

    assert report.applied == ["cB"], report.problems
    receipts = receipts_of(env.log, run.run_id)
    assert len(receipts) == 1
    assert Path(receipts[0]["root"]) == env.roots[1]
    assert Path(receipts[0]["resource"]) == env.path(1, "a.md")
    assert verify_receipts(env.log, run.run_id) == []


def test_recovery_blocks_the_workspace_that_is_actually_unresolved(env):
    """阻断要落在状态不明的那个目录上，别把另一个干净目录一起挡了。"""
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)

    _intent(env, run.run_id, "cB")
    env.snapshots.record_preimage(
        run.run_id, "cB", "a.md", "B原".encode(), _sha("B改"), root=env.roots[1]
    )
    env.write(1, "a.md", "第三种内容")  # 既非写前也非写后

    report = recover_run(env.log, run.run_id, env.roots[0], env.snapshots)

    assert report.unresolved == ["cB"]
    assert [Path(r) for r in report.blocked_workspaces] == [env.roots[1]]
    blocked = blocked_workspaces(env.log)
    assert str(env.roots[1]) in blocked
    assert str(env.roots[0]) not in blocked, "wsA 没被碰过，不该被阻断"


# ---------------------------------------------------------------- 6. CLI 撤销端到端


def test_cli_undo_is_scoped_by_workspace_and_stays_verifiable(env):
    """走完整 CLI 撤销路径：只撤指定 workspace，且撤销之后凭据仍然对得上。"""
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)
    _modify(env, task, run, claim, 0, "A改", "cA")
    _modify(env, task, run, claim, 1, "B改", "cB")
    env.leases.release_run(run.run_id)  # 撤销会开一个新的控制 Run 去拿租约

    rc = main([
        "undo", "--run-id", run.run_id, "--workspace", str(env.roots[0]),
        "--db", str(env.db), "--data-root", str(env.data), "--approve",
    ])

    assert rc == 0
    assert env.read(0, "a.md") == "A原".encode()
    assert env.read(1, "a.md") == "B改".encode(), "没被选中的 workspace 不动"

    undo_events = [e for e in env.log.read_run(run.run_id)
                   if e.type == "undo.completed"]
    assert undo_events, "撤销必须留痕"
    entries = undo_events[-1].payload["entries"]
    assert [(Path(e["root"]), e["path"]) for e in entries] == [
        (env.roots[0], "a.md")
    ]

    # 撤销之后：wsA 那条凭据的写后状态已经不成立，但日志记载了这次撤销，
    # 所以 verify 不该永久报警（否则"与磁盘不一致"会变成没人看的噪音）。
    assert verify_receipts(env.log, run.run_id) == []


def test_cli_undo_without_workspace_writes_each_root_back_to_itself(env):
    """不给 --workspace 时撤全部，但每个文件仍然写回它自己的目录。"""
    env.write(0, "a.md", "A原")
    env.write(1, "a.md", "B原")
    task, run, claim = _new_run(env)
    _modify(env, task, run, claim, 0, "A改", "cA")
    _modify(env, task, run, claim, 1, "B改", "cB")
    env.leases.release_run(run.run_id)

    rc = main([
        "undo", "--run-id", run.run_id,
        "--db", str(env.db), "--data-root", str(env.data), "--approve",
    ])

    assert rc == 0
    assert env.read(0, "a.md") == "A原".encode()
    assert env.read(1, "a.md") == "B原".encode()

    entries = [
        e for e in env.log.read_run(run.run_id) if e.type == "undo.completed"
    ][-1].payload["entries"]
    assert {(Path(item["root"]), item["path"]) for item in entries} == {
        (env.roots[0], "a.md"),
        (env.roots[1], "a.md"),
    }
    assert verify_receipts(env.log, run.run_id) == []

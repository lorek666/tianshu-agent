"""工具注册表、受限工具实现与受信任的内置文件执行器。

M0 白名单只有三个（docs/M0-scope.md §4）。M2 增加 `delete_file`，并新增一个
**不对模型暴露**的 `restore_file`（undo 还原写前内容用）。任何额外写入路径都是 bug。

两条硬规则：

1. 参数校验在执行侧做，不信任模型给出的参数。
2. **有副作用的工具必须有已核验的派发票据（`AdmittedCall`）才能写。** 直接调
   `execute()` 只有显式的遗留开关才能走通，那唯一一处开关在 `loop.run()`，
   是 M0 路径，Task 5 会删掉。

`FileExecutor` 是"实际打开文件"的唯一入口。它刻意**不接受**任何形式的
"调用方说已经批准了"——它只接受派发票据里的身份，然后自己回权威库核对一遍。
票据能被伪造（dataclass 都能构造），但伪造的票据核不过库。
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import OverwriteRefused, RunFenced, TianshuError, ToolArgumentError, ToolNotAllowed
from .fsutil import atomic_write_bytes
from .guard import ensure_not_blocked
from .jobs import JobClaim
from .permissions import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_OVERWRITE,
    ACTION_READ,
    APPROVAL_ACTIONS,
    ActionRequest,
)
from .snapshot import ABSENT, SnapshotStore
from .tasks import ExecutionPermit
from .workspace import ResourceIdentity, Workspace, normalize_name

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from .leases import LeaseStore
    from .permissions import ApprovalStore, GrantStore
    from .tasks import TaskStore

READ_TEXT_LIMIT = 64 * 1024  # 单次读取上限，超出截断并标记


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def observe_state(target: Path) -> str:
    """目标当前的写前状态：不存在返回 ABSENT，否则返回 sha256。

    审批绑定的是"执行前状态"，所以这个函数既用于申请审批时取值，也用于
    真正写文件前复核——两次取值必须相同，否则说明目标被人换过了。
    """
    if not target.exists():
        return ABSENT
    return _sha(target.read_bytes())


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    version: str
    description: str
    input_schema: dict[str, Any]
    side_effect_level: str  # pure | idempotent | non_idempotent
    exec_model: str = "E1"  # E1 | E3
    action: str = ACTION_READ  # C4 动作分类
    exposed: bool = True  # 是否出现在给模型看的工具清单里


@dataclass(frozen=True)
class AdmittedCall:
    """一次已获授权、单次使用的执行票据。

    刻意**没有** `approved: bool` 之类的字段。执行器不接受"调用方说批过了"，
    只接受这里的身份（run/call/摘要/grant/审批），然后自己去权威库核对。
    票据是可伪造的；库里的记录不是。
    """

    dispatch_id: str
    request: ActionRequest
    request_digest: str
    claim: JobClaim
    tool_id: str
    action: str
    args: dict[str, Any]
    target: str  # 已解析、已核验的绝对路径
    rel_path: str  # 相对 workspace 根的路径（证据键）
    workspace_root: str
    resource_key: str
    resources: tuple[str, ...]
    intent_seq: int
    attempt_id: int = 1
    approval_id: str | None = None
    grant_id: str = ""


@dataclass(frozen=True)
class DispatchRecord:
    """派发登记行的只读投影。执行器用它判断"这次调用真的被派发过吗"。"""

    dispatch_id: str
    run_id: str
    task_id: str
    call_id: str
    request_id: str
    request_digest: str
    tool_id: str
    action: str
    state: str  # intent | executing | done | rejected | revoked
    claim_epoch: int
    owner_instance_id: str
    attempt_id: int = 1
    approval_id: str | None = None
    grant_id: str = ""


@dataclass
class ToolContext:
    """执行期上下文。快照、日志与 run_id 由主循环注入。

    `log` 不是可选的：有副作用的工具必须能检查阻断，否则阻断可以被绕过。
    `admitted` 非空时，工具只能写票据里那个已核验的目标。
    """

    snapshots: SnapshotStore | None = None
    log: object | None = None
    run_id: str = ""
    call_id: str = ""
    admitted: AdmittedCall | None = None
    allow_legacy_unauthenticated_write: bool = False

    def prewrite(self, rel_path: str, before: bytes | None, expected_after: str) -> None:
        """写入前的证据落盘。必须在真正写文件之前调用。"""
        if self.snapshots is not None:
            self.snapshots.record_preimage(
                self.run_id, self.call_id, rel_path, before, expected_after
            )


@dataclass
class ToolResult:
    status: str  # succeeded | failed | rejected | cancelled | unknown
    effect_state: str  # none_applied | applied | partial | unknown
    output: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


# ---------------------------------------------------------------- schema 校验


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True


def validate_args(args: Any, schema: dict[str, Any]) -> dict[str, Any]:
    """最小 JSON Schema 子集：type / required / properties / additionalProperties。

    够用即可；M0 不引入 jsonschema 依赖，避免四个目标架构的轮子问题。
    """
    if not isinstance(args, dict):
        raise ToolArgumentError(f"参数必须是对象，实际是 {type(args).__name__}")

    props: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])

    missing = [k for k in required if k not in args]
    if missing:
        raise ToolArgumentError(f"缺少必填参数：{missing}")

    if schema.get("additionalProperties") is False:
        extra = [k for k in args if k not in props]
        if extra:
            raise ToolArgumentError(f"不允许的参数：{extra}")

    for key, value in args.items():
        spec = props.get(key)
        if spec and "type" in spec and not _type_ok(value, spec["type"]):
            raise ToolArgumentError(
                f"参数 {key} 期望 {spec['type']}，实际 {type(value).__name__}"
            )
    return args


# ---------------------------------------------------------------- 目标解析


def _write_target(ws: Workspace, ctx: ToolContext, raw: Any) -> tuple[Path, str]:
    """把参数里的路径解析成"真正要写的文件"。

    有派发票据时**只信任票据里的目标**：参数必须解析到同一个路径，否则说明
    参数在派发之后被改过（或者票据和参数本来就不同源），一律拒绝。
    没有票据的遗留路径才按参数自行解析。
    """
    if not isinstance(raw, str) or not raw:
        raise ToolArgumentError("目标路径必须是非空字符串")

    resolved = ws.resolve(raw)
    admitted = ctx.admitted
    if admitted is None:
        return resolved, ws.relative(resolved)

    target = Path(admitted.target)
    if resolved != target:
        raise ToolNotAllowed(
            f"参数目标与已核验目标不一致（参数：{resolved}；票据：{target}）"
        )
    return target, admitted.rel_path


# ---------------------------------------------------------------- 具体实现


def _list_files(ws: Workspace, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    sub = args.get("path", ".")
    target = ws.resolve(sub)
    if not target.is_dir():
        return ToolResult("failed", "none_applied", {"error": f"不是目录：{sub}"})

    entries = []
    for p in sorted(target.iterdir(), key=lambda x: x.name):
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": normalize_name(p.name),
                "is_dir": p.is_dir(),
                "size": st.st_size,
            }
        )
    return ToolResult("succeeded", "none_applied", {"entries": entries})


def _read_text(ws: Workspace, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    target = ws.resolve(args["path"])
    if not target.is_file():
        return ToolResult("failed", "none_applied", {"error": "不是文件"})

    total = target.stat().st_size
    with target.open("rb") as fh:
        raw = fh.read(READ_TEXT_LIMIT)
    truncated = total > len(raw)

    # 按字节截断可能切断多字节字符：回退最多 3 字节以落在码点边界上
    for back in range(4):
        try:
            text = raw[:len(raw) - back].decode("utf-8") if back else raw.decode("utf-8")
            break
        except UnicodeDecodeError:
            continue
    else:
        return ToolResult(
            "failed", "none_applied", {"error": "不是 UTF-8 文本，拒绝读取"}
        )

    return ToolResult(
        "succeeded",
        "none_applied",
        {
            "path": ws.relative(target),
            "content": text,
            "bytes": len(raw),
            "total_bytes": total,
            "truncated": truncated,
        },
    )


def _create_report(ws: Workspace, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    target, rel = _write_target(ws, ctx, args["name"])

    # 验收 A6：已存在必须拒绝，不留覆盖的可能
    if target.exists():
        raise OverwriteRefused(f"目标已存在，拒绝覆盖：{args['name']}")

    data = args["content"].encode("utf-8")
    after = _sha(data)

    # 先落证据，再写文件。顺序反了就等于没证据——崩溃窗口里无从核实。
    ctx.prewrite(rel, None, after)
    atomic_write_bytes(target, data)

    return ToolResult(
        "succeeded",
        "applied",
        {
            "path": rel,
            "bytes": len(data),
            "sha256": after,
            "after_state": after,
            "before_state": ABSENT,
        },
    )


def _modify_text(ws: Workspace, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """修改已有文本文件。新建请用 create_report。"""
    target, rel = _write_target(ws, ctx, args["path"])
    if not target.exists():
        return ToolResult(
            "failed", "none_applied",
            {"error": "文件不存在；新建请用 create_report"},
        )
    if not target.is_file():
        return ToolResult("failed", "none_applied", {"error": "不是文件"})

    before = target.read_bytes()
    data = args["content"].encode("utf-8")
    after = _sha(data)

    ctx.prewrite(rel, before, after)
    atomic_write_bytes(target, data)

    return ToolResult(
        "succeeded",
        "applied",
        {
            "path": rel,
            "bytes": len(data),
            "sha256": after,
            "after_state": after,
            "before_state": _sha(before),
        },
    )


def _delete_file(ws: Workspace, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """删除一个普通文件。

    刻意不支持递归删除目录：一次误批可能带走整棵树，而"删目录"没有可回滚的
    单一 preimage 边界（快照按文件存）。目录请让用户自己处理。
    """
    target, rel = _write_target(ws, ctx, args["path"])
    if not target.exists():
        return ToolResult("failed", "none_applied", {"error": "文件不存在"})
    if not target.is_file():
        return ToolResult(
            "failed", "none_applied",
            {"error": "不是普通文件；不支持删除目录/符号链接以外的对象"},
        )

    before = target.read_bytes()
    before_state = _sha(before)
    # 删除的写后状态就是"不存在"——恢复协议用同一个 ABSENT 语义
    ctx.prewrite(rel, before, ABSENT)
    target.unlink()

    return ToolResult(
        "succeeded",
        "applied",
        {
            "path": rel,
            "deleted": True,
            "after_state": ABSENT,
            "before_state": before_state,
        },
    )


def _restore_file(ws: Workspace, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """把文件内容还原成写前状态。**不对模型暴露**，只由 undo 控制路径使用。"""
    target, rel = _write_target(ws, ctx, args["path"])
    try:
        data = base64.b64decode(args["content_b64"], validate=True)
    except Exception as exc:
        raise ToolArgumentError(f"content_b64 不是合法 base64：{exc}") from exc

    expect = args.get("sha256")
    after = _sha(data)
    if expect is not None and expect != after:
        raise ToolArgumentError(
            f"还原内容哈希不符：声明 {expect[:12]}…，实际 {after[:12]}…"
        )

    before = target.read_bytes() if target.exists() else None
    ctx.prewrite(rel, before, after)
    atomic_write_bytes(target, data)

    return ToolResult(
        "succeeded",
        "applied",
        {
            "path": rel,
            "bytes": len(data),
            "sha256": after,
            "after_state": after,
            "before_state": ABSENT if before is None else _sha(before),
            "restored": True,
        },
    )


# ---------------------------------------------------------------- 注册表

_HANDLERS: dict[str, Callable[[Workspace, dict[str, Any], ToolContext], ToolResult]] = {
    "list_files": _list_files,
    "read_text": _read_text,
    "create_report": _create_report,
    "modify_text": _modify_text,
    "delete_file": _delete_file,
    "restore_file": _restore_file,
}

SPECS: dict[str, ToolSpec] = {
    "list_files": ToolSpec(
        tool_id="list_files",
        version="1.0",
        description="列出目录下的文件与子目录，返回名字、大小、是否目录。",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        side_effect_level="pure",
        action=ACTION_READ,
    ),
    "read_text": ToolSpec(
        tool_id="read_text",
        version="1.0",
        description="读取一个 UTF-8 文本文件，超长会截断并标记 truncated。",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        side_effect_level="pure",
        action=ACTION_READ,
    ),
    "create_report": ToolSpec(
        tool_id="create_report",
        version="1.0",
        description="在 workspace 内新建报告文件。目标已存在时拒绝，不覆盖。",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
        side_effect_level="non_idempotent",
        exec_model="E3",
        action=ACTION_CREATE,
    ),
    "modify_text": ToolSpec(
        tool_id="modify_text",
        version="1.0",
        description=(
            "覆盖写入一个已存在的文本文件。写前内容会进快照，可用于撤销。"
            "新建文件请用 create_report。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        side_effect_level="non_idempotent",
        exec_model="E3",
        action=ACTION_OVERWRITE,
    ),
    "delete_file": ToolSpec(
        tool_id="delete_file",
        version="1.0",
        description="删除一个已存在的普通文件（不递归删目录）。写前内容会进快照，可撤销。",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        side_effect_level="non_idempotent",
        exec_model="E3",
        action=ACTION_DELETE,
    ),
    # undo 的内部还原动作：不允许模型看见，也不允许模型发起
    "restore_file": ToolSpec(
        tool_id="restore_file",
        version="1.0",
        description="（内部）把文件内容还原成写前状态，仅供 undo 控制路径使用。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content_b64": {"type": "string"},
                "sha256": {"type": "string"},
            },
            "required": ["path", "content_b64"],
            "additionalProperties": False,
        },
        side_effect_level="non_idempotent",
        exec_model="E3",
        action=ACTION_OVERWRITE,
        exposed=False,
    ),
}


def tool_manifest() -> list[dict[str, Any]]:
    """给模型看的工具清单（C2 的 ToolDefinition 投影）。内部工具不出现。"""
    return [
        {
            "name": s.tool_id,
            "description": s.description,
            "input_schema": s.input_schema,
        }
        for s in SPECS.values()
        if s.exposed
    ]


def action_for(tool_id: str) -> str:
    spec = SPECS.get(tool_id)
    if spec is None:
        raise ToolNotAllowed(f"工具不在白名单：{tool_id}")
    return spec.action


def is_write_tool(tool_id: str) -> bool:
    spec = SPECS.get(tool_id)
    return spec is not None and spec.side_effect_level != "pure"


# ---------------------------------------------------------------- 执行器


class FileExecutor:
    """受信任的内置文件执行器。

    它拿到的只是"这次派发的身份"（`AdmittedCall`），然后**自己**回权威库
    把每一条都重新核对一遍。这不是重复劳动：派发事务提交之后到这个文件真正
    被打开之间，另一个进程可能已经撤销了审批、接管了 claim、改了目标或者
    替换了租约持有者。只有执行侧再核一次，这段窗口才关得上。

    "再核一次"必须发生在执行守卫之内（由 Dispatcher 负责），否则核完到打开
    文件之间又是一段同样的窗口。
    """

    def __init__(
        self,
        *,
        tasks: TaskStore,
        leases: LeaseStore,
        grants: GrantStore,
        approvals: ApprovalStore,
        claims: object,
        dispatch_lookup: Callable[[str, str], DispatchRecord | None],
        dispatch_mark: Callable[[str, str, str], None],
        clock: Callable[[], int],
        log: object | None = None,
        snapshots: SnapshotStore | None = None,
    ) -> None:
        self._tasks = tasks
        self._leases = leases
        self._grants = grants
        self._approvals = approvals
        self._claims = claims
        self._lookup = dispatch_lookup
        self._mark = dispatch_mark
        self._clock = clock
        self._log = log
        self._snapshots = snapshots

    # ---- 独立核验 ----

    def _verify(self, admitted: AdmittedCall, permit: ExecutionPermit | None) -> None:
        if not isinstance(admitted, AdmittedCall):
            raise ToolNotAllowed(
                "执行器只接受 AdmittedCall；直接把参数交给它执行是被禁止的"
            )
        if not isinstance(permit, ExecutionPermit):
            raise ToolNotAllowed(
                "执行器必须在 execution_guard 内调用"
                "（缺少由 begin_execution 产生的 ExecutionPermit）"
            )

        req = admitted.request
        if not isinstance(req, ActionRequest) or not isinstance(
            admitted.claim, JobClaim
        ):
            raise ToolNotAllowed("票据类型不合法")

        if req.digest() != admitted.request_digest:
            raise ToolNotAllowed("请求摘要与票据不一致（票据被改过）")

        if permit.run_id != req.run_id or permit.task_id != req.task_id:
            raise ToolNotAllowed("执行许可属于另一个 Run")
        if (permit.goal_revision, permit.lease_epoch) != (
            req.goal_revision,
            req.lease_epoch,
        ):
            raise RunFenced(
                f"执行许可的 revision/epoch 与请求不符：许可 "
                f"{permit.goal_revision}/{permit.lease_epoch}，请求 "
                f"{req.goal_revision}/{req.lease_epoch}"
            )

        # 1. 这个 Run 现在的 owner 还是票据上的那个人吗
        self._claims.assert_claim(admitted.claim)  # type: ignore[attr-defined]
        if admitted.claim.claim_epoch != req.claim_epoch:
            raise ToolNotAllowed("票据的 claim_epoch 与请求不符")

        # 2. Run 没被 fence，revision/epoch 没变
        run = self._tasks.assert_current(req.run_id)
        if (run.goal_revision, run.lease_epoch) != (
            req.goal_revision,
            req.lease_epoch,
        ):
            raise RunFenced(
                f"Run 的 revision/epoch 已改变：当前 {run.goal_revision}/"
                f"{run.lease_epoch}，请求 {req.goal_revision}/{req.lease_epoch}"
            )

        # 3. 目标仍被本实例持有的**目录租约**覆盖（租约是按工作区取的，
        #    真正要写的是子树里的一条路径；只认祖先方向的覆盖）
        self._leases.assert_covers(list(admitted.resources), req.run_id)

        # 4. 这次调用真的被派发过（不是凭空递进来的票据）
        row = self._lookup(req.run_id, req.call_id)
        if row is None:
            raise ToolNotAllowed(
                f"没有派发记录，拒绝执行：call_id={req.call_id}"
            )
        if row.state != "intent":
            raise ToolNotAllowed(f"派发记录状态不是 intent：{row.state}")
        if row.request_digest != admitted.request_digest:
            raise ToolNotAllowed("派发记录的摘要与票据不符")
        if row.grant_id != admitted.grant_id:
            raise ToolNotAllowed("派发记录的 grant 与票据不符")
        if row.claim_epoch != admitted.claim.claim_epoch:
            raise ToolNotAllowed("派发记录的 claim_epoch 与票据不符")
        if row.owner_instance_id != admitted.claim.owner_instance_id:
            raise ToolNotAllowed("派发记录的 owner 与票据不符")

        # 5. grant：一次性、未过期、绑定同一个摘要
        grant = self._grants.get(admitted.grant_id)
        if grant is None:
            raise ToolNotAllowed("执行许可不存在")
        if grant.request_digest != admitted.request_digest:
            raise ToolNotAllowed("执行许可绑定的是另一个请求摘要")
        if grant.remaining_uses != 0:
            raise ToolNotAllowed("执行许可尚未被消费——派发不完整")
        if grant.expires_at <= self._clock():
            raise ToolNotAllowed("执行许可已过期")

        # 6. 审批：覆盖/删除必须有，且必须已"被消费一次"
        if req.action in APPROVAL_ACTIONS and not admitted.approval_id:
            raise ToolNotAllowed(f"{req.action} 缺少审批绑定")
        if admitted.approval_id:
            appr = self._approvals.get(admitted.approval_id)
            if appr is None:
                raise ToolNotAllowed("审批记录不存在")
            if appr.request_digest != admitted.request_digest:
                raise ToolNotAllowed("审批绑定的是另一个请求摘要")
            if appr.state != "consumed":
                raise ToolNotAllowed(f"审批未消费或已被撤销：{appr.state}")

        # 7. 目标没被换掉，也在可写绑定目录内
        target = Path(admitted.target)
        if ResourceIdentity.of(target).key != admitted.resource_key:
            raise ToolNotAllowed("目标资源的身份已改变（被替换/软链到别处）")
        ws = Workspace(Path(admitted.workspace_root))
        if ws.resolve(admitted.rel_path) != target:
            raise ToolNotAllowed("目标不在绑定目录内")

        task = self._tasks.get_task(req.task_id)
        if task is None:
            raise ToolNotAllowed(f"task 不存在：{req.task_id}")
        if not self._writable(task, target):
            raise ToolNotAllowed(f"目标不在任何可写绑定内：{target}")

        # 8. 写前状态仍然是审批时绑定的那一份
        ok, reason = check_preconditions(admitted)
        if not ok:
            raise ToolNotAllowed(reason)

    @staticmethod
    def _writable(task: object, target: Path) -> bool:
        from .permissions import PolicyEngine

        for binding in getattr(task, "workspaces", []) or []:
            if binding.can_write and PolicyEngine._covers(binding, str(target)):
                return True
        return False

    # ---- 执行 ----

    def execute(
        self, admitted: AdmittedCall, permit: ExecutionPermit | None
    ) -> ToolResult:
        self._verify(admitted, permit)

        spec = SPECS[admitted.tool_id]
        validate_args(admitted.args, spec.input_schema)

        ws = Workspace(Path(admitted.workspace_root))
        ctx = ToolContext(
            snapshots=self._snapshots,
            log=self._log,
            run_id=admitted.request.run_id,
            call_id=admitted.request.call_id,
            admitted=admitted,
        )

        # 已在执行：从这一刻起，这次调用算 in-flight。取消不承诺回滚。
        self._mark(admitted.request.run_id, admitted.request.call_id, "executing")
        try:
            return _HANDLERS[admitted.tool_id](ws, admitted.args, ctx)
        except (TianshuError, OSError):
            raise


def check_request_preconditions(
    action: str, target: Path, pre: dict[str, Any]
) -> tuple[bool, str]:
    """声明过的执行前状态现在还成立吗。

    审批绑定的是"这一次、这个内容、这个写前状态"。批准之后目标被人改过，
    旧审批就不再对应这次写入了——必须重新审批，不能"反正路径一样就写"。
    """
    target = Path(target)

    if action == ACTION_CREATE:
        declared = pre.get("target_exists")
        actual = target.exists()
        if declared is not None and bool(declared) != actual:
            return False, (
                f"目标是否已存在与请求声明不符（声明 {declared}，实际 {actual}）"
            )
        if actual:
            return False, "目标已存在，新建动作不再成立（按覆盖处理需重新审批）"
        return True, ""

    if action in (ACTION_OVERWRITE, ACTION_DELETE):
        declared = pre.get("before_state")
        if declared is None:
            return False, "覆盖/删除动作必须声明写前状态 before_state"
        actual = observe_state(target)
        if actual != declared:
            return False, (
                f"写前状态已改变：声明 {str(declared)[:12]}…，"
                f"实际 {str(actual)[:12]}…（目标在被批准后被改过）"
            )
        if action == ACTION_DELETE and not target.is_file():
            return False, "删除目标不是普通文件"
        return True, ""

    return True, ""


def check_preconditions(admitted: AdmittedCall) -> tuple[bool, str]:
    return check_request_preconditions(
        admitted.action, Path(admitted.target), admitted.request.preconditions or {}
    )


# ---------------------------------------------------------------- 遗留入口


def execute(
    ws: Workspace,
    tool_id: str,
    args: dict[str, Any],
    ctx: ToolContext | None = None,
) -> ToolResult:
    """M0 的同步执行入口。

    M2 之后**有副作用的工具必须经 Dispatcher 派发**，这里只剩读取工具与
    一个显式遗留开关（`allow_legacy_unauthenticated_write`）。整个代码库里
    只有 `loop.run()` 打开它，且 Task 5 会用 Task/Run/Job 生命周期把它删掉。
    """
    if tool_id not in _HANDLERS:
        raise ToolNotAllowed(f"工具不在白名单：{tool_id}")

    ctx = ctx or ToolContext()
    spec = SPECS[tool_id]

    # 有副作用的工具必须带 log，并且 workspace 未被阻断。
    # 把检查放在执行入口而不是 CLI，绕不过去。
    if spec.side_effect_level != "pure":
        if ctx.log is None:
            raise ToolNotAllowed("有副作用的工具必须提供 ToolContext.log 才能执行")
        ensure_not_blocked(ctx.log, ws.root)
        if not ctx.allow_legacy_unauthenticated_write:
            raise ToolNotAllowed(
                "有副作用的工具必须经 Dispatcher 派发（缺少已核验的 AdmittedCall）；"
                "只有 M0 遗留同步循环可用显式开关绕过"
            )

    validate_args(args, spec.input_schema)
    return _HANDLERS[tool_id](ws, args, ctx)


__all__ = [
    "AdmittedCall",
    "DispatchRecord",
    "FileExecutor",
    "READ_TEXT_LIMIT",
    "SPECS",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "action_for",
    "check_preconditions",
    "check_request_preconditions",
    "execute",
    "is_write_tool",
    "observe_state",
    "tool_manifest",
    "validate_args",
]
"""工具注册表、受限工具实现与受信任的内置文件执行器。

M0 白名单只有三个（docs/M0-scope.md §4）。M2 增加 `delete_file`，并新增一个
**不对模型暴露**的 `restore_file`（undo 还原写前内容用）。任何额外写入路径都是 bug。

两条硬规则：

1. 参数校验在执行侧做，不信任模型给出的参数。
2. **有副作用的工具必须有已核验的派发票据（`AdmittedCall`）才能写。**
   `execute()` 那条路已经彻底关掉了写入：谁调用它都写不了文件。

`FileExecutor` 是"实际打开文件"的唯一入口。它刻意**不接受**任何形式的
"调用方说已经批准了"——它只接受派发票据里的身份，然后自己回权威库核对一遍。
票据能被伪造（dataclass 都能构造），但伪造的票据核不过库。

M3 起，"字节怎么进出"下沉到 `sandbox.FileBackend`（见该模块开头的三条诚实
声明）。这个模块仍然是**契约的唯一实现**：截断、UTF-8 回退、拒绝覆盖、写前
证据顺序、output 形状、异常映射都在这里，后端只回答"有没有东西、读到了什么"。
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .crashpoint import failpoint
from .errors import (
    OverwriteRefused,
    RunFenced,
    TianshuError,
    ToolArgumentError,
    ToolNotAllowed,
)
from .exec import LOCAL_EXEC_CAPABILITIES, spawn
from .execpolicy import (
    ExecBackendCapabilities,
    ExecPolicy,
    PolicyDenied,
    PolicyUnsupported,
    filtered_env,
)
from .guard import assert_paths_not_blocked, ensure_not_blocked
from .jobs import JobClaim
from .permissions import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_EXEC,
    ACTION_OVERWRITE,
    ACTION_READ,
    APPROVAL_ACTIONS,
    ActionRequest,
)
from .sandbox import FileBackend, current_backend
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


def observe_state(target: Path, *, backend: FileBackend | None = None) -> str:
    """目标当前的写前状态：不是普通文件返回 ABSENT，否则返回 sha256。

    审批绑定的是"执行前状态"，所以这个函数既用于申请审批时取值，也用于
    真正写文件前复核——两次取值必须相同，否则说明目标被人换过了。

    读盘一律经当前后端：审批看到的世界与写入进入的世界必须是同一个，
    否则会出现"批的是真盘、写的是虚拟盘"这种半真半假的状态。

    **目录按 ABSENT 处理**（目录没有"文件写前状态"）：读目录会抛
    `IsADirectoryError`，而这个函数在 `build_request` 生成 preconditions
    时就被调用，异常会一路冒到主循环、把整轮 run 判成失败。拒绝目录是
    `check_request_preconditions` 的职责（它给出"目标是目录"的明确结论），
    不是这里抛异常。
    """
    io = backend if backend is not None else current_backend()
    if not io.exists(target) or io.is_dir(target):
        return ABSENT
    data, _ = io.read_bytes(target)
    return _sha(data)


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
    path_arg: str = ""  # 参数里表示"要改哪个文件"的键名
    path_default: str = ""  # path_arg 缺失时的默认值（run_command 的 cwd 用）


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

    `backend` 为空时回落到进程当前后端（`sandbox.current_backend()`）。
    之所以允许为空：只读路径由主循环构造 `ToolContext`（loop.py 的 `_exec_read`），
    而 M3 的判据要求那个文件一行都不改——进程级后端选择就是为了这个。
    """

    snapshots: SnapshotStore | None = None
    log: object | None = None
    run_id: str = ""
    call_id: str = ""
    admitted: AdmittedCall | None = None
    backend: FileBackend | None = None
    #: 任务级执行策略（None = 未配置 = run_command 默认全拒）。
    exec_policy: ExecPolicy | None = None
    exec_capabilities: ExecBackendCapabilities | None = None
    #: 截断输出的内容寻址目录（快照 blob 目录）；None 时只报 truncated。
    blob_dir: Path | None = None

    @property
    def io(self) -> FileBackend:
        return self.backend if self.backend is not None else current_backend()

    def prewrite(self, rel_path: str, before: bytes | None, expected_after: str) -> None:
        """写入前的证据落盘。必须在真正写文件之前调用。

        证据键是 `(workspace 根, 相对路径)`。根**只能**来自已核验的派发票据：
        `ToolContext` 自己不知道目录（它只拿到一个相对的 `rel_path`），多目录
        任务里任何"猜一个根"的实现都会把证据写到错误的 workspace 名下。
        所以没有票据时直接拒绝，而不是退回到某个默认目录。
        """
        if self.snapshots is None:
            return
        if self.admitted is None:
            raise ToolNotAllowed(
                "写前证据必须绑定 workspace 根：只接受已核验的派发票据"
                "（AdmittedCall），不接受调用方自行指定的目录"
            )
        self.snapshots.record_preimage(
            self.run_id,
            self.call_id,
            rel_path,
            before,
            expected_after,
            root=self.admitted.workspace_root,
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
    io = ctx.io
    sub = args.get("path", ".")
    target = ws.resolve(sub)
    if not io.is_dir(target):
        return ToolResult("failed", "none_applied", {"error": f"不是目录：{sub}"})

    try:
        raw_entries = io.list_dir(target)
    except OSError as exc:
        return ToolResult("failed", "none_applied", {"error": f"无法列目录：{exc}"})

    entries = [
        {"name": normalize_name(name), "is_dir": is_dir, "size": size}
        for name, is_dir, size in raw_entries
    ]
    # 排序键用**输出里看到的**名字（NFC），否则 macOS 的 NFD 文件名会出现
    # "显示顺序与排序顺序不一致"，模型每轮看到的东西都在抖。
    entries.sort(key=lambda e: e["name"])
    return ToolResult("succeeded", "none_applied", {"entries": entries})


def _read_text(ws: Workspace, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    io = ctx.io
    target = ws.resolve(args["path"])
    if not io.is_file(target):
        return ToolResult("failed", "none_applied", {"error": "不是文件"})

    try:
        # 后端一次打开就返回 (前缀, 总大小)：不再有 stat 与 read 之间的窗口。
        raw, total = io.read_bytes(target, READ_TEXT_LIMIT)
    except OSError as exc:
        return ToolResult("failed", "none_applied", {"error": f"读取失败：{exc}"})
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
    io = ctx.io
    target, rel = _write_target(ws, ctx, args["name"])

    # 验收 A6：已存在必须拒绝，不留覆盖的可能
    if io.exists(target):
        raise OverwriteRefused(f"目标已存在，拒绝覆盖：{args['name']}")

    data = args["content"].encode("utf-8")
    after = _sha(data)

    # 先落证据，再写文件。顺序反了就等于没证据——崩溃窗口里无从核实。
    ctx.prewrite(rel, None, after)
    io.write_bytes_atomic(target, data)

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
    io = ctx.io
    target, rel = _write_target(ws, ctx, args["path"])
    if not io.exists(target):
        return ToolResult(
            "failed", "none_applied",
            {"error": "文件不存在；新建请用 create_report"},
        )
    if not io.is_file(target):
        return ToolResult("failed", "none_applied", {"error": "不是文件"})

    try:
        before, _ = io.read_bytes(target)
    except OSError as exc:
        return ToolResult("failed", "none_applied", {"error": f"读取失败：{exc}"})
    data = args["content"].encode("utf-8")
    after = _sha(data)

    if before == data:
        # 复审 轻-1：写前写后内容一致 = 没有可观测副作用。恢复流程对同一状态
        # 判 `none_applied`（见 recovery 里 before==expected_after 那条），活体
        # 路径必须给同一个口径——否则会为一次什么都没改的调用产出一条 applied
        # 凭据，"副作用已发生"这句话就成了假的。
        return ToolResult(
            "succeeded",
            "none_applied",
            {
                "path": rel,
                "bytes": len(data),
                "sha256": after,
                "after_state": after,
                "before_state": after,
                "unchanged": True,
            },
        )

    ctx.prewrite(rel, before, after)
    io.write_bytes_atomic(target, data)

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
    io = ctx.io
    target, rel = _write_target(ws, ctx, args["path"])
    if not io.exists(target):
        return ToolResult("failed", "none_applied", {"error": "文件不存在"})
    if not io.is_file(target):
        return ToolResult(
            "failed", "none_applied",
            {"error": "不是普通文件；不支持删除目录/符号链接以外的对象"},
        )

    try:
        before, _ = io.read_bytes(target)
    except OSError as exc:
        return ToolResult("failed", "none_applied", {"error": f"读取失败：{exc}"})
    before_state = _sha(before)
    # 删除的写后状态就是"不存在"——恢复协议用同一个 ABSENT 语义
    ctx.prewrite(rel, before, ABSENT)
    io.unlink(target)

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

    io = ctx.io
    before = io.read_bytes(target)[0] if io.exists(target) else None
    ctx.prewrite(rel, before, after)
    io.write_bytes_atomic(target, data)

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


def _run_command(ws: Workspace, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """在 workspace 内执行一条命令（argv 数组，不经 shell）。

    **默认全拒**：`ctx.exec_policy is None` 时这里也拒绝（真正的第一道门在
    PolicyEngine：未配置 exec 的任务连审批都不会发起）。策略判定、能力判定与
    真正的执行都在 `exec.spawn()` 里，这里只把结果翻译成 C2 的 output 形状。
    """
    policy = ctx.exec_policy
    if policy is None:
        raise ToolNotAllowed(
            "未配置执行策略：run_command 默认全拒。"
            "开启 exec 是显式动作（task create --allow-exec-cmd）"
        )

    # 票据已核验过目标；参数里的 cwd 必须解析到同一个目录。
    target, rel = _write_target(ws, ctx, args.get("cwd", "."))
    capabilities = ctx.exec_capabilities or LOCAL_EXEC_CAPABILITIES
    argv = args["argv"]
    pty = bool(args.get("pty", False))
    # 凭据/密钥不进入子进程：只传策略白名单里的变量（spawn 内部还会再过滤一次）。
    child_env = filtered_env(policy, dict(os.environ))

    try:
        result = spawn(
            argv,
            cwd=target,
            env=child_env,
            policy=policy,
            capabilities=capabilities,
            blob_dir=ctx.blob_dir,
            pty=pty,
        )
    except PolicyDenied as exc:
        return ToolResult(
            "rejected",
            "none_applied",
            {
                "error": str(exc),
                "rule_id": exc.rule_id,
                "policy": {"decision": "denied", "rule_id": exc.rule_id},
            },
        )
    except PolicyUnsupported as exc:
        return ToolResult(
            "rejected",
            "none_applied",
            {
                "error": str(exc),
                "rule_id": "c4.exec_unsupported",
                "policy": {
                    "decision": "unsupported",
                    "backend": exc.backend,
                    "feature": exc.feature,
                },
            },
        )

    status = "succeeded" if result.exit_code == 0 and not result.timed_out else "failed"
    digest = _sha(
        "\x00".join(
            [
                str(list(argv)),
                str(result.exit_code),
                str(result.signal),
                str(result.timed_out),
                result.stdout,
                result.stderr,
            ]
        ).encode("utf-8")
    )
    return ToolResult(
        status,
        "applied",
        {
            "path": rel,
            "cwd": rel,
            "argv": list(argv),
            "exit_code": result.exit_code,
            "signal": result.signal,
            "timed_out": result.timed_out,
            "duration_ms": result.duration_ms,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stderr_merged": result.stderr_merged,
            "truncated": result.truncated,
            "stdout_ref": result.stdout_ref,
            "stderr_ref": result.stderr_ref,
            "after_state": f"exec:{digest}",
            "policy": {
                "decision": "allowed",
                "argv0": str(argv[0]) if argv else "",
                "pty": pty,
            },
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
    "run_command": _run_command,
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
        path_arg="name",
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
        path_arg="path",
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
        path_arg="path",
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
        path_arg="path",
        exposed=False,
    ),
    "run_command": ToolSpec(
        tool_id="run_command",
        version="1.0",
        description=(
            "在 workspace 内执行一条命令。argv 必须是数组（不经 shell）；"
            "需要任务显式开启 exec 策略，且每次都要人工审批。"
            "PTY 只在 POSIX 可用，Windows 上会返回 unsupported。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "cwd": {"type": "string"},
                "pty": {"type": "boolean"},
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
        side_effect_level="non_idempotent",
        exec_model="E3",
        action=ACTION_EXEC,
        path_arg="cwd",
        path_default=".",
    ),
    # ---- 记忆（F10） ---------------------------------------------------------
    #
    # 记忆目标不是文件路径：`resources` 里用 `memory://<scope>/<scope_id>`
    # 表达（见 resources.py）。所以这五个 ToolSpec **都没有 `path_arg`**：
    # 主循环不能拿它去 `Workspace.resolve()` 一个 URI；作用域由派发请求的
    # resource 决定，memory_id 是参数，不是路径。
    "memory.search": ToolSpec(
        tool_id="memory.search",
        version="1.0",
        description=(
            "在当前任务允许的记忆作用域内检索已激活的记忆。"
            "候选（未确认）不会被检索到；最多返回 5 条 / 4 KiB。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                # F10 §8：模型可以要求**更少**，不能要求更多。执行侧还会把
                # 它夹到 MAX_RECALL_ITEMS 之内，schema 只是把这条能力显式
                # 暴露出来——不写进 schema 就等于模型永远拿满 5 条。
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        side_effect_level="pure",
        action=ACTION_READ,
    ),
    "memory.get": ToolSpec(
        tool_id="memory.get",
        version="1.0",
        description="按 memory_id 读取一条仍可见的记忆及其正文。",
        input_schema={
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
            "additionalProperties": False,
        },
        side_effect_level="pure",
        action=ACTION_READ,
    ),
    "memory.propose": ToolSpec(
        tool_id="memory.propose",
        version="1.0",
        description=(
            "写一条未激活的记忆候选。候选不进检索、不需要审批；"
            "确认（memory.confirm）才会进入长期召回。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "content": {"type": "string"},
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {"type": "object"}},
                "expires_at": {"type": "integer"},
            },
            "required": ["kind", "content"],
            "additionalProperties": False,
        },
        side_effect_level="non_idempotent",
        exec_model="E3",
        action=ACTION_CREATE,
    ),
    "memory.confirm": ToolSpec(
        tool_id="memory.confirm",
        version="1.0",
        description=(
            "把一条候选记忆激活为长期召回。需要人工审批；"
            "用户本人明示来源（origin=user_explicit）的确认除外。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "revision": {"type": "integer"},
            },
            "required": ["memory_id", "revision"],
            "additionalProperties": False,
        },
        side_effect_level="non_idempotent",
        exec_model="E3",
        action=ACTION_OVERWRITE,
    ),
    "memory.forget": ToolSpec(
        tool_id="memory.forget",
        version="1.0",
        description=(
            "忘记一条记忆（写 tombstone，同事务从活动投影移除）。"
            "等同删除，需要人工审批；忘记后重建索引也不能复活它。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "revision": {"type": "integer"},
                "status": {"type": "string"},
            },
            "required": ["memory_id", "revision"],
            "additionalProperties": False,
        },
        side_effect_level="non_idempotent",
        exec_model="E3",
        action=ACTION_DELETE,
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


def target_arg(tool_id: str) -> str:
    """这个工具的参数里哪个键是"要改的文件"。"""
    spec = SPECS.get(tool_id)
    if spec is None:
        raise ToolNotAllowed(f"工具不在白名单：{tool_id}")
    if not spec.path_arg:
        raise ToolNotAllowed(f"工具没有文件目标参数：{tool_id}")
    return spec.path_arg


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
        backend: FileBackend | None = None,
        exec_capabilities: ExecBackendCapabilities | None = None,
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
        self._exec_capabilities = exec_capabilities or LOCAL_EXEC_CAPABILITIES
        self._backend = backend if backend is not None else current_backend()
        if self._backend is not current_backend():
            # "executor 用 memfs、precondition 读 local" 这种半真半假状态
            # 比直接崩更难查：审批看到的世界和写入进入的世界必须同一个。
            raise TianshuError(
                "执行器后端与进程当前后端不一致："
                f"{self._backend.name} vs {current_backend().name}"
                "（后端是进程级选择，请先 configure_backend）"
            )

    @property
    def backend_name(self) -> str:
        """这条执行路径实际使用的后端名字（进凭据）。"""
        return self._backend.name

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
            # 审批绑的是**动作摘要**（做了什么），派发记录绑的是**请求摘要**
            # （谁在什么 epoch 下做）。进程重启后 claim_epoch 变了，动作没变，
            # 审批必须继续有效——详见 docs/PRD-v1.md §5.4。旧库里的审批没有
            # 动作摘要，只能按请求摘要比，因为那时它只可能是同一个 epoch。
            if appr.action_digest:
                if appr.action_digest != req.action_digest():
                    raise ToolNotAllowed("审批绑定的动作与本次请求不符")
            elif appr.request_digest != admitted.request_digest:
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
        if self._backend is not current_backend():
            # 运行期被换掉：拒绝而不是混用。ToolNotAllowed 会走"守卫内拒绝"
            # 那条路（一个字节没动 → rejected/none_applied），不会被记成 unknown。
            raise ToolNotAllowed(
                "进程的后端在运行期被换掉了，拒绝在一个进程里混用两个后端"
            )
        self._verify(admitted, permit)

        spec = SPECS[admitted.tool_id]
        validate_args(admitted.args, spec.input_schema)

        ws = Workspace(Path(admitted.workspace_root))
        # 任务级 exec 策略在这里读：它是执行期的唯一来源，不在线程/全局里缓存。
        task = self._tasks.get_task(admitted.request.task_id)
        ctx = ToolContext(
            snapshots=self._snapshots,
            log=self._log,
            run_id=admitted.request.run_id,
            call_id=admitted.request.call_id,
            admitted=admitted,
            backend=self._backend,
            exec_policy=task.exec_policy if task is not None else None,
            exec_capabilities=self._exec_capabilities,
            blob_dir=self._snapshots.blob_dir if self._snapshots is not None else None,
        )

        # 复审 中-1：贴着落盘的最后一次阻断核验。派发 phase-1 查过一遍，但查完
        # 到这里之间可能有新的 `recovery.blocked` 落库；这里按**真正要写的那个
        # 路径**再判一次，判完下一句就是改文件，中间不再交给任何回调。
        assert_paths_not_blocked(self._log, [admitted.target])

        # 已在执行：从这一刻起，这次调用算 in-flight。取消不承诺回滚。
        self._mark(admitted.request.run_id, admitted.request.call_id, "executing")
        try:
            result = _HANDLERS[admitted.tool_id](ws, admitted.args, ctx)
        except (TianshuError, OSError):
            raise
        # 文件已经改了、结果还没提交。恢复流程必须能从 preimage/intent 判断
        # "到底改了没有"——这个故障点就是用来验证那句话的。
        failpoint("after_write")
        return result


def check_memory_preconditions(
    action: str,
    pre: dict[str, Any],
    observed: tuple[str, int] | None,
) -> tuple[bool, str]:
    """记忆的前置条件：status / revision，而不是文件哈希。

    `observed` 必须来自**执行侧自己**的推导（`MemoryProjection.observe()`），
    不能接受调用方传一个"我看到的是 active/1"。没有观测结果就是"不存在"，
    不能退化成"没有前置条件所以放行"。
    """
    if action == ACTION_CREATE:
        # 新建候选没有"旧记忆"可比较；去重由 MemoryExecutor 在写入前做。
        return True, ""
    if action not in (ACTION_OVERWRITE, ACTION_DELETE):
        return True, ""

    expected_revision = pre.get("before_revision")
    if expected_revision is None:
        return False, "记忆写入必须声明写前 revision（before_revision）"
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        return False, f"before_revision 必须是整数：{expected_revision!r}"
    if observed is None:
        return False, "记忆不存在（可能已被删除或不属于当前作用域）"

    status, revision = observed
    expected_status = pre.get("before_status")
    if expected_status is not None and status != expected_status:
        return False, (
            f"记忆状态已改变：声明 {expected_status!r}，实际 {status!r}"
            "（目标在被批准后被改过）"
        )
    if revision != expected_revision:
        return False, (
            f"记忆 revision 已改变：声明 {expected_revision}，实际 {revision}"
            "（目标在被批准后被改过）"
        )
    return True, ""


def check_request_preconditions(
    action: str,
    target: Path | str,
    pre: dict[str, Any],
    *,
    backend: FileBackend | None = None,
    memory_observed: tuple[str, int] | None = None,
) -> tuple[bool, str]:
    """声明过的执行前状态现在还成立吗。

    审批绑定的是"这一次、这个内容、这个写前状态"。批准之后目标被人改过，
    旧审批就不再对应这次写入了——必须重新审批，不能"反正路径一样就写"。

    记忆目标不是文件：它的"写前状态"是 **(status, revision)**，由执行侧从
    权威投影里重新读出来（`memory_observed`）。文件分支保持原样：仍然比较
    路径是否存在 / 文件哈希，绝不用记忆的前置条件去解释一个文件路径。
    """
    if isinstance(target, str) and target.startswith("memory://"):
        return check_memory_preconditions(action, pre, memory_observed)

    target = Path(target)
    io = backend if backend is not None else current_backend()

    if action == ACTION_CREATE:
        declared = pre.get("target_exists")
        actual = io.exists(target)
        if declared is not None and bool(declared) != actual:
            return False, (
                f"目标是否已存在与请求声明不符（声明 {declared}，实际 {actual}）"
            )
        if actual:
            if io.is_dir(target):
                return False, "目标已存在且是目录：create_report 只能新建普通文件"
            return False, (
                "目标已存在：create_report 不覆盖已有文件；"
                "要覆盖请改用 modify_text（删除类，需审批）"
            )
        return True, ""

    if action in (ACTION_OVERWRITE, ACTION_DELETE):
        declared = pre.get("before_state")
        if declared is None:
            return False, "覆盖/删除动作必须声明写前状态 before_state"
        if io.is_dir(target):
            return False, (
                "目标是目录：覆盖/删除只支持普通文件"
                "（目录没有单一 preimage 边界，也没有可回滚的文件状态）"
            )
        actual = observe_state(target, backend=io)
        if actual != declared:
            return False, (
                f"写前状态已改变：声明 {str(declared)[:12]}…，"
                f"实际 {str(actual)[:12]}…（目标在被批准后被改过）"
            )
        if action == ACTION_DELETE and not io.is_file(target):
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
    """不经派发的执行入口：只给**没有副作用**的工具用。

    M2 之后有副作用的工具必须经 Dispatcher 派发（消费审批、登记 intent、
    在守卫内核验）。这里不再留任何"遗留开关"：只要工具会改文件就直接拒绝。
    留一个开关等于留一条"谁能打开它"的问题，而答案永远是"下一个想走捷径的人"。
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
        raise ToolNotAllowed(
            "有副作用的工具必须经 Dispatcher 派发（缺少已核验的 AdmittedCall）"
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
    "check_memory_preconditions",
    "check_preconditions",
    "check_request_preconditions",
    "execute",
    "is_write_tool",
    "observe_state",
    "target_arg",
    "tool_manifest",
    "validate_args",
]
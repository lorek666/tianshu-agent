"""Agent 自动执行的 subject 绑定、现场准入与受管进程生命周期。

内置 local / Seatbelt / Landlock 有可核对的启动实现；显式 Windows Job Object
仅提供候选进程树治理，不构成完整沙箱。未知后端保持 unsupported；能力声明不能
代替实际启动配置和现场探针。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from . import exec_job_object
from .events import Event, canonical_bytes, sha256_hex
from .exec import (
    BOUNDARY_ENFORCED,
    BOUNDARY_NOT_ENFORCED,
    BOUNDARY_UNKNOWN,
    ExecResult,
    LocalExecBackend,
    _run_managed_pipes,
    platform_label,
    scrub_sandbox_env,
)
from .execpolicy import (
    ISOLATION_BOUNDARIES,
    ExecPolicy,
    PolicyDenied,
    PolicyUnsupported,
    check_spawn,
)
from .isolation import (
    PROBE_GRACE_MS,
    PROBE_VERSION,
    IsolationFinding,
    IsolationRefused,
    IsolationReport,
    _wait_pid_gone,
    probe_backend,
)

SUBJECT_KINDS = ("command", "local_mcp", "remote_mcp")
SUBJECT_PROBE_VERSION = f"{PROBE_VERSION}-managed-v1"
_RUNTIME_ID = uuid.uuid4().hex
_ACTIVE_LIFECYCLES: set[str] = set()
EventSink = Callable[[str, dict[str, Any]], None]


def _digest(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def _path_binding(path: str | os.PathLike[str]) -> str:
    root = Path(path).resolve(strict=True)
    stat = root.stat()
    return _digest({"path": str(root), "device": stat.st_dev, "inode": stat.st_ino})


def _program_digest(argv: tuple[str, ...], cwd: Path) -> str:
    programs: list[dict[str, Any]] = []
    candidates = [Path(argv[0])]
    arguments = iter(argv[1:])
    # Bind an interpreter's script after flags; later arguments can be output
    # files and must not turn a completed call into a changed executable.
    for item in arguments:
        if item in ("-c", "-m", "-e", "--eval"):
            break
        if item in ("-W", "-X", "--check-hash-based-pycs"):
            next(arguments, None)
            continue
        if item.startswith("-"):
            continue
        candidate = Path(item)
        candidate = candidate if candidate.is_absolute() else cwd / candidate
        if candidate.is_file():
            candidates.append(candidate)
        break
    for candidate in candidates:
        path = candidate.resolve(strict=True)
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        stat = path.stat()
        programs.append({
            "path": str(path), "sha256": digest.hexdigest(),
            "device": stat.st_dev, "inode": stat.st_ino,
        })
    return _digest(programs)


@dataclass(frozen=True)
class ExecutionSubjectV1:
    subject_id: str
    kind: str
    task_id: str
    run_id: str
    config_digest: str
    executable_digest: str
    environment_digest: str
    workspace_binding_digest: str
    data_root_digest: str
    isolation_profile_digest: str
    format_version: int = 1

    def __post_init__(self) -> None:
        if self.format_version != 1 or self.kind not in SUBJECT_KINDS:
            raise ValueError("未知 execution subject 版本或 kind")
        for name, value in asdict(self).items():
            if name.endswith("_digest") and (
                not isinstance(value, str) or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ValueError(f"非法 subject 摘要：{name}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def digest(self) -> str:
        return _digest(self.to_dict())


def build_execution_subject(
    *, kind: str, task_id: str, run_id: str, argv: tuple[str, ...] | list[str],
    cwd: str | os.PathLike[str], env: Mapping[str, str], policy: ExecPolicy,
    workspace: str | os.PathLike[str], data_root: str | os.PathLike[str],
    config: Mapping[str, Any] | None = None,
    isolation_profile: Mapping[str, Any] | None = None,
) -> ExecutionSubjectV1:
    if kind == "remote_mcp":
        raise PolicyUnsupported(
            "远程 MCP 不具有本机进程隔离证明", backend="remote-http",
            feature="remote_transport_admission",
        )
    if kind not in SUBJECT_KINDS or not argv:
        raise ValueError("执行对象必须有封闭 kind 和实际 argv")
    facts = {
        "kind": kind, "task_id": task_id, "run_id": run_id,
        "config_digest": _digest({
            "argv": list(argv), "cwd": str(Path(cwd).resolve()),
            "config": dict(config or {}),
        }),
        "executable_digest": _program_digest(tuple(argv), Path(cwd).resolve()),
        "environment_digest": _digest(dict(env)),
        "workspace_binding_digest": _path_binding(workspace),
        "data_root_digest": _path_binding(data_root),
        "isolation_profile_digest": _digest(
            policy.to_dict() if isolation_profile is None else dict(isolation_profile)
        ),
    }
    return ExecutionSubjectV1(subject_id=_digest(facts), **facts)



def find_command_approval(log, run_id: str, approval_id: str) -> Mapping[str, Any]:
    """读取最初向人展示的执行对象，不能用重新预检得到的视图替换。"""
    return next((event.payload for event in log.read_run(
        run_id, event_types=("approval.requested",),
    ) if event.payload["approval_id"] == approval_id), {})


def command_approval_matches(approval, backend: str, subject_digest: str | None) -> bool:
    approved_backend = approval.get("command", {}).get("backend")
    approved_digest = approval.get("execution_subject_digest")
    if (backend == "windows_job_object" or approved_backend == "windows_job_object"
            or approved_digest is not None):
        return bool(subject_digest and approved_backend == backend
                    and approved_digest == subject_digest)
    return True

def _backend_identity(backend: Any) -> str:
    return f"{_RUNTIME_ID}:{type(backend).__module__}.{type(backend).__qualname__}:{id(backend)}"


@dataclass(frozen=True)
class IsolationAdmissionV1:
    subject_digest: str
    backend: str
    backend_identity: str
    platform: str
    architecture: str
    probe_version: str
    workspace: str
    data_root: str
    required_boundaries: tuple[str, ...]
    boundaries: dict[str, str]
    evidence_refs: tuple[str, ...]
    lifecycle_id: str
    format_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["required_boundaries"] = list(self.required_boundaries)
        result["evidence_refs"] = list(self.evidence_refs)
        return result


def validate_admission(
    admission: IsolationAdmissionV1, subject: ExecutionSubjectV1, backend: Any,
) -> None:
    if (admission.format_version != 1 or admission.subject_digest != subject.digest()
            or admission.backend_identity != _backend_identity(backend)
            or admission.backend != backend.capabilities.name
            or admission.platform != platform_label()
            or admission.architecture != platform.machine().lower()
            or admission.probe_version != SUBJECT_PROBE_VERSION
            or any(admission.boundaries.get(boundary) != BOUNDARY_ENFORCED
                   for boundary in admission.required_boundaries)):
        raise PolicyUnsupported(
            "隔离准入不属于当前执行对象、后端实例或平台，必须现场重测",
            backend=backend.capabilities.name, feature="subject_admission",
        )


def process_birth_identity(pid: int) -> str | None:
    """OS 原生创建身份；不会使用有秒级漂移的 ps etime 冒充启动身份。"""
    try:
        if sys.platform == "darwin":
            class ProcBsdInfo(ctypes.Structure):
                _fields_ = [
                    ("ids", ctypes.c_uint32 * 12),
                    ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                    ("details", ctypes.c_uint32 * 6),
                    ("seconds", ctypes.c_uint64), ("microseconds", ctypes.c_uint64),
                ]
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            lib.proc_pidinfo.argtypes = [
                ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                ctypes.c_void_p, ctypes.c_int,
            ]
            lib.proc_pidinfo.restype = ctypes.c_int
            info = ProcBsdInfo()
            actual = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if actual != ctypes.sizeof(info):
                return None
            return f"darwin:{info.seconds}:{info.microseconds}"
        if sys.platform.startswith("linux"):
            raw = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
            boot = Path("/proc/sys/kernel/random/boot_id").read_bytes().decode("ascii").strip()
            return f"linux:{boot}:{raw[19].decode('ascii')}"
        if os.name == "nt":
            from .leases import pid_started_at

            started = pid_started_at(pid)
            return None if started is None else f"windows:{started}"
    except (OSError, ValueError, IndexError):
        return None
    return None


def reconcile_lifecycle(
    lifecycle: Mapping[str, Any], *, process_tree_enforced: bool = False,
) -> dict[str, Any]:
    """只用 OS 身份和整组观察核查旧 Session，不依据 PID/文件宣称死亡。"""
    pid, birth = lifecycle.get("pid"), lifecycle.get("birth_identity")
    state = "unknown"
    if (not process_tree_enforced or type(pid) is not int or pid <= 0
            or not isinstance(birth, str) or not birth):
        return {**lifecycle, "process_tree": state}
    current = process_birth_identity(pid)
    if current is not None:
        # Matching identity proves it is alive; a different one proves PID reuse,
        # neither grants permission to kill a process or reuse its old scope.
        return {**lifecycle, "process_tree": state}
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                state = "exited"
            except OSError:
                pass
        except OSError:
            pass
    return {**lifecycle, "process_tree": state}


def execution_terminal_matches(admitted: Event, terminal: Event) -> bool:
    """执行端和 Goal 协调端使用同一套终态身份核对。"""
    lifecycle, payload = admitted.payload["lifecycle"], terminal.payload
    return (
        terminal.run_id == admitted.run_id
        and terminal.task_id == admitted.task_id
        and payload["lifecycle_id"] == lifecycle["lifecycle_id"]
        and payload["subject_digest"] == _digest(admitted.payload["subject"])
        and payload["pid"] == lifecycle["pid"]
        and payload["birth_identity"] == lifecycle["birth_identity"]
    )


def assert_no_unresolved_executions(log, *, workspace, run_id: str = "") -> None:
    """历史未知调用/进程树阻止同资源自动重启；完成事实也不能清洗未知调用。"""
    root = Path(workspace).resolve()
    sessions: dict[tuple[str, str], dict[str, Any]] = {}
    for event in log.read_types(("execution.admitted", "execution.ended")):
        payload = event.payload
        if event.type == "execution.admitted":
            admission = payload["admission"]
            other = Path(admission["workspace"]).resolve()
            if not (root.is_relative_to(other) or other.is_relative_to(root)):
                continue
            identity = (event.run_id, admission["lifecycle_id"])
            row = sessions.setdefault(identity, {"pending": set(), "ended": False})
            row.update(lifecycle=payload["lifecycle"], event=event)
            if payload["stage"] == "call_pending":
                row["pending"].add(payload.get("request_id", 0))
            elif payload["stage"] == "call_completed":
                row["pending"].discard(payload.get("request_id", 0))
        else:
            row = sessions.get((event.run_id, payload["lifecycle_id"]))
            if row is not None and execution_terminal_matches(row["event"], event):
                row["ended"] = payload["process_tree"] == "exited"
    for (_, identity), row in sessions.items():
        if row["pending"]:
            raise PolicyUnsupported(
                "旧 MCP 调用结果未知，禁止新调用或自动重发",
                backend="execution-recovery", feature="effect_unknown",
            )
        if row["ended"] or (
            identity in _ACTIVE_LIFECYCLES and run_id
            and row["event"].run_id == run_id
        ):
            continue
        admission = row["event"].payload["admission"]
        if admission["backend"] == "windows_job_object":
            raise PolicyUnsupported(
                "旧 Job 的整树退出不可由 PID 证明，禁止同资源自动启动",
                backend="execution-recovery", feature="recovery_required",
            )
        state = reconcile_lifecycle(
            row["lifecycle"],
            process_tree_enforced=(
                admission["probe_version"] == SUBJECT_PROBE_VERSION
                and admission["boundaries"]["process_tree"] == BOUNDARY_ENFORCED
            ),
        )
        if state["process_tree"] != "exited":
            raise PolicyUnsupported(
                "旧执行生命周期身份或整树退出尚未核对，禁止同资源自动启动",
                backend="execution-recovery", feature="recovery_required",
            )
        event = row["event"]
        log.append(
            event.run_id, "execution.ended", {
                "subject_digest": _digest(event.payload["subject"]),
                "lifecycle_id": identity, "process_tree": "exited",
                "pid": state["pid"], "birth_identity": state["birth_identity"],
            }, task_id=event.task_id,
        )


class PreparedExecution:
    """一次 subject 的固定启动配置。探针与真实执行共享同一原生 profile。"""

    def __init__(
        self, backend: Any, *, kind: str, task_id: str, run_id: str,
        argv: tuple[str, ...] | list[str], cwd: str | os.PathLike[str],
        env: Mapping[str, str], policy: ExecPolicy,
        workspace: str | os.PathLike[str], data_root: str | os.PathLike[str],
        config: Mapping[str, Any] | None = None, on_event: EventSink | None = None,
    ) -> None:
        self.backend, self.capabilities = backend, backend.capabilities
        self.argv, self.cwd, self.policy = tuple(argv), Path(cwd).resolve(), policy
        self.workspace, self.data_root = Path(workspace).resolve(), Path(data_root).resolve()
        self.env, self.config = dict(env), dict(config or {})
        check_spawn(policy, self.argv, self.cwd, self.env)
        self.subject = build_execution_subject(
            kind=kind, task_id=task_id, run_id=run_id, argv=self.argv, cwd=self.cwd,
            env=self.env, policy=policy, workspace=self.workspace, data_root=self.data_root,
            config=self.config,
        )
        self.on_event = on_event
        self.lifecycle_id = uuid.uuid4().hex
        self.admission: IsolationAdmissionV1 | None = None
        self.report: IsolationReport | None = None
        self._tmp: Path | None = None
        self._prefix: tuple[str, ...] = ()
        self._profile_rules: dict[str, Any] = {}
        self._preexec: Callable[[], None] | None = None
        self._sandboxed = False
        self._single_process = False
        self._job_backend = type(backend) is exec_job_object.WindowsJobObjectExecBackend
        self._proc: Any | None = None
        self._birth: str | None = None
        self._used = False
        self._ended = False
        self._stop_attempted = False

    def __enter__(self) -> PreparedExecution:
        from .exec_landlock import (
            LandlockExecBackend,
            _apply_landlock_in_child,
            _build_landlock_rules,
            _LinuxSyscallDriver,
            _load_linux_libc,
            landlock_available,
        )
        from .exec_seatbelt import (
            SANDBOX_EXEC_PATH,
            SeatbeltExecBackend,
            SeatbeltLeafExecBackend,
            _build_sbpl_profile,
            seatbelt_available,
        )

        if type(self.backend) is LocalExecBackend:
            self.subject = self._current_subject()
            return self
        if self._job_backend:
            self._sandboxed = True
            self._profile_rules = {"windows_job_object": exec_job_object.native_profile()}
            self.subject = self._current_subject()
            return self
        self._tmp = Path(tempfile.mkdtemp(prefix="tianshu-subject-")).resolve()
        try:
            if (
                type(self.backend) in (SeatbeltExecBackend, SeatbeltLeafExecBackend)
                and seatbelt_available()
            ):
                self._single_process = not self.backend._allow_fork
                profile = _build_sbpl_profile(
                    self.policy, argv0=self.argv[0], extra_rw_roots=(self._tmp,),
                    allow_fork=self.backend._allow_fork,
                )
                self._prefix = (SANDBOX_EXEC_PATH, "-p", profile)
            elif type(self.backend) is LandlockExecBackend and landlock_available():
                driver = _LinuxSyscallDriver(_load_linux_libc())
                rules = _build_landlock_rules(
                    self.policy, argv0=self.argv[0], extra_rw_roots=(self._tmp,),
                )
                self._preexec = lambda: _apply_landlock_in_child(
                    self.backend.abi, rules, prctl_fn=driver.prctl_no_new_privs,
                    create_ruleset_fn=driver.create_ruleset, add_rule_fn=driver.add_path_rule,
                    restrict_self_fn=driver.restrict_self,
                )
                self._profile_rules = {
                    "landlock_abi": self.backend.abi,
                    "landlock_rules": [asdict(rule) for rule in rules],
                }
            else:
                raise PolicyUnsupported(
                    "当前后端没有实际 subject/受管 stdio 启动实现，禁止回落 local",
                    backend=self.capabilities.name, feature="subject_admission",
                )
            self._sandboxed = True
            self.subject = self._current_subject()
            return self
        except BaseException:
            self._cleanup()
            raise

    def _child_env(self, env: Mapping[str, str]) -> dict[str, str]:
        result = scrub_sandbox_env(env) if self._sandboxed else dict(env)
        if self._tmp is not None:
            result["TMPDIR"] = str(self._tmp)
        return result

    def _current_subject(self) -> ExecutionSubjectV1:
        return build_execution_subject(
            kind=self.subject.kind, task_id=self.subject.task_id, run_id=self.subject.run_id,
            argv=self.argv, cwd=self.cwd, env=self._child_env(self.env), policy=self.policy,
            workspace=self.workspace, data_root=self.data_root, config=self.config,
            isolation_profile={
                "policy": self.policy.to_dict(), "native_prefix": list(self._prefix),
                **self._profile_rules,
                "private_root": None if self._tmp is None else _path_binding(self._tmp),
            },
        )

    def spawn(
        self, argv, *, cwd, env, policy, blob_dir=None, pty=False,
    ) -> ExecResult:
        """四边界探针共用 stdio 启动、超时和整组回收；原生策略固定不变。"""
        self._require_pipes(pty)
        proc = self._launch_process(argv, cwd=cwd, env=env)
        result, _state = self._collect_process(proc, policy=policy, blob_dir=blob_dir)
        return result

    def _require_pipes(self, pty: bool) -> None:
        if pty:
            raise PolicyUnsupported(
                "subject 受管启动尚未证明 PTY 生命周期", backend=self.capabilities.name,
                feature="subject_pty",
            )

    def _launch_process(self, argv, *, cwd, env) -> Any:
        if self._job_backend:
            return exec_job_object.create_managed_process(
                argv, cwd=cwd, env=self._child_env(env)
            )
        return subprocess.Popen(
            (*self._prefix, *argv), cwd=cwd, env=self._child_env(env),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name == "posix", preexec_fn=self._preexec, bufsize=0,
        )

    def _process_tree_state(self, proc: Any) -> str:
        if self._job_backend:
            if getattr(proc, "_closed", False):
                return proc.tree_state
            return proc.controller.query()
        if self._single_process:
            # The fixed native profile forbids descendants. The managed child
            # wait is authoritative; a reaped PID/PGID may already be reused.
            return "exited" if proc.poll() is not None else "unknown"
        if os.name == "posix":
            if proc.returncode is not None:
                return "unknown"
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return "exited"
            except OSError:
                pass
        return "unknown"

    def _stop_process(self, proc: Any) -> str:
        if self._job_backend:
            return proc.close(2)
        group_identity_safe = False
        if self._single_process:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        elif os.name == "posix":
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                else:
                    group_identity_safe = True
        elif proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if os.name == "posix" and not self._single_process:
            if not group_identity_safe:
                return "unknown"
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return "exited"
            except OSError:
                pass
            return "unknown"
        return self._process_tree_state(proc)

    def _collect_process(
        self, proc, *, policy, blob_dir=None, should_stop=None,
    ) -> tuple[ExecResult, str | None]:
        if self._job_backend:
            try:
                result, state = _run_managed_pipes(
                    proc,
                    policy=policy,
                    blob_dir=blob_dir,
                    finalize_tree=lambda _timed_out: proc.controller.stop(2),
                    close_stdin=True,
                    should_stop=should_stop,
                )
            finally:
                close_state = proc.close(0)
            if close_state != exec_job_object.TREE_EXITED:
                state = "unknown"
                result = replace(result, descendants_may_survive=True)
            return result, state
        return _run_managed_pipes(
            proc,
            policy=policy,
            blob_dir=blob_dir,
            finalize_tree=lambda _timed_out: self._stop_process(proc),
            close_stdin=True,
        )

    def admit(self, required_boundaries=ISOLATION_BOUNDARIES) -> IsolationAdmissionV1:
        required = tuple(required_boundaries)
        if len(set(required)) != len(required) or set(required) - set(ISOLATION_BOUNDARIES):
            raise ValueError("未知或重复的隔离边界")
        if required:
            self.report = probe_backend(
                self, workspace=self.workspace, data_root=self.data_root,
            )
            detached = self._probe_detached_descendant()
            self.report = replace(
                self.report, probe_version=SUBJECT_PROBE_VERSION,
                findings=tuple(
                    detached if finding.boundary == "process_tree"
                    and (
                        detached.status == BOUNDARY_NOT_ENFORCED
                        or (
                            detached.status != BOUNDARY_ENFORCED
                            and finding.status != BOUNDARY_NOT_ENFORCED
                        )
                    ) else finding
                    for finding in self.report.findings
                ),
            )
            for boundary in required:
                finding = self.report.finding(boundary)
                if finding.status != BOUNDARY_ENFORCED:
                    raise IsolationRefused(
                        backend=self.capabilities.name, boundary=boundary,
                        status=finding.status, evidence=finding.evidence,
                    )
        boundaries = {
            name: self.report.finding(name).status if self.report else "unknown"
            for name in ISOLATION_BOUNDARIES
        }
        self.admission = IsolationAdmissionV1(
            subject_digest=self.subject.digest(), backend=self.capabilities.name,
            backend_identity=_backend_identity(self.backend), platform=platform_label(),
            architecture=platform.machine().lower(), probe_version=SUBJECT_PROBE_VERSION,
            workspace=str(self.workspace), data_root=str(self.data_root),
            required_boundaries=required, boundaries=boundaries,
            evidence_refs=tuple(
                f"sha256:{_digest(asdict(finding))}"
                for finding in (() if self.report is None else self.report.findings)
            ), lifecycle_id=self.lifecycle_id,
        )
        return self.admission

    def _windows_probe_record(
        self, path: Path, facts: dict[str, object], prefix: str, api: exec_job_object._NativeAPI,
    ) -> dict[str, int] | None:
        try:
            value = json.loads(path.read_bytes())
            pid, birth = value["pid"], value["birth"]
            handle = facts[f"{prefix}_handle"]
            if (
                type(pid) is not int or pid <= 0
                or type(birth) is not int or birth <= 0
                or type(handle) is not int or handle <= 0
                or facts.get(f"{prefix}_pid") != pid
            ):
                return None
            if api.process_identity(handle) != (pid, birth):
                return None
        except (OSError, ValueError, TypeError, KeyError, exec_job_object.JobObjectError):
            return None
        return {"pid": pid, "birth": birth, "handle": handle}

    @staticmethod
    def _cleanup_windows_probe_records(
        records: list[dict[str, int]],
    ) -> tuple[bool, list[int]]:
        confirmed = True
        survivors: list[int] = []
        try:
            api = exec_job_object._load_native_api()
        except (OSError, AttributeError):
            return False, [record["pid"] for record in records]
        for record in records:
            handle = record["handle"]
            identity_checked = False
            try:
                identity_checked = api.process_identity(handle) == (record["pid"], record["birth"])
                if not identity_checked:
                    confirmed = False
                    continue
                # The Job count can reach zero before a process handle is signaled.
                state = api.wait(handle, PROBE_GRACE_MS)
                if state == exec_job_object.WAIT_TIMEOUT:
                    survivors.append(record["pid"])
                    if not api.terminate_process(handle, 1):
                        confirmed = False
                    state = api.wait(handle, PROBE_GRACE_MS)
                if state != exec_job_object.WAIT_OBJECT_0:
                    confirmed = False
            except (exec_job_object.JobObjectError, OSError):
                confirmed = False
            finally:
                if identity_checked:
                    for attempt in range(3):
                        try:
                            if api.close_handle(handle):
                                break
                        except (exec_job_object.JobObjectError, OSError):
                            pass
                        if attempt < 2:
                            time.sleep(0.01)
                    else:
                        confirmed = False
        return confirmed, survivors

    def _probe_windows_descendant(self) -> IsolationFinding:
        try:
            api = exec_job_object._load_native_api()
        except (AttributeError, OSError) as exc:
            return IsolationFinding(
                "process_tree", BOUNDARY_UNKNOWN,
                f"Windows 探针无法加载进程身份与清理 API：{exc}",
            )
        root = Path(tempfile.mkdtemp(prefix=".tianshu-detach-probe-", dir=self.workspace))
        descendant_file = root / "descendant.json"
        breakaway_file = root / "breakaway.json"
        child = (
            "import ctypes,json,os,threading;from ctypes import wintypes;"
            "k=ctypes.WinDLL('kernel32',use_last_error=True);"
            "k.GetCurrentProcess.restype=ctypes.c_void_p;"
            "k.GetProcessTimes.argtypes=[ctypes.c_void_p]+"
            "[ctypes.POINTER(wintypes.FILETIME)]*4;"
            "k.GetProcessTimes.restype=ctypes.c_int;"
            "c=wintypes.FILETIME();e=wintypes.FILETIME();"
            "q=wintypes.FILETIME();u=wintypes.FILETIME();"
            "assert k.GetProcessTimes(k.GetCurrentProcess(),ctypes.byref(c),"
            "ctypes.byref(e),ctypes.byref(q),ctypes.byref(u));"
            "birth=(c.dwHighDateTime<<32)|c.dwLowDateTime;"
            "open(sys.argv[1],'w').write(json.dumps({'pid':os.getpid(),'birth':birth}));"
            "print('ready',flush=True);threading.Event().wait(120)"
        )
        child = "import sys\n" + child
        payload = (
            "import ctypes,json,os,subprocess,sys\n"
            "from ctypes import wintypes\n"
            "k=ctypes.WinDLL('kernel32',use_last_error=True)\n"
            "k.GetCurrentProcess.restype=wintypes.HANDLE\n"
            "k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]\n"
            "k.OpenProcess.restype=wintypes.HANDLE\n"
            "k.DuplicateHandle.argtypes=[wintypes.HANDLE,wintypes.HANDLE,"
            "wintypes.HANDLE,ctypes.POINTER(wintypes.HANDLE),wintypes.DWORD,"
            "wintypes.BOOL,wintypes.DWORD]\n"
            "k.DuplicateHandle.restype=wintypes.BOOL\n"
            "k.CloseHandle.argtypes=[wintypes.HANDLE]\n"
            "k.CloseHandle.restype=wintypes.BOOL\n"
            "def duplicate(p):\n"
            " parent=k.OpenProcess(0x40,False,int(sys.argv[3]))\n"
            " if not parent: raise ctypes.WinError(ctypes.get_last_error())\n"
            " try:\n"
            "  out=wintypes.HANDLE()\n"
            "  if not k.DuplicateHandle(k.GetCurrentProcess(),p._handle,parent,"
            "ctypes.byref(out),0,False,2): raise ctypes.WinError(ctypes.get_last_error())\n"
            "  return out.value\n"
            " finally: k.CloseHandle(parent)\n"
            "facts={}\n"
            "p=subprocess.Popen([sys.executable,'-I','-S','-c',"
            f"{child!r},sys.argv[1]],stdin=subprocess.DEVNULL,"
            "stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=0x00000008)\n"
            "p.stdout.readline()\n"
            "facts['descendant_pid']=p.pid\n"
            "facts['descendant_handle']=duplicate(p)\n"
            "try:\n"
            " b=subprocess.Popen([sys.executable,'-I','-S','-c',"
            f"{child!r},sys.argv[2]],stdin=subprocess.DEVNULL,"
            "stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,"
            "creationflags=0x01000000|0x00000200)\n"
            " b.stdout.readline()\n"
            " facts['breakaway_pid']=b.pid\n"
            " facts['breakaway_handle']=duplicate(b)\n"
            "except OSError as exc:\n"
            " facts['denied_winerror']=exc.winerror\n"
            "print(json.dumps(facts),flush=True)\n"
        )
        evidence = "Windows Job detached/breakaway 探针没有给出可核对结果"
        status = BOUNDARY_UNKNOWN
        records: list[dict[str, int]] = []
        breakaway_valid = False
        cleanup_confirmed = False
        survivors: list[int] = []
        tree_state = "unknown"
        result: ExecResult | None = None
        proc = None
        try:
            policy = ExecPolicy(
                argv_allowlist=(sys.executable,), cwd_roots=(self.workspace,),
                env_allowlist=("PATH",), timeout_ms=5000, max_output_bytes=4096,
            )
            # Probe a real interpreter, without the Windows venv redirector's Job.
            executable = getattr(sys, "_base_executable", sys.executable)
            proc = self._launch_process(
                (executable, "-I", "-S", "-c", payload,
                 str(descendant_file), str(breakaway_file), str(os.getpid())),
                cwd=self.workspace, env={"PATH": os.defpath},
            )
            result, tree_state = self._collect_process(proc, policy=policy)
            try:
                facts = json.loads(result.stdout)
            except (ValueError, TypeError):
                facts = {}
            if not isinstance(facts, dict):
                facts = {}
            descendant = self._windows_probe_record(
                descendant_file, facts, "descendant", api
            )
            if descendant is not None:
                records.append(descendant)
            if breakaway_file.exists():
                breakaway = self._windows_probe_record(
                    breakaway_file, facts, "breakaway", api
                )
                breakaway_valid = breakaway is not None
                if breakaway is not None:
                    records.append(breakaway)
            if result.exit_code == 0 and not result.timed_out:
                if (
                    tree_state == "exited"
                    and not result.descendants_may_survive
                    and descendant is not None
                    and facts.get("denied_winerror") == 5
                    and descendant_file.exists()
                    and not breakaway_file.exists()
                ):
                    status = BOUNDARY_ENFORCED
                    evidence = (
                        "普通 DETACHED_PROCESS 后代在 leader 退出后由 Job 清空，"
                        "breakaway 创建被 ACCESS_DENIED 拒绝"
                    )
                else:
                    evidence += f"；facts={facts} tree_state={tree_state}"
            else:
                evidence += (
                    f"；facts={facts} exit_code={result.exit_code} "
                    f"timeout={result.timed_out} tree_state={tree_state}"
                )
        except (OSError, subprocess.SubprocessError, exec_job_object.JobObjectError) as exc:
            evidence += f"：{type(exc).__name__}: {exc}"
        finally:
            if proc is not None and self._job_backend:
                try:
                    if proc.close(0) != exec_job_object.TREE_EXITED:
                        status = BOUNDARY_UNKNOWN
                except (exec_job_object.JobObjectError, OSError):
                    status = BOUNDARY_UNKNOWN
            cleanup_confirmed, survivors = self._cleanup_windows_probe_records(records)
            if survivors:
                status = BOUNDARY_NOT_ENFORCED
                evidence += f"；Job 外仍存在精确句柄对应的后代：{survivors}"
            if not cleanup_confirmed:
                status = BOUNDARY_UNKNOWN
            if breakaway_valid:
                status = BOUNDARY_NOT_ENFORCED
            evidence += f"；探针清理={'confirmed' if cleanup_confirmed else 'unknown'}"
            shutil.rmtree(root, ignore_errors=True)
        return IsolationFinding("process_tree", status, evidence)

    def _probe_detached_descendant(self) -> IsolationFinding:
        """负控：真正脱离原 session 的孙进程不得被 killpg 的成功洗白。"""
        if os.name == "nt":
            return self._probe_windows_descendant()
        if os.name != "posix":
            return IsolationFinding(
                "process_tree", BOUNDARY_UNKNOWN,
                "没有原生受控进程树容器，不能用直接子进程/进程组消失证明整树退出",
            )
        root = Path(tempfile.mkdtemp(prefix=".tianshu-detach-probe-", dir=self.workspace))
        pid_file = root / "detached.pid"
        child = (
            "import os,pathlib,sys,threading;"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
            "print('ready',flush=True);threading.Event().wait(120)"
        )
        payload = (
            "import errno,json,subprocess,sys\n"
            "try:\n"
            f" p=subprocess.Popen([sys.executable,'-I','-S','-c',{child!r},{str(pid_file)!r}],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,"
            "start_new_session=True)\n"
            " p.stdout.readline()\n"
            " print(json.dumps({'detached_pid':p.pid}),flush=True)\n"
            "except OSError as exc:\n"
            " print(json.dumps({'denied_errno':exc.errno}),flush=True)\n"
        )
        evidence = "原生 detached Session 探针没有给出可核对结果"
        status = BOUNDARY_UNKNOWN
        pid = None
        birth = None
        try:
            policy = ExecPolicy(
                argv_allowlist=(sys.executable,), cwd_roots=(self.workspace,),
                env_allowlist=("PATH",), timeout_ms=3000, max_output_bytes=4096,
            )
            result = self.spawn(
                (sys.executable, "-I", "-S", "-c", payload), cwd=self.workspace,
                env={"PATH": os.defpath}, policy=policy,
            )
            try:
                facts = json.loads(result.stdout)
            except (ValueError, TypeError):
                facts = {}
            try:
                pid = int(pid_file.read_bytes())
            except (OSError, ValueError):
                pass
            if pid is not None:
                birth = process_birth_identity(pid)
            if birth is not None:
                status = BOUNDARY_NOT_ENFORCED
                evidence = (
                    f"真实孙进程 setsid 脱离受管进程组后仍存活，pid={pid}；"
                    "killpg/leader 退出不能证明整个进程树退出"
                )
            elif facts.get("denied_errno") in (1, 13):
                status = BOUNDARY_ENFORCED
                evidence = f"实际原生 profile 拒绝 fork/setsid，errno={facts['denied_errno']}"
            else:
                evidence += f"；exit_code={result.exit_code} timeout={result.timed_out}"
        except (OSError, subprocess.SubprocessError) as exc:
            evidence += f"：{type(exc).__name__}: {exc}"
        finally:
            if pid is None:
                try:
                    pid = int(pid_file.read_bytes())
                except (OSError, ValueError):
                    pass
            if pid is not None:
                if birth is not None and process_birth_identity(pid) == birth:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                alive = _wait_pid_gone(pid, 3000)
                evidence += f"；探针清理={'confirmed' if alive is False else 'unknown'}"
            shutil.rmtree(root, ignore_errors=True)
        return IsolationFinding("process_tree", status, evidence)

    def _before_start(self) -> None:
        self.check_current()
        if self._used:
            raise PolicyUnsupported(
                "执行对象已经消费，禁止重放", backend=self.capabilities.name,
                feature="subject_admission",
            )
        self._used = True
        self.record_stage("prepared")

    def check_current(self) -> None:
        try:
            current = self._current_subject()
        except OSError as exc:
            raise PolicyUnsupported(
                f"执行对象现场已失效：{exc}", backend=self.capabilities.name,
                feature="subject_admission",
            ) from exc
        if self.admission is None:
            raise PolicyUnsupported(
                "执行对象未准入或已经消费，禁止重放", backend=self.capabilities.name,
                feature="subject_admission",
            )
        validate_admission(self.admission, current, self.backend)

    def run(self, *, blob_dir=None, pty=False, should_stop=None) -> ExecResult:
        self._require_pipes(pty)
        if should_stop is not None and should_stop():
            raise PolicyDenied("命令启动前 Run 已停止", rule_id="dispatch.not_running")
        proc = self.open_stdio()
        result, state = self._collect_process(
            proc, policy=self.policy, blob_dir=blob_dir, should_stop=should_stop,
        )
        self._stop_attempted = True
        if not self._tree_enforced():
            state = "unknown"
            result = replace(result, descendants_may_survive=True)
        _ACTIVE_LIFECYCLES.discard(self.lifecycle_id)
        self._emit_end(state)
        return result

    def open_stdio(self) -> Any:
        self._before_start()
        try:
            self._proc = self._launch_process(self.argv, cwd=self.cwd, env=self.env)
        except (PolicyUnsupported, OSError):
            self._emit_end("exited")
            raise
        except exec_job_object.JobObjectError:
            self._emit_end("unknown")
            raise
        self._birth = (self._proc.birth_identity() if self._job_backend
                       else process_birth_identity(self._proc.pid))
        _ACTIVE_LIFECYCLES.add(self.lifecycle_id)
        self.record_stage("started")
        return self._proc

    def record_stage(self, stage: str, *, call_id: str = "", request_id: int = 0) -> None:
        payload = {
            "subject": self.subject.to_dict(), "admission": self.admission.to_dict(),
            "lifecycle": self.lifecycle(), "stage": stage,
            "probe_findings": [
                asdict(finding) for finding in (() if self.report is None else self.report.findings)
            ],
        }
        if stage in ("call_pending", "call_completed"):
            payload.update(call_id=call_id, request_id=request_id)
        self._emit("execution.admitted", payload)

    def lifecycle(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id,
            "pid": None if self._proc is None else self._proc.pid,
            "birth_identity": self._birth,
        }

    def reconcile(self) -> dict[str, Any]:
        state = (
            self._process_tree_state(self._proc)
            if self._proc is not None and self._tree_enforced() else "unknown"
        )
        return {**self.lifecycle(), "process_tree": state}

    def _tree_enforced(self) -> bool:
        return (
            self.report is not None
            and self.report.probe_version == SUBJECT_PROBE_VERSION
            and self.report.finding("process_tree").status == BOUNDARY_ENFORCED
        )

    def stop(self, *, record: bool = True) -> dict[str, Any]:
        proc = self._proc
        if proc is not None and not self._stop_attempted:
            self._stop_attempted = True
            self._stop_process(proc)
        result = self.reconcile()
        _ACTIVE_LIFECYCLES.discard(self.lifecycle_id)
        if self._used and record:
            self._emit_end(result["process_tree"])
        return result

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event_type, payload)

    def _emit_end(self, state: str) -> None:
        if not self._ended:
            self._emit("execution.ended", {
                "subject_digest": self.subject.digest(), "lifecycle_id": self.lifecycle_id,
                "process_tree": state, "pid": None if self._proc is None else self._proc.pid,
                "birth_identity": self._birth,
            })
            self._ended = True

    def _cleanup(self) -> None:
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def __exit__(self, *_exc) -> None:
        try:
            if self._proc is not None:
                self.stop()
        finally:
            self._cleanup()

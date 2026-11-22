"""Agent 自动执行的 subject 绑定、现场准入与受管进程生命周期。

仅内置 local / Seatbelt / Landlock 有可核对的启动实现。未知后端和 Windows
原生隔离保持 unsupported；能力声明不能代替实际启动配置和现场探针。
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
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .events import canonical_bytes, sha256_hex
from .exec import (
    BOUNDARY_ENFORCED,
    BOUNDARY_NOT_ENFORCED,
    BOUNDARY_UNKNOWN,
    ExecResult,
    LocalExecBackend,
    _build_result,
    _join_pumps,
    _pump_file,
    _start_pump,
    _StreamCollector,
    platform_label,
    scrub_sandbox_env,
)
from .execpolicy import (
    ISOLATION_BOUNDARIES,
    ExecPolicy,
    PolicyUnsupported,
    check_spawn,
)
from .isolation import (
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


def assert_no_unresolved_executions(log, *, workspace, run_id: str = "") -> None:
    """历史未知调用/进程树阻止同资源自动重启；完成事实也不能清洗未知调用。"""
    root = Path(workspace).resolve()
    sessions: dict[str, dict[str, Any]] = {}
    for event in log.read_types(("execution.admitted", "execution.ended")):
        payload = event.payload
        if event.type == "execution.admitted":
            admission = payload["admission"]
            other = Path(admission["workspace"]).resolve()
            if not (root.is_relative_to(other) or other.is_relative_to(root)):
                continue
            identity = admission["lifecycle_id"]
            row = sessions.setdefault(identity, {"pending": set(), "ended": False})
            row.update(lifecycle=payload["lifecycle"], event=event)
            if payload["stage"] == "call_pending":
                row["pending"].add(payload.get("request_id", 0))
            elif payload["stage"] == "call_completed":
                row["pending"].discard(payload.get("request_id", 0))
        else:
            row = sessions.get(payload["lifecycle_id"])
            if row is not None:
                row["ended"] = payload["process_tree"] == "exited"
    for identity, row in sessions.items():
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
        self._proc: subprocess.Popen[bytes] | None = None
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
            _build_sbpl_profile,
            seatbelt_available,
        )

        if type(self.backend) is LocalExecBackend:
            self.subject = self._current_subject()
            return self
        self._tmp = Path(tempfile.mkdtemp(prefix="tianshu-subject-")).resolve()
        try:
            if type(self.backend) is SeatbeltExecBackend and seatbelt_available():
                profile = _build_sbpl_profile(
                    self.policy, argv0=self.argv[0], extra_rw_roots=(self._tmp,),
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

    def _launch_process(self, argv, *, cwd, env) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            (*self._prefix, *argv), cwd=cwd, env=self._child_env(env),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name == "posix", preexec_fn=self._preexec, bufsize=0,
        )

    @staticmethod
    def _process_tree_state(proc: subprocess.Popen[bytes]) -> str:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return "exited"
            except OSError:
                pass
        return "unknown"

    @classmethod
    def _stop_process(cls, proc: subprocess.Popen[bytes]) -> str:
        if os.name == "posix":
            # Capture group identity at launch: the leader may already have exited.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        elif proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return cls._process_tree_state(proc)

    def _collect_process(self, proc, *, policy, blob_dir=None) -> tuple[ExecResult, str]:
        started = time.monotonic()
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        proc.stdin.close()
        os.set_blocking(proc.stdout.fileno(), False)
        os.set_blocking(proc.stderr.fileno(), False)
        out = _StreamCollector(policy.max_output_bytes, blob_dir, "stdout")
        err = _StreamCollector(policy.max_output_bytes, blob_dir, "stderr")
        stop_reading = threading.Event()
        threads = [
            _start_pump(_pump_file, proc.stdout, out, stop_reading),
            _start_pump(_pump_file, proc.stderr, err, stop_reading),
        ]
        timed_out = False
        try:
            proc.wait(timeout=policy.timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            state = self._stop_process(proc)
            _join_pumps(threads, stop_reading)
        result = _build_result(
            proc, out, err, timed_out=timed_out, tree_killed=state == "exited",
            duration_ms=int((time.monotonic() - started) * 1000), stderr_merged=False,
        )
        return result, state

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
                    and detached.status != BOUNDARY_ENFORCED else finding
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

    def _probe_detached_descendant(self) -> IsolationFinding:
        """负控：真正脱离原 session 的孙进程不得被 killpg 的成功洗白。"""
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
            f" p=subprocess.Popen([sys.executable,'-c',{child!r},{str(pid_file)!r}],"
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
                (sys.executable, "-c", payload), cwd=self.workspace,
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

    def run(self, *, blob_dir=None, pty=False) -> ExecResult:
        self._require_pipes(pty)
        proc = self.open_stdio()
        result, state = self._collect_process(proc, policy=self.policy, blob_dir=blob_dir)
        self._stop_attempted = True
        if not self._tree_enforced():
            state = "unknown"
            result = replace(result, descendants_may_survive=True)
        _ACTIVE_LIFECYCLES.discard(self.lifecycle_id)
        self._emit_end(state)
        return result

    def open_stdio(self) -> subprocess.Popen[bytes]:
        self._before_start()
        try:
            self._proc = self._launch_process(self.argv, cwd=self.cwd, env=self.env)
        except OSError:
            # Popen did not return a child. This known rejection has no live lifecycle.
            self._emit_end("exited")
            raise
        self._birth = process_birth_identity(self._proc.pid)
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

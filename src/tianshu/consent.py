"""人类签发的有限范围规则；每次兑现仍由 Dispatcher 原子签发一次性 Grant。"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .canonical import jcs_sha256
from .errors import TianshuError
from .events import EventLog
from .execpolicy import ISOLATION_BOUNDARIES, filtered_env
from .leases import GUARD_TIMEOUT_MS, execution_guard
from .permissions import ActionRequest, ApprovalError, Grant, HumanIdentity
from .workspace import ResourceIdentity, is_ancestor_or_self, overlap_key


class ConsentError(ApprovalError):
    """有限授权不可用；不能以另一张 Scope 替换已准备动作的来源。"""


@dataclass(frozen=True)
class ConsentBindingV1:
    task_id: str
    run_id: str
    goal_revision: int
    workspace_binding_digest: str
    mission_digest: str
    plan_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScopedConsentV1:
    """内容不可变；使用次数、累计字节和撤销状态由事件及投影保存。"""

    document: dict[str, Any]

    @property
    def consent_id(self) -> str:
        return self.document["consent_id"]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.document))


def workspace_binding_digest(bindings) -> str:
    """同时绑定规范目录、原生身份、读写和快照配置。"""
    return jcs_sha256([{
        **binding.to_dict(),
        "identity": binding.identity().key,
    } for binding in bindings])


def tool_binding(spec) -> dict[str, str]:
    if spec is None:
        raise ConsentError("工具未注册")
    return {
        "tool_id": spec.tool_id, "version": spec.version,
        "schema_hash": jcs_sha256(spec.input_schema),
        "description_hash": spec.description_digest(),
    }


def directory_matcher(
    directory: Path, *, recursive: bool, max_file_bytes: int, max_total_bytes: int,
) -> dict[str, Any]:
    path = Path(directory).resolve(strict=True)
    if not path.is_dir():
        raise ConsentError("有限授权目录必须是真实目录")
    return {
        "kind": "directory_overwrite", "directory": str(path),
        "directory_identity": ResourceIdentity.of(path).key,
        "recursive": recursive, "allow_content_changes": True,
        "max_file_bytes": max_file_bytes, "max_total_bytes": max_total_bytes,
    }


def command_matcher(task, *, args: dict[str, Any], cwd: Path) -> dict[str, Any]:
    policy = task.exec_policy
    argv = args.get("argv")
    if (policy is None or not isinstance(argv, list) or not argv
            or any(not isinstance(item, str) or not item or "\0" in item for item in argv)
            or not Path(argv[0]).is_absolute()):
        raise ConsentError("精确命令需要完整 argv 和已配置的 ExecPolicy")
    executable = Path(argv[0]).resolve(strict=True)
    if not executable.is_file():
        raise ConsentError("命令可执行文件不存在")
    import hashlib

    with executable.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "kind": "exact_command", "executable_path": str(executable),
        "executable_digest": digest, "argv": list(argv),
        "cwd": str(Path(cwd).resolve(strict=True)),
        "environment_digest": jcs_sha256(filtered_env(policy, dict(os.environ))),
        "timeout_ms": policy.timeout_ms,
        "isolation_profile_digest": jcs_sha256(policy.to_dict()),
        "pty": args.get("pty", False),
    }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scoped_consents (
    consent_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE, document TEXT NOT NULL, state TEXT NOT NULL,
    remaining_uses INTEGER NOT NULL, used_bytes INTEGER NOT NULL DEFAULT 0,
    monotonic_expires INTEGER NOT NULL, runtime_session_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_selections (
    request_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
    consent_id TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
"""


class ConsentStore:
    """绑定本次 daemon runtime；不提供模型可见工具，也不改变默认逐次审批。"""

    def __init__(
        self, log: EventLog, *, tasks, data_root: Path,
        runtime_session_id: str,
        binding_resolver: Callable[[str], ConsentBindingV1 | None],
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
        monotonic: Callable[[], int] = lambda: int(time.monotonic() * 1000),
        guard_timeout_ms: int = GUARD_TIMEOUT_MS,
    ):
        if log.in_transaction:
            raise TianshuError("ConsentStore 不能在事务内构造")
        if not isinstance(runtime_session_id, str) or not runtime_session_id:
            raise ConsentError("runtime_session_id 必须非空")
        if getattr(tasks, "_conn", None) is not log.conn:
            raise ConsentError("ConsentStore 与 TaskStore 必须共用事件连接")
        self._log, self._conn, self._tasks = log, log.conn, tasks
        self._data_root = Path(data_root).resolve()
        self._runtime = runtime_session_id
        self._binding_resolver = binding_resolver
        self._clock, self._monotonic = clock, monotonic
        self._guard_timeout_ms = guard_timeout_ms
        self._conn.executescript(_SCHEMA)
        # 新 runtime 不继承旧 Scope；与执行的开始线性化点使用同一把守卫。
        with execution_guard(self._data_root, timeout_ms=guard_timeout_ms):
            with log.transaction():
                for row in self._conn.execute(
                    "SELECT * FROM scoped_consents WHERE state='active'"
                    " AND runtime_session_id != ?", (runtime_session_id,),
                ).fetchall():
                    self._expire(row, "runtime_restarted")

    def _current(self, run_id: str) -> ConsentBindingV1:
        binding = self._binding_resolver(run_id)
        if not isinstance(binding, ConsentBindingV1):
            raise ConsentError("缺少已确认 Mission/Plan 绑定")
        run = self._tasks.assert_current(run_id)
        task = self._tasks.get_task(run.task_id)
        if (task is None or binding.task_id != run.task_id or binding.run_id != run_id
                or binding.goal_revision != run.goal_revision
                or binding.workspace_binding_digest != workspace_binding_digest(task.workspaces)
                or not binding.mission_digest or not binding.plan_digest):
            raise ConsentError("当前 Task/Run/工作区绑定已改变")
        return binding

    @staticmethod
    def _human(actor):
        if not isinstance(actor, HumanIdentity) or actor.source != "cli":
            raise ConsentError("有限授权仅限认证本机人类控制面")

    def issue(self, document: dict[str, Any], *, actor: HumanIdentity) -> ScopedConsentV1:
        from .event_schema import validate_payload

        self._human(actor)
        try:
            validate_payload("consent.issued", document, 1)
        except TianshuError as exc:
            raise ConsentError(str(exc)) from exc
        # 先复制，防止调用方在校验后修改已保存对象。
        document = json.loads(json.dumps(document))
        if (document["issued_by"] != actor.to_dict()
                or document["runtime_session_id"] != self._runtime):
            raise ConsentError("签发身份或 runtime session 不符")
        with execution_guard(self._data_root, timeout_ms=self._guard_timeout_ms):
            with self._log.transaction() as tx:
                prior = self._conn.execute(
                    "SELECT document FROM scoped_consents WHERE request_id=?",
                    (document["request_id"],),
                ).fetchone()
                if prior:
                    if json.loads(prior["document"]) != document:
                        raise ConsentError("request_id 已用于不同的有限授权")
                    return ScopedConsentV1(document)
                binding = self._current(document["run_id"])
                if any(document[key] != value for key, value in binding.to_dict().items()):
                    raise ConsentError("有限授权的 Mission/Plan/工作区绑定不符")
                now = self._clock()
                if document["expires_at"] <= now:
                    raise ConsentError("有限授权已过期")
                self._validate_scope(document)
                self._conn.execute(
                    "INSERT INTO scoped_consents"
                    " (consent_id,task_id,run_id,request_id,document,state,remaining_uses,"
                    "monotonic_expires,runtime_session_id) VALUES (?,?,?,?,?,'active',?,?,?)",
                    (document["consent_id"], document["task_id"], document["run_id"],
                     document["request_id"], json.dumps(document), document["max_uses"],
                     self._monotonic() + document["expires_at"] - now,
                     self._runtime),
                )
                tx.append(document["run_id"], "consent.issued", document,
                          task_id=document["task_id"], actor_kind="human",
                          actor_id=actor.actor, idem_key=document["request_id"])
        return ScopedConsentV1(document)

    def _validate_scope(self, doc):
        matcher = doc["matcher"]
        tool = doc["tool_binding"]["tool_id"]
        task = self._tasks.get_task(doc["task_id"])
        limits = [doc["max_uses"], doc["expires_at"]]
        if matcher["kind"] == "directory_overwrite":
            limits.extend([matcher["max_file_bytes"], matcher["max_total_bytes"]])
        if any(value >= 2**63 for value in limits):
            raise ConsentError("有限授权限额超出可持久化的整数范围")
        if matcher["kind"] == "directory_overwrite":
            if doc["action"] != "overwrite" or tool != "modify_text":
                raise ConsentError("目录规则只支持固定版本 modify_text 覆盖")
            root = Path(matcher["directory"])
            if (not root.is_absolute() or str(root.resolve(strict=True)) != str(root)
                    or not root.is_dir()
                    or ResourceIdentity.of(root).key != matcher["directory_identity"]
                    or not any(b.can_write and is_ancestor_or_self(
                        overlap_key(b.root_path), overlap_key(str(root)),
                    ) for b in task.workspaces)):
                raise ConsentError("目录规则不在当前可写工作区或身份不符")
            if self._authority_path(root):
                raise ConsentError("有限授权不得覆盖权威运行数据")
            if matcher["max_total_bytes"] < matcher["max_file_bytes"]:
                raise ConsentError("累计字节上限不能低于单文件上限")
        elif doc["action"] != "exec" or tool != "run_command":
            raise ConsentError("精确命令规则只支持固定版本 run_command")
        elif (task.exec_policy is None
              or not set(ISOLATION_BOUNDARIES).issubset(task.exec_policy.isolation_requires)):
            raise ConsentError("有限命令授权必须要求全部执行隔离边界")
        elif matcher != command_matcher(
            task, args={"argv": matcher["argv"], "pty": matcher["pty"]},
            cwd=Path(matcher["cwd"]),
        ):
            raise ConsentError("命令可执行文件、环境或隔离配置已改变")

    def _authority_path(self, path: Path) -> bool:
        # 工作区可能包含数据根；范围规则不获得事件库、快照或数据根的权限。
        protected = [
            self._data_root, self._log.db_path, self._log.blob_dir,
            Path(str(self._log.db_path) + "-wal"),
            Path(str(self._log.db_path) + "-shm"),
        ]
        for target in protected:
            if target is not None and is_ancestor_or_self(
                overlap_key(str(Path(target).resolve())), overlap_key(str(path.resolve())),
            ):
                return True
        return False

    def list(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scoped_consents"
        rows = self._conn.execute(
            sql + (" WHERE run_id=?" if run_id else "") + " ORDER BY rowid",
            (run_id,) if run_id else (),
        ).fetchall()
        return [{
            **json.loads(row["document"]), "state": (
                "expired" if row["state"] == "active" and not self._live(row)
                else row["state"]
            ),
            "remaining_uses": row["remaining_uses"], "used_bytes": row["used_bytes"],
        } for row in rows]

    def _row(self, consent_id):
        row = self._conn.execute(
            "SELECT * FROM scoped_consents WHERE consent_id=?", (consent_id,),
        ).fetchone()
        if row is None:
            raise ConsentError("有限授权不存在")
        return row

    def _live(self, row):
        doc = json.loads(row["document"])
        return (row["state"] == "active"
                and row["runtime_session_id"] == self._runtime
                and self._clock() < doc["expires_at"]
                and self._monotonic() < row["monotonic_expires"])

    def _expire(self, row, reason):
        self._conn.execute(
            "UPDATE scoped_consents SET state='expired' WHERE consent_id=?",
            (row["consent_id"],),
        )
        self._log.append(row["run_id"], "consent.expired", {
            "format_version": 1, "consent_id": row["consent_id"], "reason": reason,
        }, task_id=row["task_id"])

    def _matches(self, row, request, args):
        doc = json.loads(row["document"])
        if not self._live(row):
            return False
        try:
            binding = self._current(request.run_id)
        except (TianshuError, OSError, ValueError):
            return False
        if (any(doc[k] != v for k, v in binding.to_dict().items())
                or request.goal_revision != binding.goal_revision
                or request.task_id != binding.task_id
                or request.action != doc["action"]
                or doc["tool_binding"] != {
                    "tool_id": request.tool_id, "version": request.tool_version,
                    "schema_hash": request.schema_hash,
                    "description_hash": request.description_hash,
                }
                or len(request.resources) != 1
                or jcs_sha256(args) != request.args_hash):
            return False
        matcher = doc["matcher"]
        try:
            path = Path(request.resources[0]).resolve(strict=True)
            if matcher["kind"] == "directory_overwrite":
                root = Path(matcher["directory"])
                content = args.get("content")
                return (
                    request.tool_id == "modify_text" and request.action == "overwrite"
                    and isinstance(content, str) and path.is_file()
                    and ResourceIdentity.of(root).key == matcher["directory_identity"]
                    and is_ancestor_or_self(overlap_key(str(root)), overlap_key(str(path)))
                    and (matcher["recursive"] or path.parent == root)
                    and not self._authority_path(path)
                    and len(content.encode("utf-8")) <= matcher["max_file_bytes"]
                )
            task = self._tasks.get_task(request.task_id)
            return (request.tool_id == "run_command" and request.action == "exec"
                    and set(ISOLATION_BOUNDARIES).issubset(task.exec_policy.isolation_requires)
                    and matcher == command_matcher(task, args=args, cwd=path))
        except (OSError, ValueError, TypeError, AttributeError, TianshuError):
            return False

    @staticmethod
    def _bytes(doc, args):
        return len(args["content"].encode("utf-8")) if doc["action"] == "overwrite" else 0

    def selected(self, request: ActionRequest) -> str | None:
        row = self._conn.execute(
            "SELECT * FROM consent_selections WHERE request_id=?", (request.request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request.digest():
            raise ConsentError("有限授权绑定的请求摘要不符")
        return row["consent_id"]

    def select(self, request: ActionRequest, *, args: dict[str, Any]) -> str | None:
        """prepare 只固定来源，不预扣配额；事务派发才消耗。"""
        with self._log.transaction():
            chosen = self.selected(request)
            if chosen:
                if not self._matches(self._row(chosen), request, args):
                    raise ConsentError("已准备动作的有限授权被撤销、到期或绑定改变")
                return chosen
            for row in self._conn.execute(
                "SELECT * FROM scoped_consents WHERE run_id=? AND state='active'"
                " AND remaining_uses > 0 ORDER BY rowid", (request.run_id,),
            ).fetchall():
                doc = json.loads(row["document"])
                if (self._matches(row, request, args)
                        and (doc["action"] != "overwrite"
                             or row["used_bytes"] + self._bytes(doc, args)
                             <= doc["matcher"]["max_total_bytes"])):
                    self._conn.execute(
                        "INSERT INTO consent_selections VALUES (?,?,?,0)",
                        (request.request_id, request.digest(), row["consent_id"]),
                    )
                    return row["consent_id"]
        return None

    def consume(self, request: ActionRequest, *, args: dict[str, Any]) -> str:
        """只许在 Dispatcher 的同一 intent 事务里兑现，不能由 worker 单独调用。"""
        if not self._log.in_transaction:
            raise ConsentError("有限授权兑现必须与具体 Grant 和 intent 同事务")
        consent_id = self.selected(request)
        if consent_id is None:
            raise ConsentError("动作未绑定有限授权")
        row = self._row(consent_id)
        if not self._matches(row, request, args):
            raise ConsentError("有限授权已撤销、过期或当前绑定不符")
        doc = json.loads(row["document"])
        count = self._bytes(doc, args)
        maximum = doc["matcher"].get("max_total_bytes", 0)
        cur = self._conn.execute(
            "UPDATE scoped_consents SET remaining_uses=remaining_uses-1,"
            " used_bytes=used_bytes+? WHERE consent_id=? AND remaining_uses>0"
            " AND (used_bytes+? <= ? OR ?='exec')",
            (count, consent_id, count, maximum, doc["action"]),
        )
        if cur.rowcount != 1:
            raise ConsentError("有限授权次数或累计字节配额已用尽")
        used = self._conn.execute(
            "UPDATE consent_selections SET used=1 WHERE request_id=? AND used=0",
            (request.request_id,),
        )
        if used.rowcount != 1:
            raise ConsentError("同一次派发不能重复兑现有限授权")
        self._log.append(request.run_id, "consent.used", {
            "format_version": 1, "consent_id": consent_id,
            "request_id": request.request_id, "request_digest": request.digest(),
            "action_digest": request.action_digest(), "attempt_id": request.attempt_id,
            "bytes": count, "remaining_uses": row["remaining_uses"] - 1,
        }, task_id=request.task_id)
        return consent_id

    def verify(self, request: ActionRequest, *, args: dict[str, Any], grant: Grant) -> None:
        consent_id = self.selected(request)
        if (not consent_id or grant.issuer != f"consent:{consent_id}"
                or grant.request_digest != request.digest()
                or grant.subject != f"{request.run_id}#{request.claim_epoch}"
                or grant.remaining_uses != 0):
            raise ConsentError("具体执行 Grant 与有限授权兑现不符")
        selection = self._conn.execute(
            "SELECT used FROM consent_selections WHERE request_id=?", (request.request_id,),
        ).fetchone()
        if not selection or not selection["used"] or not self._matches(
            self._row(consent_id), request, args,
        ):
            raise ConsentError("有限授权已撤销、过期或当前绑定失效")

    def revoke(
        self, consent_id: str, *, actor: HumanIdentity, request_id: str,
        reason: str = "revoked by human",
    ) -> dict[str, Any]:
        self._human(actor)
        if not isinstance(request_id, str) or not request_id or not reason:
            raise ConsentError("撤销必须有 request_id 和原因")
        with execution_guard(self._data_root, timeout_ms=self._guard_timeout_ms):
            with self._log.transaction():
                row = self._row(consent_id)
                if row["state"] == "active":
                    self._conn.execute(
                        "UPDATE scoped_consents SET state='revoked' WHERE consent_id=?",
                        (consent_id,),
                    )
                    self._log.append(row["run_id"], "consent.revoked", {
                        "format_version": 1, "consent_id": consent_id,
                        "request_id": request_id, "reason": reason,
                    }, task_id=row["task_id"], actor_kind="human", actor_id=actor.actor)
        return next(item for item in self.list(run_id=row["run_id"])
                    if item["consent_id"] == consent_id)

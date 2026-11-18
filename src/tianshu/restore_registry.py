"""固定用户 restore registry：严格只追加权威与完整链观察。

Observation 是进程内事实快照，不是可序列化契约或执行许可。所有持久记录都从
规范字节重新解析并用 T0 value contract 验证；head 仅是同一事务维护的缓存。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from .crashpoint import failpoint
from .errors import TianshuError
from .maintenance import (
    DatabaseAccess,
    MaintenanceBusy,
    acquire_database_access,
    acquire_entity_access,
    acquire_restore_gate,
)
from .platform_identity import NativeEntityError, observe_entity
from .restore_contract import (
    DirRefV1,
    EntityIdentityV1,
    EntityRefV1,
    NativePathCodecV1,
    RegistryMetaV1,
    RestoreEventV1,
    RestoreEventV2,
    domain_digest,
    validate_event_chain,
    validate_event_chain_v2,
)

APPLICATION_ID = 1414746673
USER_VERSION = 1
USER_VERSION_V2 = 2

REGISTRY_SCHEMA = """\
PRAGMA application_id = 1414746673;
PRAGMA user_version = 1;

CREATE TABLE registry_meta (
    singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
    registry_id     TEXT NOT NULL,
    meta_bytes      BLOB NOT NULL,
    meta_sha256     TEXT NOT NULL
) STRICT;

CREATE TABLE restore_events (
    sequence        INTEGER PRIMARY KEY CHECK (sequence > 0),
    registry_id     TEXT NOT NULL,
    batch_id        TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (
        kind IN (
            'reserved',
            'stage_owned',
            'sealed_hold',
            'publish_intent',
            'published_hold',
            'abort_intent',
            'aborted'
        )
    ),
    previous_sha256 TEXT,
    event_sha256    TEXT NOT NULL UNIQUE,
    event_bytes     BLOB NOT NULL,
    UNIQUE (batch_id, kind)
) STRICT;

CREATE TABLE registry_head (
    singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
    sequence        INTEGER NOT NULL CHECK (sequence >= 0),
    event_sha256    TEXT,
    CHECK ((sequence = 0) = (event_sha256 IS NULL))
) STRICT;

CREATE TRIGGER registry_meta_no_update
BEFORE UPDATE ON registry_meta
BEGIN
    SELECT RAISE(ABORT, 'registry meta is immutable');
END;

CREATE TRIGGER registry_meta_no_delete
BEFORE DELETE ON registry_meta
BEGIN
    SELECT RAISE(ABORT, 'registry meta is immutable');
END;

CREATE TRIGGER registry_events_no_update
BEFORE UPDATE ON restore_events
BEGIN
    SELECT RAISE(ABORT, 'restore_events is append-only');
END;

CREATE TRIGGER registry_events_no_delete
BEFORE DELETE ON restore_events
BEGIN
    SELECT RAISE(ABORT, 'restore_events is append-only');
END;

CREATE TRIGGER registry_events_chain_guard
BEFORE INSERT ON restore_events
BEGIN
    SELECT CASE
        WHEN NEW.sequence != (SELECT COALESCE(MAX(sequence), 0) + 1 FROM restore_events)
        THEN RAISE(ABORT, 'restore event sequence is not the exact next value')
        WHEN NEW.sequence = 1 AND NEW.previous_sha256 IS NOT NULL
        THEN RAISE(ABORT, 'first restore event must not have a previous hash')
        WHEN NEW.sequence > 1 AND NOT EXISTS (
            SELECT 1 FROM restore_events
            WHERE sequence = NEW.sequence - 1
              AND event_sha256 = NEW.previous_sha256
        )
        THEN RAISE(ABORT, 'restore event previous hash does not match the tail')
        WHEN NEW.registry_id != (
            SELECT registry_id FROM registry_meta WHERE singleton = 1
        )
        THEN RAISE(ABORT, 'restore event registry id does not match immutable meta')
    END;
END;

CREATE TRIGGER registry_head_advance_guard
BEFORE UPDATE ON registry_head
BEGIN
    SELECT CASE
        WHEN NEW.sequence != OLD.sequence + 1
        THEN RAISE(ABORT, 'registry head must advance by one')
        WHEN NEW.event_sha256 IS NOT (
            SELECT event_sha256 FROM restore_events WHERE sequence = NEW.sequence
        )
        THEN RAISE(ABORT, 'registry head must name the exact event tail')
    END;
END;
"""

_V2_EVENTS_TABLE_DDL = """\
CREATE TABLE restore_events (
    sequence        INTEGER PRIMARY KEY CHECK (sequence > 0),
    registry_id     TEXT NOT NULL,
    batch_id        TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (
        kind IN (
            'reserved',
            'stage_owned',
            'sealed_hold',
            'publish_intent',
            'published_hold',
            'abort_intent',
            'aborted',
            'release_intent',
            'released'
        )
    ),
    previous_sha256 TEXT,
    event_sha256    TEXT NOT NULL UNIQUE,
    event_bytes     BLOB NOT NULL,
    UNIQUE (batch_id, kind)
) STRICT"""

_V2_TRIGGER_DDLS = (
    """\
CREATE TRIGGER registry_events_no_update
BEFORE UPDATE ON restore_events
BEGIN
    SELECT RAISE(ABORT, 'restore_events is append-only');
END""",
    """\
CREATE TRIGGER registry_events_no_delete
BEFORE DELETE ON restore_events
BEGIN
    SELECT RAISE(ABORT, 'restore_events is append-only');
END""",
    """\
CREATE TRIGGER registry_events_chain_guard
BEFORE INSERT ON restore_events
BEGIN
    SELECT CASE
        WHEN NEW.sequence != (SELECT COALESCE(MAX(sequence), 0) + 1 FROM restore_events)
        THEN RAISE(ABORT, 'restore event sequence is not the exact next value')
        WHEN NEW.sequence = 1 AND NEW.previous_sha256 IS NOT NULL
        THEN RAISE(ABORT, 'first restore event must not have a previous hash')
        WHEN NEW.sequence > 1 AND NOT EXISTS (
            SELECT 1 FROM restore_events
            WHERE sequence = NEW.sequence - 1
              AND event_sha256 = NEW.previous_sha256
        )
        THEN RAISE(ABORT, 'restore event previous hash does not match the tail')
        WHEN NEW.registry_id != (
            SELECT registry_id FROM registry_meta WHERE singleton = 1
        )
        THEN RAISE(ABORT, 'restore event registry id does not match immutable meta')
    END;
END""",
    """\
CREATE TRIGGER registry_head_advance_guard
BEFORE UPDATE ON registry_head
BEGIN
    SELECT CASE
        WHEN NEW.sequence != OLD.sequence + 1
        THEN RAISE(ABORT, 'registry head must advance by one')
        WHEN NEW.event_sha256 IS NOT (
            SELECT event_sha256 FROM restore_events WHERE sequence = NEW.sequence
        )
        THEN RAISE(ABORT, 'registry head must name the exact event tail')
    END;
END""",
)

REGISTRY_SCHEMA_V2 = (
    REGISTRY_SCHEMA.replace("PRAGMA user_version = 1;", "PRAGMA user_version = 2;", 1).replace(
        "'aborted'\n",
        "'aborted',\n            'release_intent',\n            'released'\n",
        1,
    )
)

class RestoreRegistryError(TianshuError):
    """registry 无法形成可解释的完整事实。"""


class RestoreRegistryCorrupt(RestoreRegistryError):
    """registry 缺失片段、格式未知或链不成立。"""


class RestoreRegistryBusy(RestoreRegistryError):
    """另一个写者已经改变 head；调用者必须重新完整观察。"""


class RestoreRegistryDurabilityUnavailable(RestoreRegistryError):
    """平台无法证明已声明的目录持久化边界。"""


@dataclass(frozen=True)
class AbsentRegistryObservation:
    present: Literal[False]
    home: Path
    authority_directory: Path
    home_identity: EntityIdentityV1


@dataclass(frozen=True)
class PresentRegistryObservation:
    present: Literal[True]
    home: Path
    authority_directory: Path
    registry_database: Path
    home_identity: EntityIdentityV1
    authority_identity: EntityIdentityV1
    database_identity: EntityIdentityV1
    meta: RegistryMetaV1
    meta_bytes: bytes
    events: tuple[RestoreEventV1 | RestoreEventV2, ...]
    head_sequence: int
    head_sha256: str | None

    @property
    def next_sequence(self) -> int:
        return self.head_sequence + 1


RegistryObservation = AbsentRegistryObservation | PresentRegistryObservation


def _posix_home() -> Path:
    import pwd

    try:
        value = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError) as exc:
        raise RestoreRegistryCorrupt(f"无法从真实账户取得Home：{exc}") from exc
    if not value:
        raise RestoreRegistryCorrupt("真实账户Home为空")
    return Path(value)


def _windows_home() -> Path:  # pragma: no cover - native Windows only
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    get_folder = shell32.SHGetFolderPathW
    get_folder.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
    ]
    get_folder.restype = wintypes.LONG
    folder = ctypes.create_unicode_buffer(260)
    csidl_profile = 0x0028
    result = get_folder(None, csidl_profile, None, 0, folder)
    if result != 0 or not folder.value:
        raise RestoreRegistryCorrupt(
            f"SHGetFolderPathW(Profile)失败：HRESULT=0x{result & 0xffffffff:08x}"
        )
    return Path(folder.value)


def _user_home() -> Path:
    return _windows_home() if os.name == "nt" else _posix_home()


def _reject_link(path: Path, kind: str) -> None:
    try:
        if path.is_symlink() or path.is_junction():
            raise RestoreRegistryCorrupt(f"registry {kind}不能是链接：{path}")
    except OSError as exc:
        raise RestoreRegistryCorrupt(f"registry {kind}链接状态不可判定：{path}") from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _native_reference(path: Path, identity):
    codec = NativePathCodecV1()
    return codec.from_path(path), identity


def _meta(
    registry_id: str,
    home: Path,
    authority: Path,
    database: Path,
    *,
    home_identity,
    authority_identity,
    database_identity,
) -> RegistryMetaV1:
    return RegistryMetaV1(
        1,
        registry_id,
        DirRefV1(*_native_reference(home, home_identity)),
        DirRefV1(*_native_reference(authority, authority_identity)),
        EntityRefV1(*_native_reference(database, database_identity)),
    )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=0")
    connection.execute("PRAGMA query_only=ON")
    return connection


def _connect_write(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.resolve().as_uri(),
        uri=True,
        timeout=0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=0")
    return connection


def _verify_schema(connection: sqlite3.Connection, *, expected_version: int | None = None) -> int:
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if application_id != APPLICATION_ID or user_version not in (USER_VERSION, USER_VERSION_V2):
        raise RestoreRegistryCorrupt("registry application_id/user_version不符")
    if expected_version is not None and user_version != expected_version:
        raise RestoreRegistryCorrupt("registry user_version与期望版本不符")
    rows = tuple(
        connection.execute(
            "SELECT name,type,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )
    actual = tuple((row["name"], row["type"], row["tbl_name"], row["sql"]) for row in rows)
    if actual != _expected_schema_rows(user_version):
        raise RestoreRegistryCorrupt("registry schema对象集合、所有权或DDL不符")
    return int(user_version)


@cache
def _expected_schema_rows(version: int = USER_VERSION) -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        ddl = REGISTRY_SCHEMA if version == USER_VERSION else REGISTRY_SCHEMA_V2
        connection.executescript(ddl)
        rows = connection.execute(
            "SELECT name,type,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
        return tuple((row["name"], row["type"], row["tbl_name"], row["sql"]) for row in rows)
    finally:
        connection.close()


def _upgrade_v1_to_v2_in_transaction(connection: sqlite3.Connection) -> None:
    """在已开启的 BEGIN IMMEDIATE 事务内原地无损将 RestoreRegistry 由 v1 升至 v2。"""
    _verify_schema(connection, expected_version=USER_VERSION)
    buffered = list(
        connection.execute(
            "SELECT sequence,registry_id,batch_id,kind,previous_sha256,"
            "event_sha256,event_bytes FROM restore_events ORDER BY sequence ASC"
        )
    )
    for trigger_name in (
        "registry_head_advance_guard",
        "registry_events_chain_guard",
        "registry_events_no_delete",
        "registry_events_no_update",
    ):
        connection.execute(f"DROP TRIGGER {trigger_name}")
    connection.execute("DROP TABLE restore_events")
    connection.execute(_V2_EVENTS_TABLE_DDL)
    for trig_sql in _V2_TRIGGER_DDLS:
        connection.execute(trig_sql)
    for row in buffered:
        connection.execute(
            "INSERT INTO restore_events("
            "sequence,registry_id,batch_id,kind,previous_sha256,"
            "event_sha256,event_bytes"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                row["sequence"],
                row["registry_id"],
                row["batch_id"],
                row["kind"],
                row["previous_sha256"],
                row["event_sha256"],
                row["event_bytes"],
            ),
        )
    connection.execute(f"PRAGMA user_version = {USER_VERSION_V2}")
    _verify_schema(connection, expected_version=USER_VERSION_V2)


def _translate_sqlite(exc: BaseException) -> RestoreRegistryError:
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return RestoreRegistryBusy("registry正在写入，必须立即重新观察")
    return RestoreRegistryCorrupt(f"registry SQLite事实不可信：{type(exc).__name__}: {exc}")


class RestoreRegistry:
    """固定 Home 下的单 registry；生产入口不暴露可变路径参数。"""

    def __init__(self) -> None:
        self._initialize(_user_home())

    @classmethod
    def _for_home(cls, home: Path) -> RestoreRegistry:
        value = cls.__new__(cls)
        value._initialize(Path(home))
        return value

    def _initialize(self, home: Path) -> None:
        _reject_link(home, "home")
        try:
            self.home = home.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RestoreRegistryCorrupt(f"registry Home不可解析：{home}") from exc
        self.dot_directory = self.home / ".tianshu"
        self.authority_directory = self.dot_directory / "restore-registry-v1"
        self.path = self.authority_directory / "registry.db"

    def observe(self) -> RegistryObservation:
        home_identity = observe_entity(self.home, "directory")
        try:
            self.dot_directory.lstat()
        except FileNotFoundError:
            return AbsentRegistryObservation(
                False,
                self.home,
                self.authority_directory,
                home_identity,
            )
        except OSError as exc:
            raise RestoreRegistryCorrupt(f"registry固定父目录不可读：{self.dot_directory}") from exc
        _reject_link(self.dot_directory, "父目录")
        if not self.dot_directory.is_dir():
            raise RestoreRegistryCorrupt("registry固定父路径不是目录")
        init_residue = tuple(self.dot_directory.glob(".restore-registry-v1.init-*"))
        if init_residue:
            raise RestoreRegistryCorrupt("registry存在未发布初始化现场")
        try:
            self.authority_directory.lstat()
        except FileNotFoundError:
            return AbsentRegistryObservation(
                False,
                self.home,
                self.authority_directory,
                home_identity,
            )
        except OSError as exc:
            raise RestoreRegistryCorrupt(
                f"registry权威目录不可读：{self.authority_directory}"
            ) from exc
        _reject_link(self.authority_directory, "权威目录")
        if not self.authority_directory.is_dir():
            raise RestoreRegistryCorrupt("registry权威路径不是目录")
        try:
            entries = tuple(self.authority_directory.iterdir())
        except OSError as exc:
            raise RestoreRegistryCorrupt("registry权威目录不可枚举") from exc
        if not self.path.is_file() or self.path.is_symlink() or self.path.is_junction():
            raise RestoreRegistryCorrupt("registry目录存在但数据库缺失或不是普通文件")
        if any(entry != self.path for entry in entries):
            raise RestoreRegistryCorrupt("registry目录含未知文件、热journal或初始化残留")

        authority_identity = observe_entity(self.authority_directory, "directory")
        database_identity = observe_entity(self.path, "file")
        namespace = None
        entity = None
        connection = None
        try:
            namespace = acquire_database_access(self.path)
            entity = acquire_entity_access(database_identity)
            if observe_entity(self.path, "file") != database_identity:
                raise RestoreRegistryBusy("registry数据库实体在观察期间被替换")
            connection = _connect_readonly(self.path)
            connection.execute("BEGIN")
            schema_uv = _verify_schema(connection)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RestoreRegistryCorrupt("registry integrity_check未通过")
            meta_rows = tuple(connection.execute("SELECT * FROM registry_meta"))
            if len(meta_rows) != 1:
                raise RestoreRegistryCorrupt("registry meta必须恰有一行")
            row = meta_rows[0]
            meta_bytes = row["meta_bytes"]
            meta = RegistryMetaV1.from_bytes(meta_bytes)
            if (
                row["registry_id"] != meta.registry_id
                or row["meta_sha256"] != _sha256(meta_bytes)
            ):
                raise RestoreRegistryCorrupt("registry meta ID/hash字段与规范字节不符")
            expected_meta = _meta(
                meta.registry_id,
                self.home,
                self.authority_directory,
                self.path,
                home_identity=home_identity,
                authority_identity=authority_identity,
                database_identity=database_identity,
            )
            if meta != expected_meta:
                raise RestoreRegistryCorrupt("registry meta路径或实体身份已被替换")

            event_rows = tuple(
                connection.execute("SELECT * FROM restore_events ORDER BY sequence")
            )
            events: list[RestoreEventV1 | RestoreEventV2] = []
            previous = None
            for expected_sequence, event_row in enumerate(event_rows, 1):
                raw = event_row["event_bytes"]
                if schema_uv == USER_VERSION or event_row["kind"] not in (
                    "release_intent",
                    "released",
                ):
                    event: RestoreEventV1 | RestoreEventV2 = RestoreEventV1.from_bytes(raw)
                else:
                    event = RestoreEventV2.from_bytes(raw)
                event_sha = domain_digest("event", event)
                if (
                    event_row["sequence"] != expected_sequence
                    or event_row["registry_id"] != meta.registry_id
                    or event_row["batch_id"] != event.batch_id
                    or event_row["kind"] != event.kind
                    or event_row["previous_sha256"] != event.previous_sha256
                    or event_row["event_sha256"] != event_sha
                    or event.previous_sha256 != previous
                ):
                    raise RestoreRegistryCorrupt("registry事件行与其规范本体或前驱不符")
                events.append(event)
                previous = event_sha
            if schema_uv == USER_VERSION:
                validate_event_chain(tuple(events))
            else:
                validate_event_chain_v2(tuple(events))

            head = tuple(connection.execute("SELECT * FROM registry_head"))
            if len(head) != 1:
                raise RestoreRegistryCorrupt("registry head必须恰有一行")
            head_row = head[0]
            expected_sequence = len(events)
            if (
                head_row["sequence"] != expected_sequence
                or head_row["event_sha256"] != previous
            ):
                raise RestoreRegistryCorrupt("registry head不是完整已验证链尾")
            connection.execute("COMMIT")
            return PresentRegistryObservation(
                True,
                self.home,
                self.authority_directory,
                self.path,
                home_identity,
                authority_identity,
                database_identity,
                meta,
                meta_bytes,
                tuple(events),
                expected_sequence,
                previous,
            )
        except MaintenanceBusy as exc:
            raise RestoreRegistryBusy("registry维护锁正在使用中") from exc
        except RestoreRegistryError:
            raise
        except (sqlite3.Error, ValueError, TypeError, OSError) as exc:
            raise _translate_sqlite(exc) from exc
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                connection.close()
            if entity is not None:
                entity.close()
            if namespace is not None:
                namespace.close()

    def revalidate(self, expected: PresentRegistryObservation) -> None:
        """gate内短核：只验同实体、meta原字节和head，不替代完整链观察。"""

        if type(expected) is not PresentRegistryObservation or not expected.present:
            raise RestoreRegistryCorrupt("gate内重核需要present观察")
        namespace = None
        entity = None
        connection = None
        try:
            if observe_entity(self.home, "directory") != expected.home_identity:
                raise RestoreRegistryBusy("registry Home实体在观察后变化")
            if observe_entity(self.authority_directory, "directory") != expected.authority_identity:
                raise RestoreRegistryBusy("registry权威目录实体在观察后变化")
            if observe_entity(self.path, "file") != expected.database_identity:
                raise RestoreRegistryBusy("registry数据库实体在观察后变化")
            namespace = acquire_database_access(self.path)
            entity = acquire_entity_access(expected.database_identity)
            if observe_entity(self.path, "file") != expected.database_identity:
                raise RestoreRegistryBusy("registry数据库实体在重核期间被替换")
            connection = _connect_readonly(self.path)
            connection.execute("BEGIN")
            _verify_schema(connection)
            meta_rows = tuple(connection.execute("SELECT * FROM registry_meta"))
            if len(meta_rows) != 1:
                raise RestoreRegistryCorrupt("registry meta行数变化")
            row = meta_rows[0]
            if (
                row["registry_id"] != expected.meta.registry_id
                or row["meta_bytes"] != expected.meta_bytes
                or row["meta_sha256"] != _sha256(expected.meta_bytes)
            ):
                raise RestoreRegistryBusy("registry meta在观察后变化")
            head = connection.execute(
                "SELECT sequence,event_sha256 FROM registry_head"
            ).fetchone()
            if (
                head is None
                or head["sequence"] != expected.head_sequence
                or head["event_sha256"] != expected.head_sha256
            ):
                raise RestoreRegistryBusy("registry head在观察后变化")
            connection.execute("COMMIT")
        except MaintenanceBusy as exc:
            raise RestoreRegistryBusy("registry维护锁正在使用中") from exc
        except RestoreRegistryError:
            raise
        except (sqlite3.Error, ValueError, TypeError, OSError) as exc:
            raise _translate_sqlite(exc) from exc
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                connection.close()
            if entity is not None:
                entity.close()
            if namespace is not None:
                namespace.close()

    def initialize(self, *, registry_id: str | None = None) -> PresentRegistryObservation:
        current = self.observe()
        if current.present:
            return current
        gate = acquire_restore_gate(exclusive=True)
        try:
            return self.initialize_under_gate(gate, registry_id=registry_id)
        finally:
            gate.close()

    def initialize_under_gate(
        self,
        gate: DatabaseAccess,
        *,
        registry_id: str | None = None,
    ) -> PresentRegistryObservation:
        if (
            not isinstance(gate, DatabaseAccess)
            or not gate.active
            or not gate.authorizes_registry_write
        ):
            raise RestoreRegistryBusy("初始化 registry 必须持有活跃的独占 restore gate")
        current = self.observe()
        if current.present:
            return current
        selected_id = str(uuid.uuid4()) if registry_id is None else registry_id
        self._initialize_absent(selected_id)
        result = self.observe()
        if not result.present:
            raise RestoreRegistryCorrupt("registry初始化返回后仍不存在")
        return result

    def _initialize_absent(self, registry_id: str) -> None:
        if self.dot_directory.exists():
            _reject_link(self.dot_directory, "父目录")
            if not self.dot_directory.is_dir():
                raise RestoreRegistryCorrupt("registry固定父路径不是目录")
        else:
            try:
                self.dot_directory.mkdir(mode=0o700)
            except OSError as exc:
                raise RestoreRegistryCorrupt("无法创建registry固定父目录") from exc
        if os.name != "nt":
            try:
                self.dot_directory.chmod(0o700)
            except OSError as exc:
                raise RestoreRegistryCorrupt("无法收紧registry固定父目录权限") from exc

        temporary = self.dot_directory / f".restore-registry-v1.init-{uuid.uuid4()}"
        database = temporary / "registry.db"
        connection = None
        try:
            temporary.mkdir(mode=0o700)
            connection = _connect_write(database)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(REGISTRY_SCHEMA)
            if os.name != "nt":
                database.chmod(0o600)
            home_identity = observe_entity(self.home, "directory")
            authority_identity = observe_entity(temporary, "directory")
            database_identity = observe_entity(database, "file")
            meta = _meta(
                registry_id,
                self.home,
                self.authority_directory,
                self.path,
                home_identity=home_identity,
                authority_identity=authority_identity,
                database_identity=database_identity,
            )
            meta_bytes = meta.to_bytes()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO registry_meta(singleton,registry_id,meta_bytes,meta_sha256) "
                "VALUES(1,?,?,?)",
                (registry_id, meta_bytes, _sha256(meta_bytes)),
            )
            connection.execute(
                "INSERT INTO registry_head(singleton,sequence,event_sha256) VALUES(1,0,NULL)"
            )
            connection.execute("COMMIT")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RestoreRegistryCorrupt("新registry integrity_check未通过")
            connection.close()
            connection = None
            failpoint("restore-registry-init-before-file-fsync", path=database)
            _fsync_file(database)
            failpoint("restore-registry-init-before-publish", path=temporary)
            if self.authority_directory.exists() or self.authority_directory.is_symlink():
                raise RestoreRegistryCorrupt("registry发布前出现竞争权威")
            _publish_directory(temporary, self.authority_directory)
            failpoint("restore-registry-init-after-publish", path=self.authority_directory)
            _fsync_directory(self.dot_directory)
            failpoint("restore-registry-init-after-parent-fsync", path=self.dot_directory)
        except RestoreRegistryError:
            raise
        except (sqlite3.Error, ValueError, TypeError, OSError) as exc:
            raise _translate_sqlite(exc) from exc
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                connection.close()

    def append_event(
        self,
        event: RestoreEventV1 | RestoreEventV2,
        *,
        expected: PresentRegistryObservation,
        operation_access: DatabaseAccess | None,
    ) -> PresentRegistryObservation:
        if type(event) not in (RestoreEventV1, RestoreEventV2):
            raise RestoreRegistryCorrupt("registry只接受RestoreEventV1/V2")
        if not isinstance(operation_access, DatabaseAccess) or not operation_access.active:
            raise RestoreRegistryCorrupt("append前没有持续持有的操作独占")
        if not operation_access.authorizes_registry_write:
            raise RestoreRegistryBusy("append access不是restore gate或批次操作独占")
        if type(expected) is not PresentRegistryObservation or not expected.present:
            raise RestoreRegistryCorrupt("append必须绑定完整present观察")
        raw = event.to_bytes()
        event_sha = domain_digest("event", event)
        if event.registry_id != expected.meta.registry_id:
            raise RestoreRegistryCorrupt("待追加事件registry_id不符")

        namespace = None
        entity = None
        connection = None
        try:
            namespace = acquire_database_access(self.path, exclusive=True)
            entity = acquire_entity_access(expected.database_identity, exclusive=True)
            if observe_entity(self.home, "directory") != expected.home_identity:
                raise RestoreRegistryBusy("registry Home实体在观察后发生变化")
            if observe_entity(self.path, "file") != expected.database_identity:
                raise RestoreRegistryBusy("registry实体在观察后发生变化")
            connection = _connect_write(self.path)
            connection.execute("BEGIN IMMEDIATE")
            self._revalidate_in_transaction(connection, expected)
            duplicate = connection.execute(
                "SELECT event_bytes FROM restore_events WHERE batch_id=? AND kind=?",
                (event.batch_id, event.kind),
            ).fetchone()
            if duplicate is not None:
                if duplicate["event_bytes"] != raw:
                    raise RestoreRegistryCorrupt("同batch/kind已经绑定不同规范字节")
                connection.execute("COMMIT")
                # 重复追加是幂等成功：库内状态与 expected 完全一致。
                result = expected
            else:
                current_uv = connection.execute("PRAGMA user_version").fetchone()[0]
                if type(event) is RestoreEventV2 and event.kind in ("release_intent", "released"):
                    if current_uv == USER_VERSION:
                        _upgrade_v1_to_v2_in_transaction(connection)
                        current_uv = USER_VERSION_V2
                if current_uv == USER_VERSION and type(event) is RestoreEventV1:
                    validate_event_chain((*expected.events, event))
                else:
                    validate_event_chain_v2((*expected.events, event))
                connection.execute(
                    "INSERT INTO restore_events("
                    "sequence,registry_id,batch_id,kind,previous_sha256,"
                    "event_sha256,event_bytes"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        event.sequence,
                        event.registry_id,
                        event.batch_id,
                        event.kind,
                        event.previous_sha256,
                        event_sha,
                        raw,
                    ),
                )
                changed = connection.execute(
                    "UPDATE registry_head SET sequence=?,event_sha256=? "
                    "WHERE singleton=1 AND sequence=? AND event_sha256 IS ?",
                    (
                        event.sequence,
                        event_sha,
                        expected.head_sequence,
                        expected.head_sha256,
                    ),
                ).rowcount
                if changed != 1:
                    raise RestoreRegistryBusy("registry head CAS失败")
                connection.execute("COMMIT")
                # 提交后的状态由本事务决定：命名空间独占锁尚未释放，没有
                # 其他写入能插进来。**不要**在提交后再 observe()——那要再抢
                # 一次共享锁，会被并发写入方挡住，把已提交的成功误报成
                # Busy（issue #171：两个进程同时 append → 双方都 busy，
                # 但事件其实已经落盘）。取舍：提交后不再整库复检，既有行若
                # 被非协作写入方改成非规范字节，本调用不再当场发现，下一次
                # observe() 仍会稳定报 Corrupt。
                result = PresentRegistryObservation(
                    True,
                    self.home,
                    self.authority_directory,
                    self.path,
                    expected.home_identity,
                    expected.authority_identity,
                    expected.database_identity,
                    expected.meta,
                    expected.meta_bytes,
                    (*expected.events, event),
                    event.sequence,
                    event_sha,
                )
        except MaintenanceBusy as exc:
            raise RestoreRegistryBusy("registry维护锁正在使用中") from exc
        except NativeEntityError as exc:
            raise RestoreRegistryBusy("registry实体无法重核") from exc
        except RestoreRegistryError:
            raise
        except (sqlite3.Error, ValueError, TypeError, OSError) as exc:
            raise _translate_sqlite(exc) from exc
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                connection.close()
            if entity is not None:
                entity.close()
            if namespace is not None:
                namespace.close()
        return result

    def _revalidate_in_transaction(
        self,
        connection: sqlite3.Connection,
        expected: PresentRegistryObservation,
    ) -> None:
        _verify_schema(connection)
        meta_rows = tuple(connection.execute("SELECT * FROM registry_meta"))
        if len(meta_rows) != 1:
            raise RestoreRegistryCorrupt("registry meta行数变化")
        row = meta_rows[0]
        if (
            row["registry_id"] != expected.meta.registry_id
            or row["meta_bytes"] != expected.meta_bytes
            or row["meta_sha256"] != _sha256(expected.meta_bytes)
        ):
            raise RestoreRegistryBusy("registry meta在观察后变化")
        # 返回观察复用 expected.meta：这里补一条「字节→值」的一致性核对，
        # 使手工构造的 observation 也不能把与自身 meta_bytes 不符的 meta
        # 带进成功返回（合法路径下二者恒等，此检查不改变正常行为）。
        if RegistryMetaV1.from_bytes(expected.meta_bytes) != expected.meta:
            raise RestoreRegistryBusy("registry meta与观察字节不符")
        head = connection.execute("SELECT sequence,event_sha256 FROM registry_head").fetchone()
        if (
            head is None
            or head["sequence"] != expected.head_sequence
            or head["event_sha256"] != expected.head_sha256
        ):
            raise RestoreRegistryBusy("registry head在观察后变化")
        try:
            if observe_entity(self.home, "directory") != expected.home_identity:
                raise RestoreRegistryBusy("registry Home实体在观察后变化")
            if observe_entity(self.authority_directory, "directory") != expected.authority_identity:
                raise RestoreRegistryBusy("registry权威目录实体在观察后变化")
            if observe_entity(self.path, "file") != expected.database_identity:
                raise RestoreRegistryBusy("registry数据库实体在观察后变化")
        except NativeEntityError as exc:
            raise RestoreRegistryBusy("registry实体无法重核") from exc


def _fsync_file(path: Path) -> None:
    flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RestoreRegistryDurabilityUnavailable(f"registry DB文件fsync失败：{path}") from exc


def _publish_directory(temporary: Path, authority: Path) -> None:
    if os.name != "nt":
        os.replace(temporary, authority)
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel32.MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    movefile_write_through = 0x00000008
    if not move(str(temporary), str(authority), movefile_write_through):
        raise RestoreRegistryDurabilityUnavailable(
            f"registry目录MoveFileExW(WRITE_THROUGH)失败：{temporary}"
        )


def _fsync_directory(path: Path) -> None:
    """POSIX目录屏障；Windows把物理目录持久性保留为未证明边界。"""

    if os.name == "nt":  # pragma: no cover - native Windows only
        # Windows没有可由标准句柄证明的目录fsync等价原语。如果发布走
        # MoveFileExW(MOVEFILE_WRITE_THROUGH)，这里不能再把FlushFileBuffers
        # 的失败吞掉，也不能宣称掉电级目录持久性已经证明。
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RestoreRegistryDurabilityUnavailable(f"registry目录fsync失败：{path}") from exc


__all__ = [
    "AbsentRegistryObservation",
    "PresentRegistryObservation",
    "REGISTRY_SCHEMA",
    "REGISTRY_SCHEMA_V2",
    "RegistryObservation",
    "RestoreRegistry",
    "RestoreRegistryBusy",
    "RestoreRegistryCorrupt",
    "RestoreRegistryDurabilityUnavailable",
    "RestoreRegistryError",
]

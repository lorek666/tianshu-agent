
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    task_id      TEXT,
    type         TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    actor_kind   TEXT    NOT NULL,
    actor_id     TEXT    NOT NULL DEFAULT '',
    payload      TEXT,
    payload_ref  TEXT,
    payload_hash TEXT    NOT NULL,
    parent_seq   INTEGER,
    idem_key     TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);

-- 阻断回放按 type 过滤（`guard.scan_blocked`），而它在每次写入前都要跑一遍
-- （派发 phase-1、执行守卫内、改文件前各一次；复审 中-1 之后是三次）。没有
-- 这个索引时每次都要扫全表：事件越写越多，单次写入就越慢。索引不改变事件
-- 类型集合（C3 封闭集），也不是契约的一部分，只是让"读阻断状态"不随日志增长。
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, seq);

-- payload 版本旁表：与事件行同一事务写入，不改 events 表、不 UPDATE 历史行。
-- 旧库升级后这张表是空的，历史行读取时按 version=0（冻结前旧版）映射。
CREATE TABLE IF NOT EXISTS event_versions (
    seq     INTEGER PRIMARY KEY,
    type    TEXT    NOT NULL,
    version INTEGER NOT NULL
);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: DELETE forbidden');
END;
INSERT INTO events(run_id,type,ts,actor_kind,actor_id,payload,payload_hash) VALUES('legacy','run.start',1,'core','','{"task":"历史夹具","workspace":"/fixture","out":"x.md","limits":{},"tools":[]}','5d6058af79ebfba317a806062786a9f269350227e04a3b533a12e5b120b55651');
CREATE TABLE IF NOT EXISTS migration_log ( step INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
ALTER TABLE migration_log ADD COLUMN note TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_migration_log_applied ON migration_log(applied_at);
INSERT INTO migration_log(step,applied_at,note) VALUES(1,10,''),(2,20,'');
PRAGMA user_version=2;

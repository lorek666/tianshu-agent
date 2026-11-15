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

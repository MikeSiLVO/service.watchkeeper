"""Watchkeeper database schema and its version."""

import sqlite3

SCHEMA_VERSION = 2

STATE_GROUPS = (
    ("playcount_changed", ("playcount", "lastplayed")),
    ("resume_changed", ("resume_position", "resume_total", "resume_file")),
    ("userrating_changed", ("userrating",)),
)
STATE_FIELDS = tuple(field for stamp, fields in STATE_GROUPS for field in fields + (stamp,))
VALUE_FIELDS = tuple(field for stamp, fields in STATE_GROUPS for field in fields)

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS media (
        id           INTEGER PRIMARY KEY,
        media_type   TEXT    NOT NULL CHECK (media_type IN ('movie', 'tvshow', 'episode')),
        dbid         INTEGER,
        parent       INTEGER REFERENCES media(id),
        season       INTEGER,
        number       INTEGER,
        name         TEXT,
        year         INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS uniqueid (
        media_id   INTEGER NOT NULL REFERENCES media(id),
        media_type TEXT    NOT NULL,
        type       TEXT    NOT NULL,
        value      TEXT    NOT NULL,
        PRIMARY KEY (media_type, type, value)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS state (
        media_id           INTEGER PRIMARY KEY REFERENCES media(id),
        playcount          INTEGER,
        lastplayed         TEXT,
        playcount_changed  INTEGER,
        resume_position    REAL,
        resume_total       REAL,
        resume_file        TEXT,
        resume_changed     INTEGER,
        userrating         INTEGER,
        userrating_changed INTEGER,
        device             TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS history (
        id              INTEGER PRIMARY KEY,
        media_id        INTEGER NOT NULL REFERENCES media(id),
        field_group     TEXT    NOT NULL,
        playcount       INTEGER,
        lastplayed      TEXT,
        resume_position REAL,
        resume_total    REAL,
        resume_file     TEXT,
        userrating      INTEGER,
        source          TEXT    NOT NULL,
        batch           INTEGER,
        stamp           INTEGER NOT NULL,
        device          TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        stamp INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rebuilt (
        media_id INTEGER PRIMARY KEY REFERENCES media(id),
        stamp    INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_uniqueid_media ON uniqueid(media_id)",
    "CREATE INDEX IF NOT EXISTS idx_media_dbid ON media(media_type, dbid)",
    "CREATE INDEX IF NOT EXISTS idx_history_item ON history(media_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_history_batch ON history(batch)",
    "CREATE INDEX IF NOT EXISTS idx_media_episode ON media(parent, season, number)",
)


MIGRATIONS = {
    2: (
        "INSERT INTO sqlite_sequence (name, seq) "
        "SELECT 'batch', COALESCE(MAX(batch), 0) FROM history "
        "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'batch')",
        "ALTER TABLE state ADD COLUMN resume_file TEXT",
        "ALTER TABLE history ADD COLUMN resume_file TEXT",
    ),
}


def apply_schema(cursor: sqlite3.Cursor) -> None:
    """Apply every table and index the database needs, migrating an older one on the way."""
    found = schema_version(cursor)
    for statement in SCHEMA:
        cursor.execute(statement)
    if found:
        for version in range(found + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS.get(version, ()):
                cursor.execute(statement)
    if found != SCHEMA_VERSION:
        cursor.execute("PRAGMA user_version = {0}".format(SCHEMA_VERSION))


def schema_version(cursor: sqlite3.Cursor) -> int:
    """Version stamped on the database, 0 if it was never built."""
    cursor.execute("PRAGMA user_version")
    return cursor.fetchone()[0]

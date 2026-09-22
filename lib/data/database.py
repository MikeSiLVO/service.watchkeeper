"""Connection handling and queries against the watchkeeper database."""

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from lib.data.schema import (STATE_FIELDS, STATE_GROUPS, SCHEMA_VERSION, VALUE_FIELDS,
                             apply_schema, schema_version)

LOCK_TIMEOUT = 30.0
SNAPSHOTS_KEPT = 3
SNAPSHOT_SUFFIX = ".snapshot.db"

_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA journal_size_limit = 16777216",
    "PRAGMA cache_size = -16000",
    "PRAGMA foreign_keys = ON",
)

_connection = None
_lock = threading.Lock()
_path = ""
_stamp = 0


class DatabaseBusy(Exception):
    """Raised when the shared connection is not open or could not be claimed in time."""


class DatabaseTooNew(Exception):
    """Raised when the database was written by a later version of the addon."""


def open_database(path: str) -> None:
    """Open the database, building or migrating it, and refuse one a newer addon wrote."""
    global _connection, _path
    if _connection is not None:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=LOCK_TIMEOUT)
    cursor = connection.cursor()
    for pragma in _PRAGMAS:
        cursor.execute(pragma)
    found = schema_version(cursor)
    if found > SCHEMA_VERSION:
        connection.close()
        raise DatabaseTooNew(
            "database is version {0}, this addon knows {1}".format(found, SCHEMA_VERSION)
        )
    apply_schema(cursor)
    _seed_stamp(cursor)
    connection.commit()
    cursor.close()
    _connection, _path = connection, path


def _seed_stamp(cursor: sqlite3.Cursor) -> None:
    """Start this run's change stamps above the newest one already in the database."""
    global _stamp
    columns = ", ".join("MAX(COALESCE({0}, 0))".format(stamp) for stamp, _ in STATE_GROUPS)
    cursor.execute("SELECT MAX({0}) FROM state".format(columns))
    row = cursor.fetchone()
    _stamp = (row[0] or 0) if row else 0


def stamp_now() -> int:
    """The time now as a change stamp, always higher than the last one."""
    global _stamp
    _stamp = max(int(time.time()), _stamp + 1)
    return _stamp


def close_database() -> None:
    """Close the shared connection, which also clears the -wal file."""
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


@contextmanager
def get_db() -> Iterator[sqlite3.Cursor]:
    """Get a cursor on the shared connection, committing on success and rolling back on error."""
    if _connection is None:
        raise DatabaseBusy("database is not open")
    if not _lock.acquire(timeout=LOCK_TIMEOUT):
        raise DatabaseBusy("shared connection held for over {0} seconds".format(LOCK_TIMEOUT))
    cursor = _connection.cursor()
    try:
        yield cursor
    except Exception:
        _connection.rollback()
        raise
    else:
        _connection.commit()
    finally:
        cursor.close()
        _lock.release()


def new_connection() -> sqlite3.Connection:
    """Open a separate connection for a long loop that must not hold the shared lock."""
    connection = sqlite3.connect(_path, check_same_thread=False, timeout=LOCK_TIMEOUT)
    cursor = connection.cursor()
    for pragma in _PRAGMAS:
        cursor.execute(pragma)
    cursor.close()
    return connection


def read_state(cursor: sqlite3.Cursor, media_id: int) -> Dict[str, Any]:
    """Read every user field held for one item, empty when it has none."""
    columns = ("media_id",) + STATE_FIELDS
    cursor.execute(
        "SELECT {0} FROM state WHERE media_id = ?".format(", ".join(columns)), (media_id,)
    )
    row = cursor.fetchone()
    return dict(zip(columns, row)) if row else {}


def next_batch(cursor: sqlite3.Cursor) -> int:
    """Claim the next batch number and commit it, which groups one run's history rows."""
    cursor.execute("INSERT INTO batch (stamp) VALUES (?)", (stamp_now(),))
    cursor.execute("SELECT last_insert_rowid()")
    batch = cursor.fetchone()[0]
    cursor.connection.commit()
    return batch


def append_history(
    cursor: sqlite3.Cursor,
    media_id: int,
    group: str,
    values: tuple,
    stamp: int,
    device: str,
    source: str,
    batch: Optional[int] = None,
) -> None:
    """Append one field group's new values to the history, with their source and stamp."""
    fields = dict(STATE_GROUPS)[group]
    columns = ("media_id", "field_group") + fields + ("source", "batch", "stamp", "device")
    cursor.execute(
        "INSERT INTO history ({0}) VALUES ({1})".format(
            ", ".join(columns), ", ".join("?" * len(columns))
        ),
        (media_id, group) + tuple(values) + (source, batch, stamp, device),
    )


def record_prior_state(cursor: sqlite3.Cursor, media_id: int, group: str, values: tuple,
                       stored: Dict[str, Any], device: str) -> None:
    """Record what an item held before its first change, so that change can be undone."""
    was = tuple(stored.get(field) for field in dict(STATE_GROUPS)[group])
    if not stored.get(group) or was == tuple(values):
        return
    cursor.execute("SELECT 1 FROM history WHERE media_id = ? AND field_group = ? LIMIT 1",
                   (media_id, group))
    if cursor.fetchone():
        return
    append_history(cursor, media_id, group, was, stored[group], device, "held")


def record_group(
    cursor: sqlite3.Cursor,
    media_id: int,
    group: str,
    values: tuple,
    stamp: int,
    device: str,
    source: str = "capture",
    batch: Optional[int] = None,
    baseline: bool = False,
) -> bool:
    """Record one field group plus a history row, unless a later change or equal values are held."""
    fields = dict(STATE_GROUPS)[group]
    written = fields + (group, "device")
    columns = ("media_id",) + written
    cursor.execute(
        "INSERT INTO state ({0}) VALUES ({1}) ON CONFLICT(media_id) DO UPDATE SET {2} "
        "WHERE excluded.{3} > COALESCE({3}, 0) AND ({4})".format(
            ", ".join(columns),
            ", ".join("?" * len(columns)),
            ", ".join("{0} = excluded.{0}".format(name) for name in written),
            group,
            " OR ".join("{0} IS NOT excluded.{0}".format(name) for name in fields),
        ),
        (media_id,) + tuple(values) + (stamp, device),
    )
    if not cursor.rowcount:
        return False
    if not baseline:
        append_history(cursor, media_id, group, values, stamp, device, source, batch)
    return True


def mark_rebuilt(cursor: sqlite3.Cursor, kind: str, dbid: int, stamp: int) -> None:
    """Mark an item whose library row is gone, so what we hold now beats what Kodi reports."""
    cursor.execute(
        "INSERT OR REPLACE INTO rebuilt (media_id, stamp) "
        "SELECT id, ? FROM media WHERE media_type = ? AND dbid = ?",
        (stamp, kind, dbid),
    )


def is_rebuilt(cursor: sqlite3.Cursor, media_id: int) -> bool:
    """True while this item is waiting to be filled back after a rebuild."""
    cursor.execute("SELECT 1 FROM rebuilt WHERE media_id = ?", (media_id,))
    return cursor.fetchone() is not None


def count_rebuilt(cursor: sqlite3.Cursor) -> int:
    """Count the items a rebuild marked and nothing has filled back yet."""
    cursor.execute("SELECT COUNT(*) FROM rebuilt")
    return cursor.fetchone()[0]


def item_history(cursor: sqlite3.Cursor, media_id: int, limit: int = 50) -> list:
    """List an item's recorded changes, oldest first."""
    cursor.execute(
        "SELECT id, field_group, {0}, source, stamp FROM history "
        "WHERE media_id = ? ORDER BY id DESC LIMIT ?".format(", ".join(VALUE_FIELDS)),
        (media_id, limit),
    )
    return list(reversed(cursor.fetchall()))


def group_fields(group: str, values: tuple) -> Dict[str, Any]:
    """Pick one field group's values out of a history row, as a dict keyed by field name."""
    offset = 0
    for stamp, fields in STATE_GROUPS:
        if stamp == group:
            return dict(zip(fields, values[offset:offset + len(fields)]))
        offset += len(fields)
    return {}


def undoable_restores(cursor: sqlite3.Cursor, limit: int = 20) -> list:
    """List the restores we could still undo, newest first, with how many items each touched."""
    cursor.execute(
        "SELECT batch, COUNT(DISTINCT media_id), MIN(stamp) FROM history "
        "WHERE batch IS NOT NULL AND source = 'library' "
        "GROUP BY batch ORDER BY batch DESC LIMIT ?",
        (limit,),
    )
    return cursor.fetchall()


def batch_rows(cursor: sqlite3.Cursor, batch: int, source: str = "library") -> list:
    """List one side of a restore: what the library held before, or what we wrote."""
    cursor.execute(
        "SELECT media_id, field_group, {0} FROM history "
        "WHERE batch = ? AND source = ? ORDER BY id".format(", ".join(VALUE_FIELDS)),
        (batch, source),
    )
    return cursor.fetchall()


def media_row(cursor: sqlite3.Cursor, media_id: int) -> Optional[tuple]:
    """Read an item's media type and the library id we last saw it under."""
    cursor.execute("SELECT media_type, dbid FROM media WHERE id = ?", (media_id,))
    return cursor.fetchone()


def note_dbid(cursor: sqlite3.Cursor, media_id: int, dbid: int) -> None:
    """Keep an item's library id current, which is all a removal announcement carries."""
    cursor.execute(
        "UPDATE media SET dbid = ? WHERE id = ? AND (dbid IS NULL OR dbid <> ?)",
        (dbid, media_id, dbid),
    )


def clear_rebuilt(cursor: sqlite3.Cursor, media_id: int) -> None:
    """Forget an item's rebuild mark, once Kodi holds data for it again."""
    cursor.execute("DELETE FROM rebuilt WHERE media_id = ?", (media_id,))


def holdings() -> Dict[str, int]:
    """Count records by media type, plus how many have a play count."""
    with get_db() as cursor:
        cursor.execute("SELECT media_type, COUNT(*) FROM media GROUP BY media_type")
        counts = dict(cursor.fetchall())
        cursor.execute("SELECT COUNT(*) FROM state WHERE playcount IS NOT NULL")
        counts["watched"] = cursor.fetchone()[0]
    return counts


def snapshot(keep: int = SNAPSHOTS_KEPT, force: bool = False) -> str:
    """Copy the database to a dated file beside it, unless the newest copy already matches."""
    if _connection is None:
        raise DatabaseBusy("database is not open")
    recent = snapshots()
    if recent and not force and _fingerprint(recent[0]) == _fingerprint(_path):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_path = "{0}.{1}{2}".format(os.path.splitext(_path)[0], stamp, SNAPSHOT_SUFFIX)
    with _lock:
        target = sqlite3.connect(target_path)
        try:
            _connection.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
        finally:
            target.close()
    _discard_old_snapshots(keep)
    return target_path


def snapshots() -> list:
    """List the snapshot files beside the database, newest first."""
    import glob

    pattern = "{0}.*{1}".format(os.path.splitext(_path)[0], SNAPSHOT_SUFFIX)
    return sorted(glob.glob(pattern), reverse=True)


def _discard_old_snapshots(keep: int) -> None:
    """Discard the oldest snapshots over the limit."""
    for stale in snapshots()[max(keep, 1):]:
        for path in (stale, stale + "-wal", stale + "-shm"):
            try:
                os.remove(path)
            except OSError:
                pass


def _fingerprint(path: str) -> str:
    """Fingerprint the user data in a database file, ignoring everything else."""
    import hashlib

    connection = sqlite3.connect("file:{0}?mode=ro".format(path), uri=True)
    try:
        rows = connection.execute(
            "SELECT media_id, {0} FROM state ORDER BY media_id".format(", ".join(VALUE_FIELDS))
        ).fetchall()
    except sqlite3.Error:
        return ""
    finally:
        connection.close()
    fingerprint = hashlib.sha1()
    for row in rows:
        fingerprint.update(repr(row).encode("utf-8"))
    return fingerprint.hexdigest()

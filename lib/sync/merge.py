"""Merge another install's saved data into this one."""

import sqlite3
from typing import Any, Dict, Optional

from lib.data.database import read_state, record_group, record_prior_state
from lib.data.schema import STATE_FIELDS, STATE_GROUPS
from lib.kodi.client import log
from lib.kodi.utilities import device_id
from lib.sync.matcher import (find_by_ids, resolve_episode, resolve_movie,
                              resolve_show)


def merge_database(cursor: Any, path: str, batch: Optional[int] = None) -> Dict[str, int]:
    """Take what another install recorded later than we did, item by item."""
    shows, held, ids = _read_store(path)
    counts = {"items": 0, "changed": 0, "new": 0}
    ours: Dict[int, int] = {}
    ours_device = device_id()
    for media_id, name, year in shows:
        here = resolve_show(cursor, ids.get(media_id) or {}, name or "", year)
        if here is not None:
            ours[media_id] = here
    for row in held:
        theirs = dict(zip(STATE_FIELDS + ("device",), row[7:]))
        their_ids = ids.get(row[0]) or {}
        if not _holds_anything(theirs) and find_by_ids(cursor, row[1], their_ids) is None:
            continue
        media_id = _resolve_row(cursor, row, their_ids, ours)
        if media_id is None:
            continue
        ours[row[0]] = media_id
        counts["items"] += 1
        stored = read_state(cursor, media_id)
        if not stored:
            counts["new"] += 1
        for group, fields in STATE_GROUPS:
            stamp = theirs.get(group)
            if not stamp:
                continue
            values = tuple(theirs.get(field) for field in fields)
            record_prior_state(cursor, media_id, group, values, stored, ours_device)
            if record_group(cursor, media_id, group, values, stamp,
                            theirs.get("device") or "", "import", batch):
                counts["changed"] += 1
    log("merged {0} from {1}".format(counts, path))
    return counts


def _holds_anything(theirs: Dict[str, Any]) -> bool:
    """True when a record from another install carries any user data."""
    return bool(theirs.get("playcount") or theirs.get("lastplayed")
                or theirs.get("resume_position") or theirs.get("userrating"))


def _read_store(path: str) -> tuple:
    """Read what another install holds: its shows, then every record carrying data."""
    connection = sqlite3.connect("file:{0}?mode=ro".format(path), uri=True)
    try:
        shows = connection.execute(
            "SELECT id, name, year FROM media WHERE media_type = 'tvshow' ORDER BY id"
        ).fetchall()
        present = {row[1] for row in connection.execute("PRAGMA table_info(state)")}
        columns = ", ".join("s." + field if field in present else "NULL AS " + field
                            for field in STATE_FIELDS + ("device",))
        held = connection.execute(
            "SELECT m.id, m.media_type, m.parent, m.season, m.number, m.name, m.year, {0} "
            "FROM media m JOIN state s ON s.media_id = m.id ORDER BY m.id".format(columns)
        ).fetchall()
        ids: Dict[int, Dict[str, str]] = {}
        for media_id, kind, value in connection.execute(
                "SELECT media_id, type, value FROM uniqueid"):
            ids.setdefault(media_id, {})[kind] = value
        return shows, held, ids
    finally:
        connection.close()


def _resolve_row(cursor: Any, row: tuple, ids: Dict[str, str],
                 ours: Dict[int, int]) -> Optional[int]:
    """Resolve one of their rows to a record here, adding one when it is new to us."""
    kind, parent, season, number, name, year = row[1:7]
    if kind == "movie":
        return resolve_movie(cursor, ids, name or "", year)
    if kind == "tvshow":
        return resolve_show(cursor, ids, name or "", year)
    parent_id = ours.get(parent) if parent else None
    if parent_id is None:
        return None
    return resolve_episode(cursor, parent_id, ids, season, number, name or "")

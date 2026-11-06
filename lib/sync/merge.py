"""Merge another install's saved data into this one."""

import os
import sqlite3
import tempfile
from typing import Any, Dict, Optional, Tuple

import xbmcvfs

from lib.data.database import advance_stamp, read_state, record_group, record_prior_state
from lib.data.schema import STATE_FIELDS, STATE_GROUPS
from lib.kodi.client import log
from lib.kodi.utilities import database_path, device_id
from lib.sync.matcher import (find_by_ids, resolve_episode, resolve_movie,
                              resolve_show)

GROUPS = tuple(group for group, _ in STATE_GROUPS)
_SEEN = ("EXISTS (SELECT 1 FROM history h WHERE h.media_id = m.id AND h.field_group = '{0}' "
         "AND h.stamp = s.{0} AND h.source <> 'held')")


def merge_database(cursor: Any, path: str,
                   batch: Optional[int] = None) -> Tuple[Dict[str, int], Dict[int, dict]]:
    """Take what another install recorded later than we did; return what each change replaced."""
    shows, held, ids = _read_store(path)
    counts = {"items": 0, "changed": 0, "new": 0}
    before: Dict[int, dict] = {}
    ours: Dict[int, int] = {}
    ours_device = device_id()
    newest = 0
    width = len(STATE_FIELDS) + 1
    for media_id, name, year in shows:
        here = resolve_show(cursor, ids.get(media_id) or {}, name or "", year)
        if here is not None:
            ours[media_id] = here
    for row in held:
        theirs = dict(zip(STATE_FIELDS + ("device",), row[7:7 + width]))
        seen = dict(zip(GROUPS, row[7 + width:]))
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
            # a state they only found on first look never replaces one we hold
            if not stamp or (not seen[group] and stored.get(group)):
                continue
            values = tuple(theirs.get(field) for field in fields)
            record_prior_state(cursor, media_id, group, values, stored, ours_device)
            if record_group(cursor, media_id, group, values, stamp, theirs.get("device") or "",
                            "import", batch, baseline=not seen[group]):
                counts["changed"] += 1
                newest = max(newest, stamp)
                before.setdefault(media_id, {}).setdefault(
                    group, tuple(stored.get(field) for field in fields))
    advance_stamp(newest)
    return counts, before


def local_copy(path: str) -> str:
    """Copy a file into the addon folder under a name of its own, or return empty on failure."""
    handle, target = tempfile.mkstemp(".db", "incoming-", os.path.dirname(database_path()))
    os.close(handle)
    if not xbmcvfs.copy(path, target):
        log("could not copy {0} into the addon folder".format(path))
        discard(target)
        return ""
    return target


def discard(path: str) -> None:
    """Discard a working copy, and say nothing when it was never there."""
    try:
        os.remove(path)
    except OSError:
        pass


def _holds_anything(theirs: Dict[str, Any]) -> bool:
    """True when a record from another install carries any user data."""
    return bool(theirs.get("playcount") or theirs.get("lastplayed")
                or theirs.get("resume_position") or theirs.get("userrating"))


def _read_store(path: str) -> tuple:
    """Read what another install holds: its shows, then every record with what it saw change."""
    connection = sqlite3.connect("file:{0}?mode=ro".format(path), uri=True)
    try:
        shows = connection.execute(
            "SELECT id, name, year FROM media WHERE media_type = 'tvshow' ORDER BY id"
        ).fetchall()
        present = {row[1] for row in connection.execute("PRAGMA table_info(state)")}
        columns = ", ".join("s." + field if field in present else "NULL AS " + field
                            for field in STATE_FIELDS + ("device",))
        seen = ", ".join(_SEEN.format(group) for group in GROUPS)
        held = connection.execute(
            "SELECT m.id, m.media_type, m.parent, m.season, m.number, m.name, m.year, {0}, {1} "
            "FROM media m JOIN state s ON s.media_id = m.id ORDER BY m.id".format(columns, seen)
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

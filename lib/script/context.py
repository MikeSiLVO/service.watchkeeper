"""Restore one movie or show from the context menu."""

import json
import sqlite3
import sys
from typing import Any, Dict, List, Tuple

import xbmc

from lib.data.database import (close_database, get_db, group_fields, item_history, next_batch,
                               open_database)
from lib.data.schema import VALUE_FIELDS
from lib.kodi.client import log
from lib.kodi.dialogs import (addon_name, choose, confirm, describe_state, kodi_text, notify,
                              progress, text, when)
from lib.kodi.slot import WAIT, SlotTaken, claim
from lib.kodi.utilities import database_path
from lib.sync.backup import read_details
from lib.sync.restore import find_record, plan, restore_item, restore_show, restore_state

WRITING = "the context menu"


def main() -> None:
    """Restore the item the menu was opened on, after asking."""
    kind, dbid = _chosen_item()
    log("context menu on {0} dbid {1}".format(kind or "nothing", dbid))
    if not kind or not dbid:
        notify(text(32016))
        return
    if not confirm(addon_name(), text(32017)):
        return
    try:
        with claim(WRITING, WAIT):
            _restore_one(kind, dbid)
    except SlotTaken as busy:
        log("not restoring, {0} is writing".format(busy))
        notify(text(32045))


def _restore_one(kind: str, dbid: int) -> None:
    """Put back what is held for one item, then offer any earlier state it had."""
    try:
        open_database(database_path())
        with get_db() as cursor:
            written = _restore_held(cursor, kind, dbid, next_batch(cursor))
        offered, restored = _restore_earlier(kind, dbid)
        written += restored
    except sqlite3.OperationalError as error:
        log("database locked: {0}".format(error))
        notify(text(32045))
        return
    finally:
        close_database()
    if written:
        notify(text(32018))
    elif not offered:
        notify(text(32019))


def _restore_held(cursor: Any, kind: str, dbid: int, batch: int) -> int:
    """Restore this item from what we hold, counting a show as the episodes under it."""
    if kind != "tvshow":
        return 1 if restore_item(cursor, kind, dbid, False, batch) else 0
    with progress(text(32007)) as bar:
        return restore_show(cursor, dbid, False, batch, bar.step)["restored"]


def _restore_earlier(kind: str, dbid: int) -> Tuple[bool, int]:
    """Restore this item to a state it held before, saying whether one was offered at all."""
    details = read_details(kind, dbid)
    if not details:
        return False, 0
    with get_db() as cursor:
        offers = _offers(cursor, kind, details)
    if not offers:
        return False, 0
    labels = ["{0}   {1}".format(when(stamp), holds) for stamp, state, holds in offers]
    chosen = choose(text(32026), labels + [kodi_text(222)])
    if chosen < 0 or chosen >= len(offers):
        return True, 0
    stamp, state, holds = offers[chosen]
    log("restoring {0} dbid {1} to how it was at {2}".format(kind, dbid, when(stamp)))
    with get_db() as cursor:
        written = restore_state(cursor, kind, details, state, next_batch(cursor))
    return True, 1 if written else 0


def _offers(cursor: Any, kind: str,
            details: Dict[str, Any]) -> List[Tuple[int, Dict[str, Any], str]]:
    """Each earlier state of this item, with what putting it back would write."""
    media_id = find_record(cursor, kind, details)
    if media_id is None:
        return []
    offers: List[Tuple[int, Dict[str, Any], str]] = []
    seen = set()
    for stamp, state in reversed(_states(item_history(cursor, media_id))):
        changes = plan(state, details, True)
        if kind == "tvshow":
            changes.pop("resume", None)
        outcome = json.dumps(changes, sort_keys=True, default=str)
        if changes and outcome not in seen:
            seen.add(outcome)
            offers.append((stamp, state, _label(changes)))
    return offers


def _states(rows: List[tuple]) -> List[Tuple[int, Dict[str, Any]]]:
    """List the state an item was in after each of its recorded changes."""
    holding: Dict[str, Any] = {}
    for row in reversed(rows):
        holding.update(_row_state(row))
    states = []
    for row in rows:
        holding.update(_row_state(row))
        states.append((row[-1], dict(holding)))
    return states


def _row_state(row: tuple) -> Dict[str, Any]:
    """Read one history row as the state fields of its group."""
    return group_fields(row[1], row[2:2 + len(VALUE_FIELDS)])


def _label(changes: Dict[str, Any]) -> str:
    """Describe what putting a state back would write, for a picker label."""
    resume = (changes.get("resume") or {}).get("position")
    return describe_state(changes.get("playcount"), changes.get("lastplayed"), resume,
                          changes.get("userrating")) or text(32034)


def _chosen_item() -> Tuple[str, int]:
    """Name the library item the context menu was opened on, from Kodi's own infolabels."""
    kind = xbmc.getInfoLabel("ListItem.DBType")
    dbid = xbmc.getInfoLabel("ListItem.DBID")
    if len(sys.argv) > 2:
        kind = sys.argv[1] or kind
        dbid = sys.argv[2] or dbid
    try:
        return kind, int(dbid)
    except (TypeError, ValueError):
        return kind, 0

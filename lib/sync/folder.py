"""Keep installs in step through a folder they share, each writing a file of its own."""

import os
import sqlite3
import time
from typing import Callable, Dict, Tuple

import xbmcaddon
import xbmcvfs

from lib.data.database import (copy_database, data_version, holds_same, new_connection,
                               next_batch)
from lib.kodi.client import ADDON_ID, log
from lib.kodi.slot import SlotTaken, claim, holder, stopping
from lib.kodi.utilities import database_path, device_id
from lib.sync.restore import restore_merged

CHECK_INTERVAL = 60.0
WRITE_DELAY = 5.0
PREFIX = "watchkeeper-sync-"
SUFFIX = ".db"
OUTGOING_NAME = "outgoing.db"
SYNCING = "the sync"

_folder = ""
_next_check = 0.0
_write_at = 0.0
_version = 0
_seen: Dict[str, Tuple[int, int]] = {}
_before: Dict[int, dict] = {}


def note_change() -> None:
    """Note a change to our data, moving our file's next write a few seconds out."""
    global _write_at
    _write_at = time.time() + WRITE_DELAY


def keep_in_step(should_stop: Callable[[], bool]) -> None:
    """Keep in step with the other installs, reading, restoring and writing as each falls due."""
    global _folder, _next_check, _write_at, _version
    now = time.time()
    if now >= _next_check:
        _next_check = now + CHECK_INTERVAL
        folder = _shared_folder()
        if folder != _folder:
            _folder, _write_at = folder, now
            _seen.clear()
            _before.clear()
        if _folder:
            _read_theirs(_folder)
            version = data_version()
            # the Programs script and context menu write the store without telling us
            if version != _version:
                _version, _write_at = version, _write_at or now
    if not _folder:
        return
    if _before and not should_stop() and not holder():
        _restore_theirs(should_stop)
    if _write_at and now >= _write_at:
        _write_at = 0.0 if _write_ours(_folder) else now + CHECK_INTERVAL


def _shared_folder() -> str:
    """Read the folder the installs share, or empty while syncing is off."""
    addon = xbmcaddon.Addon(ADDON_ID)
    if not addon.getSettingBool("sync"):
        return ""
    return xbmcvfs.translatePath(addon.getSettingString("sync_folder"))


def _read_theirs(folder: str) -> None:
    """Read each other install's file that changed since we last read it, and merge it."""
    ours = PREFIX + device_id() + SUFFIX
    _, files = xbmcvfs.listdir(folder)
    for name in files:
        if not name.startswith(PREFIX) or not name.endswith(SUFFIX) or name == ours:
            continue
        path = os.path.join(folder, name)
        stat = xbmcvfs.Stat(path)
        mark = (stat.st_size(), stat.st_mtime())
        if _seen.get(name) != mark and _merge_file(path):
            _seen[name] = mark


def _merge_file(path: str) -> bool:
    """Merge one install's file and hold what it changed for restoring; False if it never copied."""
    from lib.sync.merge import discard, local_copy, merge_database

    incoming = local_copy(path)
    if not incoming:
        return False
    connection = new_connection()
    try:
        cursor = connection.cursor()
        counts, before = merge_database(cursor, incoming, next_batch(cursor))
        connection.commit()
    except sqlite3.Error as error:
        log("could not merge {0}: {1!r}".format(path, error))
        return True
    finally:
        connection.close()
        discard(incoming)
    log("merged {0} changes across {1} items, {2} of them new to us, from {3}".format(
        counts["changed"], counts["items"], counts["new"], path))
    for media_id, groups in before.items():
        held = _before.setdefault(media_id, {})
        for group, values in groups.items():
            held.setdefault(group, values)
    if counts["changed"]:
        note_change()
    return True


def _restore_theirs(should_stop: Callable[[], bool]) -> None:
    """Restore what the other installs changed; anything not reached waits for the next pass."""
    connection = new_connection()
    try:
        with claim(SYNCING):
            cursor = connection.cursor()
            counts = restore_merged(cursor, _before, next_batch(cursor),
                                    lambda *_: not (should_stop() or stopping()))
            connection.commit()
        if counts["checked"] == len(_before):
            _before.clear()
    except SlotTaken as taken:
        log("not restoring merged changes, {0} is writing".format(taken))
    except sqlite3.OperationalError:
        log("database locked, restoring merged changes later")
    finally:
        connection.close()


def _write_ours(folder: str) -> bool:
    """Write our file into the shared folder unless it already holds the same; False on failure."""
    outgoing = os.path.join(os.path.dirname(database_path()), OUTGOING_NAME)
    target = os.path.join(folder, PREFIX + device_id() + SUFFIX)
    if holds_same(outgoing) and xbmcvfs.exists(target):
        return True
    copy_database(outgoing)
    part = target + ".part"
    if xbmcvfs.copy(outgoing, part):
        xbmcvfs.delete(target)
        if xbmcvfs.rename(part, target):
            log("wrote our saved data to {0}".format(target))
            return True
    log("could not write {0}, trying again later".format(target))
    xbmcvfs.delete(outgoing)
    return False

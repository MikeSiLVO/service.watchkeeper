"""Run the background service, capturing user data as Kodi announces it."""

import os
import sqlite3
import time
from queue import Empty, Queue
from typing import Callable

import xbmc

from lib.data.database import (DatabaseBusy, close_database, count_rebuilt, get_db, holdings,
                               mark_rebuilt, new_connection, next_batch, open_database,
                               snapshot, stamp_now)
from lib.kodi.client import ADDON, Listener, log
from lib.kodi.dialogs import BackgroundProgress, addon_name, notify, text
from lib.kodi.slot import SlotTaken, claim, holder, stopping
from lib.kodi.utilities import database_path, wait_for_kodi_ready
from lib.sync.backup import (backup_item, library_settled, announced_item,
                             removed_item, sweep)
from lib.sync.restore import restore_library

DRAIN_INTERVAL = 1.0
BACKLOG_INTERVAL = 0.1
DRAIN_BATCH = 50
SWEEP_INTERVAL = 6 * 60 * 60
FILL_WAIT = 2 * 60
BROWSING = "Window.IsActive(videos)"
FILLING = "the automatic fill"

_pending = {}
_settled = False
_waiting = 0.0
_rebuilding = False
_next_sweep = 0.0


def main() -> None:
    """Open the database and capture announcements until Kodi asks the service to stop."""
    global _settled, _next_sweep
    path = database_path()
    open_database(path)
    held = holdings()
    log("opened {0}, holding {1}".format(path, held))
    queue = Queue()
    listener = Listener(queue)
    try:
        if not wait_for_kodi_ready(listener):
            return
        _settled = any(count for kind, count in held.items() if kind != "watched")
        _next_sweep = _last_swept() + SWEEP_INTERVAL
        log("watching for changes")
        while not listener.abortRequested():
            if listener.waitForAbort(BACKLOG_INTERVAL if _pending else DRAIN_INTERVAL):
                break
            try:
                drain(queue)
                if fill_due():
                    run_fill()
                if sweep_due():
                    run_sweep(listener.abortRequested)
            except Exception:
                import traceback
                log("drain failed\n" + traceback.format_exc(), xbmc.LOGERROR)
    finally:
        close_database()
        log("stopped")


def drain(queue: Queue) -> None:
    """Drain the queue: mark removals, note the library settling, capture up to a batch of items."""
    global _rebuilding, _settled
    removals = []
    while True:
        try:
            method, data = queue.get_nowait()
        except Empty:
            break
        item = announced_item(method, data)
        if item is not None:
            _pending[item] = None
        gone = removed_item(method, data)
        if gone is not None:
            removals.append(gone)
        if library_settled(method):
            _settled = True
    if not _pending and not removals:
        return
    shows = {}
    try:
        with get_db() as cursor:
            if removals:
                when = stamp_now()
                for kind, dbid in removals:
                    mark_rebuilt(cursor, kind, dbid, when)
                _rebuilding = True
                log("{0} items left the library, holding their data".format(len(removals)))
            for _ in range(min(len(_pending), DRAIN_BATCH)):
                kind, dbid = next(iter(_pending))
                try:
                    backup_item(cursor, kind, dbid, shows=shows)
                    cursor.connection.commit()
                except sqlite3.OperationalError:
                    cursor.connection.rollback()
                    log("database locked, {0} items held for the next pass".format(len(_pending)))
                    return
                except Exception:
                    import traceback
                    log("could not capture {0} dbid {1}\n{2}".format(
                        kind, dbid, traceback.format_exc()), xbmc.LOGERROR)
                del _pending[(kind, dbid)]
    except DatabaseBusy:
        log("database busy, {0} items held for the next pass".format(len(_pending)))


def fill_due() -> bool:
    """True once the library settled, nobody else is writing, and no video window is in the way."""
    global _waiting
    if not _settled or xbmc.getCondVisibility("Library.IsScanningVideo"):
        return False
    if holder():
        return False
    if not xbmc.getCondVisibility(BROWSING):
        _waiting = 0.0
        return True
    if _rebuilding:
        return True
    if not _waiting:
        _waiting = time.time()
        log("holding the fill back, a video window is open")
        notify(text(32044))
        return False
    if time.time() - _waiting < FILL_WAIT:
        return False
    _waiting = 0.0
    return True


def run_fill() -> None:
    """Fill in what the library is missing, unless another run is writing."""
    global _rebuilding, _settled
    try:
        with claim(FILLING):
            _settled = False
            _rebuilding = False
            _fill()
            if stopping():
                _settled = True
    except SlotTaken as taken:
        log("not filling, {0} is writing".format(taken))
    except sqlite3.OperationalError:
        _settled = True
        log("database locked, filling later")


def _fill() -> None:
    """Walk the library once on its own connection, never overwriting what Kodi holds."""
    bar = BackgroundProgress(addon_name(), text(32007))
    connection = new_connection()
    try:
        cursor = connection.cursor()
        marked = count_rebuilt(cursor)
        counts = restore_library(cursor, bar.step, batch=next_batch(cursor), filling=True,
                                 on_restore=bar.start)
        connection.commit()
        log("filled {0}, {1} of them marked rebuilt".format(counts, marked))
    finally:
        bar.close()
        connection.close()


def sweep_due() -> bool:
    """True when a sweep is overdue and nothing is playing."""
    return time.time() >= _next_sweep and not xbmc.getCondVisibility("Player.Playing")


def run_sweep(should_stop: Callable[[], bool]) -> None:
    """Sweep the library on its own connection, snapshot, and fill if the sweep saw a rebuild."""
    global _next_sweep, _settled
    _next_sweep = time.time() + SWEEP_INTERVAL
    connection = new_connection()
    try:
        cursor = connection.cursor()
        counts = sweep(cursor, should_stop=should_stop)
        connection.commit()
    finally:
        connection.close()
    if should_stop():
        return
    ADDON.setSetting("last_swept", str(int(time.time())))
    taken = snapshot()
    log("swept {0}, snapshot {1}".format(counts, os.path.basename(taken) or "none needed"))
    if counts["moved"]:
        # a rebuild on another client sharing the library announces nothing here
        _settled = True


def _last_swept() -> float:
    """When the last sweep finished, as a Unix time, or zero when none has."""
    try:
        return float(ADDON.getSetting("last_swept") or 0)
    except ValueError:
        return 0.0

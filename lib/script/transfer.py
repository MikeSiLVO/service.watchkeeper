"""Move saved data between installs, in either direction."""

import os
import sqlite3
import time

import xbmcvfs

from lib.data.database import get_db, next_batch, snapshot
from lib.kodi.client import log
from lib.kodi.dialogs import choose_file, choose_folder, confirm, notify, text
from lib.kodi.utilities import database_path
from lib.sync.merge import merge_database

TRANSFER_NAME = "watchkeeper-{0}.db"
INCOMING_NAME = "incoming.db"


def export_data() -> None:
    """Copy everything we hold to a file the user picks, to carry to another install."""
    folder = choose_folder(text(32037))
    if not folder:
        return
    taken = snapshot(force=True)
    target = os.path.join(xbmcvfs.translatePath(folder),
                          TRANSFER_NAME.format(time.strftime("%Y%m%d-%H%M%S")))
    if not xbmcvfs.copy(taken, target):
        log("could not copy {0} to {1}".format(taken, target))
        notify(text(32046).format(target))
        return
    log("exported to {0}".format(target))
    notify(text(32041).format(os.path.basename(target)))


def import_data() -> None:
    """Merge saved data from a file the user picks, keeping whichever side changed later."""
    picked = choose_file(text(32039), ".db")
    if not picked:
        return
    if not confirm(text(32038), text(32040)):
        return
    incoming = _local_copy(picked)
    if not incoming:
        notify(text(32043))
        return
    try:
        with get_db() as cursor:
            counts = merge_database(cursor, incoming, next_batch(cursor))
    except sqlite3.Error as error:
        log("{0} is not a watchkeeper backup: {1!r}".format(picked, error))
        notify(text(32043))
        return
    finally:
        _discard(incoming)
    notify(text(32042).format(counts["changed"], counts["items"]))


def _local_copy(picked: str) -> str:
    """Copy a chosen file next to our own, since SQLite cannot open one across the network."""
    target = os.path.join(os.path.dirname(database_path()), INCOMING_NAME)
    _discard(target)
    if not xbmcvfs.copy(picked, target):
        log("could not copy {0} here".format(picked))
        return ""
    return target


def _discard(path: str) -> None:
    """Delete a working copy, and say nothing when it was never there."""
    try:
        os.remove(path)
    except OSError:
        pass

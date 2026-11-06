"""Move saved data between installs, in either direction."""

import os
import sqlite3
import time

import xbmcvfs

from lib.data.database import get_db, next_batch, snapshot
from lib.kodi.client import log
from lib.kodi.dialogs import choose_file, choose_folder, confirm, notify, progress, text
from lib.kodi.slot import WAIT, SlotTaken, claim
from lib.sync.merge import discard, local_copy, merge_database
from lib.sync.restore import restore_merged

TRANSFER_NAME = "watchkeeper-{0}.db"
IMPORTING = "the import"


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
    """Merge saved data from a file the user picks, then restore what changed into the library."""
    picked = choose_file(text(32039), ".db")
    if not picked:
        return
    if not confirm(text(32038), text(32040)):
        return
    incoming = local_copy(picked)
    if not incoming:
        notify(text(32043))
        return
    try:
        with claim(IMPORTING, WAIT):
            _import(picked, incoming)
    except SlotTaken as busy:
        log("not importing, {0} is writing".format(busy))
        notify(text(32045))
    finally:
        discard(incoming)


def _import(picked: str, incoming: str) -> None:
    """Merge a working copy, keeping whichever side changed later, and restore what changed."""
    try:
        with get_db() as cursor:
            batch = next_batch(cursor)
            counts, before = merge_database(cursor, incoming, batch)
    except sqlite3.Error as error:
        log("{0} is not a watchkeeper backup: {1!r}".format(picked, error))
        notify(text(32043))
        return
    log("imported {0} changes across {1} items, {2} of them new to us, from {3}".format(
        counts["changed"], counts["items"], counts["new"], picked))
    if before:
        with progress(text(32007)) as bar, get_db() as cursor:
            restore_merged(cursor, before, batch, bar.step)
    notify(text(32042).format(counts["changed"], counts["items"]))

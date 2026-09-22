"""Menu shown from the Programs list, and the actions the settings buttons run."""

import os
import sqlite3
import sys

from lib.data.database import (close_database, get_db, holdings, next_batch, open_database,
                               snapshot, undoable_restores)
from lib.kodi.client import log
from lib.kodi.dialogs import (addon_name, choose, confirm, kodi_text, notify, progress, report,
                              text, when)
from lib.kodi.slot import WAIT, SlotTaken, claim
from lib.kodi.utilities import database_path
from lib.sync.backup import sweep
from lib.sync.restore import restore_library, undo_batch

RESTORE, UNDO, BACK_UP, SUMMARY = 0, 1, 2, 3
WRITING = "the Programs menu"
ACTIONS = ("export", "export_data", "import_data")


def main() -> None:
    """Run the named action, or offer the menu when none was named."""
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    path = database_path()
    try:
        open_database(path)
        log("opened {0}, holding {1}".format(path, holdings()))
        if action in ACTIONS:
            _run_action(action)
            return
        chosen = choose(addon_name(), [text(32003), text(32026), text(32004), text(32005)])
        if chosen == RESTORE:
            restore()
        elif chosen == UNDO:
            undo()
        elif chosen == BACK_UP:
            backup()
        elif chosen == SUMMARY:
            summary()
    except sqlite3.OperationalError as error:
        log("database locked: {0}".format(error))
        notify(text(32045))
    finally:
        close_database()


def _run_action(action: str) -> None:
    """Run a settings button's action, importing its module only now."""
    if action == "export":
        from lib.script.export import export
        export()
        return
    from lib.script.transfer import export_data, import_data
    (export_data if action == "export_data" else import_data)()


def restore() -> None:
    """Restore held user data into the library, after warning that it writes to it."""
    if not confirm(text(32003), text(32006)):
        return
    try:
        with claim(WRITING, WAIT):
            _restore_all()
    except SlotTaken as busy:
        log("not restoring, {0} is writing".format(busy))
        notify(text(32045))


def _restore_all() -> None:
    """Put everything held back into the library, offering an undo when it wrote nothing."""
    taken = snapshot()
    log("snapshot before restoring: {0}".format(os.path.basename(taken) or "none needed"))
    with get_db() as cursor, progress(text(32007)) as bar:
        counts = restore_library(cursor, bar.step, batch=next_batch(cursor))
    log("restored from what we hold: {0}".format(counts))
    if not counts["restored"]:
        with get_db() as cursor:
            undoable = bool(undoable_restores(cursor, 1))
        if not undoable:
            notify(text(32019))
            return
        if confirm(text(32003), text(32033)):
            undo()
            return
    notify(text(32008).format(counts["restored"], counts["checked"]))


def backup() -> None:
    """Back up the whole library on demand, then take a snapshot."""
    with progress(text(32009)):
        with get_db() as cursor:
            counts = sweep(cursor)
    taken = snapshot()
    log("backed up {0}, snapshot {1}".format(counts, os.path.basename(taken) or "none needed"))
    notify(text(32010).format(counts.get("movie", 0), counts.get("episode", 0)))


def summary() -> None:
    """Show what the addon holds, and where it keeps it."""
    held = holdings()
    watched = held["watched"]
    lines = [
        text(32011).format(held.get("movie", 0)),
        text(32012).format(held.get("episode", 0)),
        text(32013).format(held.get("tvshow", 0)),
        "",
        text(32014).format(watched),
        text(32015).format(database_path()),
    ]
    report(addon_name(), lines)


def undo() -> None:
    """Undo a restore the user picks, putting back what the library held before it."""
    try:
        with claim(WRITING, WAIT):
            _undo_restore()
    except SlotTaken as busy:
        log("not undoing, {0} is writing".format(busy))
        notify(text(32045))


def _undo_restore() -> None:
    """Offer the restores that can be undone and undo the one picked."""
    with get_db() as cursor:
        runs = undoable_restores(cursor)
    if not runs:
        notify(text(32025))
        return
    labels = [text(32028).format(when(stamp), items) for batch, items, stamp in runs]
    log("offering {0} restores to undo: {1}".format(len(runs), " | ".join(labels)))
    chosen = choose(text(32026), labels + [kodi_text(222)])
    if chosen < 0 or chosen >= len(runs) or not confirm(text(32026), text(32027)):
        return
    with progress(text(32007)) as bar:
        with get_db() as cursor:
            counts = undo_batch(cursor, runs[chosen][0], bar.step)
    notify(text(32008).format(counts["restored"], counts["checked"]))


"""Write stored user data back into the Kodi library."""

from typing import Any, Callable, Dict, NamedTuple, Optional, Set

from lib.data.database import (append_history, batch_rows, clear_rebuilt, group_fields, media_row,
                               next_batch, note_dbid, read_state, stamp_now)
from lib.kodi.client import log, request
from lib.kodi.utilities import device_id
from lib.sync.backup import list_items, read_details, short_file
from lib.sync.matcher import find_by_ids, find_episode, ids_of, normalize_ids

MAX_RATING = 10
SET_METHOD = {
    "movie": "VideoLibrary.SetMovieDetails",
    "episode": "VideoLibrary.SetEpisodeDetails",
    "tvshow": "VideoLibrary.SetTVShowDetails",
}
_NO_RESUME = ("tvshow",)
_GROUP_OF = {
    "playcount": "playcount_changed",
    "lastplayed": "playcount_changed",
    "resume": "resume_changed",
    "userrating": "userrating_changed",
}


class Run(NamedTuple):
    """What one restore pass carries from item to item."""

    batch: Optional[int] = None
    shows: Optional[Dict[int, Optional[int]]] = None
    filling: bool = False
    claimed: Optional[Dict[int, int]] = None
    listed: bool = False
    files: Optional[Set[str]] = None


def plan(record: Dict[str, Any], current: Dict[str, Any], force: bool = False,
         movable: bool = True) -> Dict[str, Any]:
    """Decide which fields to write back, leaving Kodi's newer values alone unless forced."""
    changes = {}
    _plan_watched(record, current, force, changes)
    if movable:
        _plan_resume(record, current, force, changes)
    _plan_rating(record, current, force, changes)
    return changes


def _plan_watched(record: Dict[str, Any], current: Dict[str, Any], force: bool,
                  changes: Dict[str, Any]) -> None:
    """Plan the play count and last played time, the only fields Kodi keeps a date for."""
    held = record.get("playcount")
    if held is None or held < 0:
        return
    if not held and not force:
        return
    ours, theirs = record.get("lastplayed") or "", current.get("lastplayed") or ""
    if not force and theirs >= ours and not _unwatched(current):
        return
    if held == current.get("playcount", 0) and ours == theirs:
        return
    changes["playcount"] = held
    if ours:
        changes["lastplayed"] = ours
    elif not held:
        changes["lastplayed"] = ""


def _plan_resume(record: Dict[str, Any], current: Dict[str, Any], force: bool,
                 changes: Dict[str, Any]) -> None:
    """Plan the resume point, unless the library played more recently or already holds one."""
    position = int(record.get("resume_position") or 0)
    total = int(record.get("resume_total") or 0)
    if position <= 0 or (total and position > total):
        return
    theirs = current.get("resume") or {}
    if not force and (current.get("lastplayed") or "") > (record.get("lastplayed") or ""):
        return
    if not force and (theirs.get("position") or 0.0) > 0.0:
        return
    if (theirs.get("position") or 0.0) == position:
        return
    changes["resume"] = {"position": position, "total": total}


def _plan_rating(record: Dict[str, Any], current: Dict[str, Any], force: bool,
                 changes: Dict[str, Any]) -> None:
    """Plan the user rating, only where the library has none, unless forced."""
    held = record.get("userrating") or 0
    if held <= 0 or held > MAX_RATING:
        return
    theirs = current.get("userrating") or 0
    if theirs == held or (theirs > 0 and not force):
        return
    changes["userrating"] = held


def _unwatched(current: Dict[str, Any]) -> bool:
    """True when Kodi holds no watch history for an item."""
    return not current.get("playcount") and not (current.get("lastplayed") or "")


def summarize(changes: Dict[str, Any]) -> Optional[str]:
    """Name the fields a plan would write, or None for an empty plan."""
    if not changes:
        return None
    return ", ".join(sorted(changes))


def restore_item(cursor: Any, kind: str, dbid: int, force: bool = False,
                 batch: Optional[int] = None) -> Optional[str]:
    """Restore one library item's user data, writing only the fields that need it."""
    details = read_details(kind, dbid)
    if not details:
        return None
    return restore_details(cursor, kind, details, force, Run(batch=batch))


def preview(cursor: Any, kind: str, details: Dict[str, Any], force: bool = False,
            shows: Optional[Dict[int, Optional[int]]] = None,
            files: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Work out what a restore would write for one item, without writing any of it."""
    media_id = find_record(cursor, kind, details, shows)
    if media_id is None:
        return {}
    return _plan_changes(cursor, kind, media_id, details, force, Run(files=files))


def find_record(cursor: Any, kind: str, details: Dict[str, Any],
                shows: Optional[Dict[int, Optional[int]]] = None) -> Optional[int]:
    """Find the record a library item belongs to, by ids or by its place in a show."""
    media_id = find_by_ids(cursor, kind, normalize_ids(details.get("uniqueid") or {}))
    if media_id is None and kind == "episode" and shows is not None:
        media_id = _find_by_place(cursor, details, shows)
    return media_id


def _plan_changes(cursor: Any, kind: str, media_id: int, details: Dict[str, Any],
                  force: bool, run: Optional["Run"] = None) -> Dict[str, Any]:
    """Plan the fields a restore would write, given the record an item matched."""
    record = read_state(cursor, media_id)
    if not record:
        return {}
    movable = kind not in _NO_RESUME and _resume_belongs_here(record, kind, details, run)
    return plan(record, details, force, movable)


def _resume_belongs_here(record: Dict[str, Any], kind: str, details: Dict[str, Any],
                         run: Optional["Run"]) -> bool:
    """True when the resume came from this file, or from a file that has since left the library."""
    wanted = record.get("resume_file")
    if not wanted or short_file(details.get("file") or "") == wanted:
        return True
    if run is not None and run.files is not None:
        return wanted not in run.files
    name = wanted.split("/")[-1]
    listed = list_items(kind, filter={"field": "filename", "operator": "is", "value": name})
    return wanted not in {short_file(item.get("file") or "") for item in listed}


def _find_by_place(cursor: Any, details: Dict[str, Any],
                   shows: Dict[int, Optional[int]]) -> Optional[int]:
    """Find an episode with no id of its own by where it sits in its show."""
    tvshowid, season, number = (details.get("tvshowid"), details.get("season"),
                                details.get("episode"))
    if not tvshowid or season is None or number is None:
        return None
    if tvshowid not in shows:
        show = read_details("tvshow", tvshowid) or {}
        shows[tvshowid] = find_by_ids(cursor, "tvshow",
                                      normalize_ids(show.get("uniqueid") or {}))
    parent = shows[tvshowid]
    return find_episode(cursor, parent, season, number) if parent else None


def restore_details(cursor: Any, kind: str, details: Dict[str, Any], force: bool = False,
                    run: Optional["Run"] = None) -> Optional[str]:
    """Restore an item whose library details have already been read, committing as it goes."""
    run = run or Run()
    dbid = details.get(kind + "id")
    if not dbid:
        return None
    media_id = find_record(cursor, kind, details, run.shows)
    if kind == "tvshow" and run.shows is not None:
        run.shows[dbid] = media_id
    if media_id is None:
        return None
    _log_shared_record(run, kind, dbid, media_id, details)
    changes = _plan_changes(cursor, kind, media_id, details, force, run)
    if changes and run.listed:
        # the listing may be minutes old by now, a play since then must win
        details = read_details(kind, dbid) or {}
        changes = _plan_changes(cursor, kind, media_id, details, force, run) if details else {}
    written = _write(cursor, kind, dbid, media_id, details, changes, run.batch)
    if run.filling:
        note_dbid(cursor, media_id, dbid)
        clear_rebuilt(cursor, media_id)
    cursor.connection.commit()
    return written


def _log_shared_record(run: "Run", kind: str, dbid: int, media_id: int,
                       details: Dict[str, Any]) -> None:
    """Log when a second library item matches the same record, since no id tells them apart."""
    if run.claimed is None:
        return
    first = run.claimed.setdefault(media_id, dbid)
    if first != dbid:
        log("record {0} matches {1} dbid {2} and dbid {3}, \"{4}\" cannot be told from the "
            "other by id".format(media_id, kind, first, dbid, details.get("title")))


def restore_state(cursor: Any, kind: str, details: Dict[str, Any], state: Dict[str, Any],
                  batch: Optional[int] = None) -> Optional[str]:
    """Restore an earlier state of this item over whatever the library holds now."""
    dbid = details.get(kind + "id")
    media_id = find_record(cursor, kind, details)
    if not dbid or media_id is None:
        return None
    changes = plan(state, details, True)
    if kind in _NO_RESUME:
        changes.pop("resume", None)
    return _write(cursor, kind, dbid, media_id, details, changes, batch)


def _write(cursor: Any, kind: str, dbid: int, media_id: int, details: Dict[str, Any],
           changes: Dict[str, Any], batch: Optional[int]) -> Optional[str]:
    """Write planned changes into the library and record the before and after."""
    if not changes:
        return None
    params = dict(changes)
    params[kind + "id"] = dbid
    title = details.get("title")
    moved = _before_and_after(details, changes)
    if request(SET_METHOD[kind], **params) is None:
        log("could not restore {0} \"{1}\" (dbid {2})".format(kind, title, dbid))
        return None
    if batch is not None:
        _record_restore(cursor, media_id, details, changes, batch)
    written = summarize(changes)
    log("restored {0} \"{1}\" (dbid {2}): {3}\n  matched record {4} {5}\n  library ids {6}".format(
        kind, title, dbid, moved, media_id, _spell_record(cursor, media_id),
        _spell(normalize_ids(details.get("uniqueid") or {}))))
    return written


def _spell_record(cursor: Any, media_id: int) -> str:
    """Spell a record's name and ids out on one line, so a wrong match shows in the log."""
    cursor.execute("SELECT name FROM media WHERE id = ?", (media_id,))
    row = cursor.fetchone()
    return "{0!r} ids {1}".format(row[0] if row else None, _spell(dict(ids_of(cursor, media_id))))


def _spell(ids: Dict[str, str]) -> str:
    """Spell a set of ids out on one line."""
    return ",".join("{0}={1}".format(kind, value) for kind, value in sorted(ids.items())) or "none"


def _before_and_after(details: Dict[str, Any], changes: Dict[str, Any]) -> str:
    """Say what each field being written was and what it becomes."""
    said = []
    for field in sorted(changes):
        if field == "resume":
            was = (details.get("resume") or {}).get("position") or 0
            said.append("resume {0} -> {1}".format(was, changes[field].get("position")))
        else:
            said.append("{0} {1!r} -> {2!r}".format(field, details.get(field), changes[field]))
    return ", ".join(said)


def _record_restore(cursor: Any, media_id: int, details: Dict[str, Any],
                    changes: Dict[str, Any], batch: int) -> None:
    """Record what the library held and what we wrote, under one batch."""
    stamp, device = stamp_now(), device_id()
    after = dict(details, **changes)
    for group in {_GROUP_OF[field] for field in changes}:
        append_history(cursor, media_id, group, _values_from_details(group, details), stamp, device,
                       "library", batch)
        append_history(cursor, media_id, group, _values_from_details(group, after), stamp, device,
                       "restore", batch)


def _values_from_details(group: str, details: Dict[str, Any]) -> tuple:
    """Pull one field group's values out of an item as Kodi reports it."""
    if group == "playcount_changed":
        return (details.get("playcount"), details.get("lastplayed") or None)
    if group == "resume_changed":
        resume = details.get("resume") or {}
        position = resume.get("position") or 0.0
        return (position, resume.get("total") or 0.0,
                short_file(details.get("file") or "") if position else None)
    return (details.get("userrating") or 0,)


def restore_library(cursor: Any, on_step: Optional[Callable[[int, int, str], bool]] = None,
                    force: bool = False, batch: Optional[int] = None,
                    filling: bool = False,
                    on_restore: Optional[Callable[[], None]] = None) -> Dict[str, int]:
    """Restore every item, reading each media type in one call."""
    counts = {"restored": 0, "checked": 0}
    listed = [(kind, list_items(kind)) for kind in ("tvshow", "movie", "episode")]
    files = {short_file(details.get("file") or "") for _, items in listed for details in items}
    run = Run(batch=batch, shows={}, filling=filling, claimed={}, listed=True, files=files)
    total = sum(len(items) for _, items in listed)
    for kind, items in listed:
        for details in items:
            if on_step and not on_step(counts["checked"], total, details.get("title") or ""):
                return counts
            counts["checked"] += 1
            if restore_details(cursor, kind, details, force, run):
                counts["restored"] += 1
                if on_restore:
                    on_restore()
    return counts


def undo_batch(cursor: Any, batch: int,
               on_step: Optional[Callable[[int, int, str], bool]] = None) -> Dict[str, int]:
    """Put back what the library held before one restore, as a restore of its own, item by item."""
    wanted: Dict[int, Dict[str, tuple]] = {}
    for row in batch_rows(cursor, batch):
        wanted.setdefault(row[0], {})[row[1]] = row[2:]
    replacing: Dict[int, Dict[str, tuple]] = {}
    for row in batch_rows(cursor, batch, "restore"):
        replacing.setdefault(row[0], {})[row[1]] = row[2:]
    undoing = next_batch(cursor)
    counts = {"restored": 0, "checked": 0}
    for index, (media_id, groups) in enumerate(wanted.items()):
        item = media_row(cursor, media_id)
        counts["checked"] += 1
        if on_step and not on_step(index, len(wanted), ""):
            return counts
        if not item or not item[1]:
            continue
        kind, dbid = item
        details = read_details(kind, dbid)
        if not details or find_record(cursor, kind, details) != media_id:
            log("skipping undo of record {0}, {1} dbid {2} is now another title".format(
                media_id, kind, dbid))
            continue
        params: Dict[str, Any] = {}
        for group, values in groups.items():
            params.update(_params_from_row(group, values))
        if kind in _NO_RESUME:
            params.pop("resume", None)
        params[kind + "id"] = dbid
        if request(SET_METHOD[kind], **params) is None:
            log("could not undo {0} dbid {1}".format(kind, dbid))
            continue
        counts["restored"] += 1
        stamp, device = stamp_now(), device_id()
        for group, values in groups.items():
            was = replacing.get(media_id, {}).get(group)
            if was is not None:
                append_history(cursor, media_id, group, _values_from_row(group, was), stamp,
                               device, "library", undoing)
            append_history(cursor, media_id, group, _values_from_row(group, values), stamp, device,
                           "restore", undoing)
        cursor.connection.commit()
    log("undid restore {0}: {1}".format(batch, counts))
    return counts


def _params_from_row(group: str, values: tuple) -> Dict[str, Any]:
    """Turn one field group's recorded values into the parameters Kodi's setter takes."""
    fields = group_fields(group, values)
    if group == "playcount_changed":
        return {"playcount": fields["playcount"] or 0, "lastplayed": fields["lastplayed"] or ""}
    if group == "resume_changed":
        return {"resume": {"position": fields["resume_position"] or 0,
                           "total": fields["resume_total"] or 0}}
    return {"userrating": fields["userrating"] or 0}


def _values_from_row(group: str, values: tuple) -> tuple:
    """Take one field group's columns out of a full history row."""
    return tuple(group_fields(group, values).values())


def restore_show(cursor: Any, tvshowid: int, force: bool = False, batch: Optional[int] = None,
                 on_step: Optional[Callable[[int, int, str], bool]] = None) -> Dict[str, int]:
    """Restore one show's own rating and every episode under it."""
    counts = {"restored": 0, "checked": 0}
    if restore_item(cursor, "tvshow", tvshowid, force, batch):
        counts["restored"] += 1
    counts["checked"] += 1
    items = list_items("episode", tvshowid=tvshowid)
    files = {short_file(details.get("file") or "") for details in items}
    run = Run(batch=batch, shows={}, claimed={}, listed=True, files=files)
    for index, details in enumerate(items):
        if on_step and not on_step(index, len(items), details.get("title") or ""):
            return counts
        counts["checked"] += 1
        if restore_details(cursor, "episode", details, force, run):
            counts["restored"] += 1
    return counts

"""Back up user data from the Kodi library as it changes."""

import json
from typing import Any, Callable, Dict, Optional, Tuple

from lib.data.database import (clear_rebuilt, is_rebuilt, read_state, record_group,
                               record_prior_state, stamp_now)
from lib.kodi.client import log, request
from lib.kodi.utilities import device_id
from lib.sync.matcher import (find_by_ids, normalize_ids, resolve_episode,
                              resolve_movie, resolve_show)

PROPERTIES = {
    "movie": ["uniqueid", "title", "year", "playcount", "lastplayed", "resume", "userrating",
              "file"],
    "episode": ["uniqueid", "title", "season", "episode", "playcount", "lastplayed", "resume",
                "userrating", "tvshowid", "file"],
    "tvshow": ["uniqueid", "title", "year", "userrating"],
}
_DETAIL_METHOD = {
    "movie": "VideoLibrary.GetMovieDetails",
    "episode": "VideoLibrary.GetEpisodeDetails",
    "tvshow": "VideoLibrary.GetTVShowDetails",
}
ANNOUNCEMENTS = ("VideoLibrary.OnUpdate", "Player.OnStop")
REMOVALS = ("VideoLibrary.OnRemove",)
SETTLED = ("VideoLibrary.OnScanFinished", "VideoLibrary.OnCleanFinished")

LIST_METHOD = {
    "movie": "VideoLibrary.GetMovies",
    "episode": "VideoLibrary.GetEpisodes",
    "tvshow": "VideoLibrary.GetTVShows",
}
LIST_KEY = {"movie": "movies", "episode": "episodes", "tvshow": "tvshows"}

_WATCHABLE = ("movie", "episode")


def short_file(path: str) -> str:
    """Folder and filename of a library path, the same on every box however the share is mounted."""
    parts = path.replace("\\", "/").rstrip("/").split("/")
    if len(parts) > 2 and parts[-2].upper() in ("VIDEO_TS", "BDMV"):
        parts = parts[:-2] + parts[-1:]
    return "/".join(parts[-2:])


def list_items(kind: str, **params: Any) -> list:
    """List every library item of one type, with the fields we back up, in one call."""
    result = request(LIST_METHOD[kind], properties=PROPERTIES[kind], **params)
    return (result or {}).get(LIST_KEY[kind]) or []


def read_details(kind: str, dbid: int) -> Optional[Dict[str, Any]]:
    """Read the fields we back up for one library item, or None when Kodi returns nothing."""
    result = request(_DETAIL_METHOD[kind], **{kind + "id": dbid, "properties": PROPERTIES[kind]})
    if not result:
        return None
    return result.get(kind + "details")


def backup_item(cursor: Any, kind: str, dbid: int, stamp: Optional[int] = None,
                sweeping: bool = False,
                shows: Optional[Dict[int, int]] = None) -> Optional[int]:
    """Back up one library item's user data into the record it belongs to."""
    details = read_details(kind, dbid)
    if not details or not _worth_keeping(cursor, kind, details):
        return None
    when = stamp_now() if stamp is None else stamp
    device = device_id()
    if kind == "episode":
        media_id = _resolve_with_show(cursor, details, when, device, sweeping, shows)
    elif kind == "movie":
        media_id = resolve_movie(cursor, details.get("uniqueid") or {}, details.get("title") or "",
                               details.get("year"))
    else:
        media_id = resolve_show(cursor, details.get("uniqueid") or {}, details.get("title") or "",
                              details.get("year"))
    if media_id is None:
        log("no usable unique id for {0} dbid {1}, skipping".format(kind, dbid))
        return None
    marked = is_rebuilt(cursor, media_id)
    trust_empty = not sweeping and not _dbid_changed(cursor, media_id, dbid) and not marked
    _record_fields(cursor, kind, media_id, details, when, device, trust_empty,
                   "sweep" if sweeping else "capture")
    if marked and _carries_data(details):
        clear_rebuilt(cursor, media_id)
    log("captured {0} \"{1}\" (dbid {2})".format(kind, details.get("title"), dbid))
    return media_id


def announced_item(method: str, data: str) -> Optional[Tuple[str, int]]:
    """Name the library item an announcement is about, or None when it is not one we keep."""
    if method not in ANNOUNCEMENTS:
        return None
    payload = json.loads(data)
    if payload.get("transaction"):
        return None
    item = payload.get("item") or payload
    kind, dbid = item.get("type"), item.get("id")
    if kind not in PROPERTIES or not dbid:
        return None
    return kind, dbid


def _worth_keeping(cursor: Any, kind: str, details: Dict[str, Any]) -> bool:
    """True when an item carries user data or we already hold a record for it."""
    if _carries_data(details):
        return True
    return find_by_ids(cursor, kind, normalize_ids(details.get("uniqueid") or {})) is not None


def _carries_data(details: Dict[str, Any]) -> bool:
    """True when Kodi reports any user data for an item."""
    resume = details.get("resume") or {}
    return bool(details.get("playcount") or details.get("lastplayed")
                or (resume.get("position") or 0) or details.get("userrating"))


def removed_item(method: str, data: str) -> Optional[Tuple[str, int]]:
    """Name the library item a removal announcement is about, or None when it is not one."""
    if method not in REMOVALS:
        return None
    payload = json.loads(data)
    item = payload.get("item") or payload
    kind, dbid = item.get("type"), item.get("id")
    return (kind, dbid) if kind in PROPERTIES and dbid else None


def library_settled(method: str) -> bool:
    """True when Kodi says a scan or a clean has finished."""
    return method in SETTLED


def _resolve_with_show(cursor: Any, details: Dict[str, Any], when: int, device: str,
                       sweeping: bool = False,
                       shows: Optional[Dict[int, int]] = None) -> Optional[int]:
    """Resolve an episode through its show, reading and capturing the show once."""
    tvshowid = details.get("tvshowid") or 0
    parent = shows.get(tvshowid) if shows is not None else None
    if parent is None:
        show = read_details("tvshow", tvshowid)
        if not show:
            return None
        parent = resolve_show(cursor, show.get("uniqueid") or {}, show.get("title") or "",
                              show.get("year"))
        if parent is None:
            return None
        _record_fields(cursor, "tvshow", parent, show, when, device,
                       not sweeping and not _dbid_changed(cursor, parent, tvshowid),
                       "sweep" if sweeping else "capture")
        if shows is not None:
            shows[tvshowid] = parent
    return resolve_episode(cursor, parent, details.get("uniqueid") or {},
                           details.get("season", 0), details.get("episode", 0),
                           details.get("title") or "")


def _dbid_changed(cursor: Any, media_id: int, dbid: int) -> bool:
    """True when Kodi now gives this item a different id, keeping the stored one current."""
    cursor.execute("SELECT dbid FROM media WHERE id = ?", (media_id,))
    row = cursor.fetchone()
    known = row[0] if row else None
    if known != dbid:
        cursor.execute("UPDATE media SET dbid = ? WHERE id = ?", (dbid, media_id))
    return known is not None and known != dbid


def _record_fields(cursor: Any, kind: str, media_id: int, details: Dict[str, Any], when: int,
                   device: str, trust_empty: bool = True, source: str = "capture") -> None:
    """Record the field groups this type has. An empty value replaces held data only if trusted."""
    stored = read_state(cursor, media_id)
    held = {} if trust_empty else stored
    first = not stored
    if kind in _WATCHABLE:
        playcount = details.get("playcount") or 0
        if playcount or not (held.get("playcount") or 0):
            watched = (details.get("playcount"), details.get("lastplayed") or None)
            record_prior_state(cursor, media_id, "playcount_changed", watched, stored, device)
            record_group(cursor, media_id, "playcount_changed", watched, when, device, source,
                         baseline=first)
        resume = details.get("resume") or {}
        position = resume.get("position") or 0.0
        if position or not (held.get("resume_position") or 0.0):
            stopped = (position, resume.get("total", 0.0),
                       short_file(details.get("file") or "") if position else None)
            record_prior_state(cursor, media_id, "resume_changed", stopped, stored, device)
            record_group(cursor, media_id, "resume_changed", stopped, when, device, source,
                         baseline=first)
    rating = details.get("userrating") or 0
    if rating or not (held.get("userrating") or 0):
        record_prior_state(cursor, media_id, "userrating_changed", (rating,), stored, device)
        record_group(cursor, media_id, "userrating_changed", (rating,), when, device, source,
                     baseline=first)


def sweep(cursor: Any, stamp: Optional[int] = None,
          should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, int]:
    """Back up the whole library, committing per item, and count the items whose dbid moved."""
    when = stamp_now() if stamp is None else stamp
    device = device_id()
    shows: Dict[int, int] = {}
    unread: Dict[int, Dict[str, Any]] = {}
    counts = {"moved": 0}
    for kind in ("tvshow", "movie", "episode"):
        done = 0
        for details in list_items(kind):
            if should_stop and should_stop():
                return counts
            dbid = details.get(kind + "id")
            if not dbid:
                continue
            if not _worth_keeping(cursor, kind, details):
                if kind == "tvshow":
                    unread[dbid] = details
                continue
            media_id = _resolve_swept(cursor, kind, details, shows, unread)
            if media_id is None:
                continue
            if kind == "tvshow":
                shows[dbid] = media_id
            if _dbid_changed(cursor, media_id, dbid):
                counts["moved"] += 1
            _record_fields(cursor, kind, media_id, details, when, device, False, "sweep")
            cursor.connection.commit()
            done += 1
        counts[kind] = done
    return counts


def _resolve_swept(cursor: Any, kind: str, details: Dict[str, Any], shows: Dict[int, int],
                   unread: Dict[int, Dict[str, Any]]) -> Optional[int]:
    """Resolve one swept item, adding its show to the map when its first episode comes up."""
    ids = details.get("uniqueid") or {}
    title = details.get("title") or ""
    if kind == "movie":
        return resolve_movie(cursor, ids, title, details.get("year"))
    if kind == "tvshow":
        return resolve_show(cursor, ids, title, details.get("year"))
    tvshowid = details.get("tvshowid") or 0
    parent = shows.get(tvshowid)
    if parent is None:
        show = unread.pop(tvshowid, None)
        if not show:
            return None
        parent = resolve_show(cursor, show.get("uniqueid") or {}, show.get("title") or "",
                              show.get("year"))
        if parent is None:
            return None
        shows[tvshowid] = parent
    return resolve_episode(cursor, parent, ids, details.get("season", 0),
                           details.get("episode", 0), title)

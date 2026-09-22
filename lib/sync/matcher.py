"""Match library items to stored records by their unique id sets."""

import sqlite3
from typing import Dict, List, Optional, Tuple

from lib.data.database import read_state
from lib.data.schema import STATE_FIELDS, STATE_GROUPS
from lib.kodi.client import log

ID_TYPES = ("imdb", "tmdb", "tvdb")


def normalize_ids(uniqueids: dict) -> Dict[str, str]:
    """Normalize Kodi's uniqueid mapping to the id types we match on, as strings."""
    clean = {}
    for id_type in ID_TYPES:
        value = str(uniqueids.get(id_type) or "").strip()
        if value:
            clean[id_type] = value
    return clean


def find_by_ids(cursor: sqlite3.Cursor, kind: str, ids: Dict[str, str]) -> Optional[int]:
    """Find the record these ids agree with, writing nothing; most ids wins when several do."""
    found = candidates(cursor, kind, ids)
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return _best_known(cursor, found)


def candidates(cursor: sqlite3.Cursor, kind: str, ids: Dict[str, str]) -> List[int]:
    """List every record of this media type that shares one of these ids and disagrees on none."""
    if not ids:
        return []
    # media_type inside every term, or three terms make SQLite scan the whole type
    where = " OR ".join("(media_type = ? AND type = ? AND value = ?)" for _ in ids)
    cursor.execute(
        "SELECT DISTINCT media_id FROM uniqueid WHERE {0}".format(where),
        [part for id_type, value in ids.items() for part in (kind, id_type, value)],
    )
    kept = []
    for media_id in sorted(row[0] for row in cursor.fetchall()):
        clash = _clash(cursor, media_id, ids)
        if clash:
            log("refused {0} record {1}, {2}".format(kind, media_id, clash))
        else:
            kept.append(media_id)
    return kept


def ids_of(cursor: sqlite3.Cursor, media_id: int) -> List[Tuple[str, str]]:
    """Every unique id a record carries, as (type, value) pairs. A type can appear twice."""
    cursor.execute("SELECT type, value FROM uniqueid WHERE media_id = ?", (media_id,))
    return cursor.fetchall()


def _clash(cursor: sqlite3.Cursor, media_id: int, ids: Dict[str, str]) -> str:
    """Name the first id the record and the item disagree on, or empty when they agree."""
    for id_type, value in ids_of(cursor, media_id):
        if ids.get(id_type, value) != value:
            return "its {0} {1} is not the item's {2}".format(id_type, value, ids[id_type])
    return ""


def find_episode(
    cursor: sqlite3.Cursor, show: int, season: int, number: int
) -> Optional[int]:
    """Find the episode at this place in the show, for one carrying no id of its own."""
    cursor.execute(
        "SELECT id FROM media WHERE parent = ? AND season = ? AND number = ?",
        (show, season, number),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def merge_records(cursor: sqlite3.Cursor, candidates: List[int]) -> int:
    """Merge records a new id shows to be one title, keeping the newer state and all history."""
    keeper = candidates[0]
    for other in candidates[1:]:
        _merge_state(cursor, keeper, other)
        for table in ("uniqueid", "history"):
            cursor.execute("UPDATE {0} SET media_id = ? WHERE media_id = ?".format(table),
                           (keeper, other))
        cursor.execute("UPDATE media SET parent = ? WHERE parent = ?", (keeper, other))
        cursor.execute("INSERT OR IGNORE INTO rebuilt (media_id, stamp) "
                       "SELECT ?, stamp FROM rebuilt WHERE media_id = ?", (keeper, other))
        cursor.execute("DELETE FROM rebuilt WHERE media_id = ?", (other,))
        cursor.execute("DELETE FROM media WHERE id = ?", (other,))
    return keeper


def resolve_movie(
    cursor: sqlite3.Cursor,
    uniqueids: dict,
    name: str = "",
    year: Optional[int] = None,
) -> Optional[int]:
    """Resolve a movie to its record, creating one if needed and learning any new ids."""
    return _resolve(cursor, "movie", uniqueids, name=name, year=year)


def resolve_show(
    cursor: sqlite3.Cursor,
    uniqueids: dict,
    name: str = "",
    year: Optional[int] = None,
) -> Optional[int]:
    """Resolve a TV show to its record, creating one if needed and learning any new ids."""
    return _resolve(cursor, "tvshow", uniqueids, name=name, year=year)


def resolve_episode(
    cursor: sqlite3.Cursor,
    show: int,
    uniqueids: dict,
    season: int,
    number: int,
    name: str = "",
) -> Optional[int]:
    """Resolve an episode to its record by its ids or its place in the show, or create one."""
    media_id = _resolve(
        cursor,
        "episode",
        uniqueids,
        name=name,
        parent=show,
        season=season,
        number=number,
    )
    if media_id is not None:
        # a scraper switch renumbers episodes
        cursor.execute(
            "UPDATE media SET parent = ?, season = ?, number = ? WHERE id = ?",
            (show, season, number, media_id),
        )
    return media_id


def _resolve(
    cursor: sqlite3.Cursor,
    kind: str,
    uniqueids: dict,
    name: str = "",
    year: Optional[int] = None,
    parent: Optional[int] = None,
    season: Optional[int] = None,
    number: Optional[int] = None,
) -> Optional[int]:
    """Resolve an item to its record, creating one if needed, then attach any new ids."""
    ids = normalize_ids(uniqueids)
    found = candidates(cursor, kind, ids)
    media_id = found[0] if len(found) == 1 else None
    if len(found) > 1:
        media_id = (_best_known(cursor, found) if _ids_disagree(cursor, found)
                    else merge_records(cursor, found))
    if media_id is None and parent is not None and season is not None and number is not None:
        media_id = find_episode(cursor, parent, season, number)
    if media_id is None:
        if not ids and parent is None:
            return None
        cursor.execute(
            "INSERT INTO media (media_type, parent, season, number, name, year) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, parent, season, number, name or None, year),
        )
        media_id = cursor.lastrowid
    for id_type, value in ids.items():
        cursor.execute(
            "INSERT OR IGNORE INTO uniqueid (media_id, media_type, type, value) "
            "VALUES (?, ?, ?, ?)",
            (media_id, kind, id_type, value),
        )
    return media_id


def _ids_disagree(cursor: sqlite3.Cursor, candidates: List[int]) -> bool:
    """True when two of these records carry different values for the same id type."""
    seen = {}
    for media_id in candidates:
        for id_type, value in ids_of(cursor, media_id):
            if seen.setdefault(id_type, value) != value:
                return True
    return False


def _best_known(cursor: sqlite3.Cursor, candidates: List[int]) -> int:
    """Pick the record with the most ids, for when a merge is refused."""
    cursor.execute(
        "SELECT media_id, COUNT(*) FROM uniqueid WHERE media_id IN ({0}) GROUP BY media_id".format(
            ", ".join("?" * len(candidates))
        ),
        candidates,
    )
    counts = dict(cursor.fetchall())
    return max(candidates, key=lambda media_id: (counts.get(media_id, 0), -media_id))


def _merge_state(cursor: sqlite3.Cursor, keeper: int, other: int) -> None:
    """Merge one record's state into another, each field group from whoever changed it later."""
    theirs = read_state(cursor, other)
    if not theirs:
        return
    ours = read_state(cursor, keeper)
    if not ours:
        cursor.execute("UPDATE state SET media_id = ? WHERE media_id = ?", (keeper, other))
        return
    winner = dict(ours)
    for stamp, fields in STATE_GROUPS:
        if (theirs[stamp] or 0) > (ours[stamp] or 0):
            winner[stamp] = theirs[stamp]
            for field in fields:
                winner[field] = theirs[field]
    assignments = ", ".join("{0} = ?".format(field) for field in STATE_FIELDS)
    cursor.execute(
        "UPDATE state SET {0} WHERE media_id = ?".format(assignments),
        [winner[field] for field in STATE_FIELDS] + [keeper],
    )
    cursor.execute("DELETE FROM state WHERE media_id = ?", (other,))

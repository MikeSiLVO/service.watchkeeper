"""Write a readable list of everything saved to a file the user picks."""

import os
import time

import xbmcvfs

from lib.data.database import get_db
from lib.kodi.client import log
from lib.kodi.dialogs import choose_folder, describe_state, notify, text

EXPORT_NAME = "watchkeeper-{0}.txt"


def export() -> None:
    """Write a readable list of everything held to a folder the user picks."""
    folder = choose_folder(text(32020))
    if not folder:
        return
    with get_db() as cursor:
        lines = _export_lines(cursor)
    target = os.path.join(xbmcvfs.translatePath(folder),
                          EXPORT_NAME.format(time.strftime("%Y%m%d-%H%M%S")))
    handle = xbmcvfs.File(target, "w")
    try:
        written = handle.write("\n".join(lines) + "\n")
    finally:
        handle.close()
    if not written:
        log("could not write {0}".format(target))
        notify(text(32046).format(target))
        return
    log("wrote {0} lines to {1}".format(len(lines), target))
    notify(text(32021).format(len(lines)))


def _export_lines(cursor) -> list:
    """Build the export: movies, then episodes under their show."""
    lines = [text(32022).format(time.strftime("%Y-%m-%d %H:%M")), ""]
    cursor.execute("""SELECT m.name, m.year, s.playcount, s.lastplayed, s.userrating,
                             s.resume_position
                      FROM media m JOIN state s ON s.media_id = m.id
                      WHERE m.media_type = 'movie' AND (COALESCE(s.playcount, 0) > 0
                            OR COALESCE(s.userrating, 0) > 0
                            OR COALESCE(s.resume_position, 0) > 0)
                      ORDER BY m.name""")
    movies = cursor.fetchall()
    lines.append(text(32011).format(len(movies)))
    for row in movies:
        lines.append("  {0}".format(_describe(row)))
    cursor.execute("""SELECT p.name, m.season, m.number, m.name, s.playcount, s.lastplayed,
                             s.userrating, s.resume_position
                      FROM media m JOIN state s ON s.media_id = m.id
                      LEFT JOIN media p ON p.id = m.parent
                      WHERE m.media_type = 'episode' AND (COALESCE(s.playcount, 0) > 0
                            OR COALESCE(s.userrating, 0) > 0
                            OR COALESCE(s.resume_position, 0) > 0)
                      ORDER BY p.name, m.season, m.number""")
    episodes = cursor.fetchall()
    lines += ["", text(32012).format(len(episodes))]
    for show, season, number, title, playcount, lastplayed, rating, resume in episodes:
        label = "{0} S{1:02d}E{2:02d} {3}".format(
            show or "?", season or 0, number or 0, title or "")
        lines.append("  {0}".format(
            _describe((label, None, playcount, lastplayed, rating, resume))))
    return lines


def _describe(row) -> str:
    """Render one saved item as one line, title padded so the data lines up."""
    title, year, playcount, lastplayed, rating, resume = row
    label = "{0} ({1})".format(title, year) if year else str(title)
    held = describe_state(playcount, lastplayed, resume, rating)
    return "{0}  {1}".format(label.ljust(52), held).rstrip()

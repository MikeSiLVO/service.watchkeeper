"""Dialogs and the text they show."""

import time
from contextlib import contextmanager
from typing import Iterator, List, Optional, Sequence

import xbmc
import xbmcgui

from lib.kodi.client import ADDON, log
from lib.kodi.slot import stopping

_MONITOR = xbmc.Monitor()


class ProgressDialog(xbmcgui.DialogProgress):
    """Progress dialog that counts a Kodi shutdown as a cancellation."""

    def iscanceled(self) -> bool:
        """True when the user cancelled or Kodi is shutting down."""
        if _MONITOR.abortRequested():
            log("giving up, Kodi is shutting down")
            return True
        if xbmcgui.DialogProgress.iscanceled(self):
            log("cancelled by the user")
            return True
        return False

    def step(self, index: int, total: int, title: str) -> bool:
        """Step the bar on. False once cancelled."""
        self.update(int(index * 100 / max(total, 1)), title)
        return not self.iscanceled()


@contextmanager
def progress(message: str) -> Iterator[ProgressDialog]:
    """Show a cancellable progress dialog under the addon's name while the block runs."""
    dialog = ProgressDialog()
    dialog.create(addon_name(), message)
    try:
        yield dialog
    finally:
        dialog.close()


class BackgroundProgress:
    """Progress bar in the corner, shown only once there is something to report."""

    def __init__(self, heading: str, message: str):
        self.heading = heading
        self.message = message
        self._bar: Optional[xbmcgui.DialogProgressBG] = None

    def start(self) -> None:
        """Start showing the bar, unless it is already up."""
        if self._bar is None:
            bar = xbmcgui.DialogProgressBG()
            bar.create(self.heading, self.message)
            self._bar = bar

    def step(self, index: int, total: int, title: str) -> bool:
        """Step the bar on if it is up; False on shutdown or when another run needs the library."""
        if self._bar is not None:
            self._bar.update(int(index * 100 / max(total, 1)), self.heading, self.message)
        if _MONITOR.abortRequested():
            log("giving up, Kodi is shutting down")
            return False
        if stopping():
            log("giving up, another run needs the library")
            return False
        return True

    def close(self) -> None:
        """Close the bar, if it was ever shown."""
        if self._bar is not None:
            self._bar.close()
            self._bar = None


def text(string_id: int) -> str:
    """Look up one of the addon's own strings by id."""
    return ADDON.getLocalizedString(string_id)


def kodi_text(string_id: int) -> str:
    """Look up one of Kodi's own strings by id."""
    return xbmc.getLocalizedString(string_id)


def addon_name() -> str:
    """Addon name, for dialog headings."""
    return ADDON.getAddonInfo("name")


def describe_state(playcount, lastplayed, resume, rating) -> str:
    """Describe the data held for one item, naming only the parts it has."""
    parts = []
    if playcount:
        parts.append(text(32029).format(playcount))
    if lastplayed:
        parts.append(text(32030).format(str(lastplayed)[:10]))
    if resume:
        parts.append(text(32031).format(clock(resume)))
    if rating:
        parts.append(text(32032).format(rating))
    return "  ".join(parts)


def clock(seconds) -> str:
    """Render a resume position as h:mm:ss, or m:ss when under an hour."""
    total = int(seconds or 0)
    if total >= 3600:
        return "{0}:{1:02d}:{2:02d}".format(total // 3600, total % 3600 // 60, total % 60)
    return "{0}:{1:02d}".format(total // 60, total % 60)


def when(stamp: int) -> str:
    """Turn a change stamp into a readable date and time."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


def notify(message: str, heading: str = "", milliseconds: int = 4000) -> None:
    """Show a notification, falling back to the addon's own name as the heading."""
    xbmcgui.Dialog().notification(heading or addon_name(), message, xbmcgui.NOTIFICATION_INFO,
                                  milliseconds)


def confirm(heading: str, message: str) -> bool:
    """Ask the user to confirm, returning True only when they agree."""
    agreed = bool(xbmcgui.Dialog().yesno(heading, message))
    log("asked \"{0}\": {1}".format(heading, "yes" if agreed else "no"))
    return agreed


def choose(heading: str, options: Sequence[str]) -> int:
    """Ask the user to choose a label, returning its index or -1 when they back out."""
    chosen = xbmcgui.Dialog().select(heading, list(options))
    log("menu \"{0}\": {1}".format(heading, options[chosen] if chosen >= 0 else "closed"))
    return chosen


def choose_folder(heading: str) -> str:
    """Ask which folder to write a file into."""
    return _browse(3, heading)


def choose_file(heading: str, mask: str = "") -> str:
    """Ask which file to read, from anywhere Kodi can reach."""
    return _browse(1, heading, mask)


def _browse(dialog_type: int, heading: str, mask: str = "") -> str:
    """Browse with Kodi's file dialog, returning what was picked or empty when nothing was."""
    chosen = xbmcgui.Dialog().browse(dialog_type, heading, "files", mask)
    picked = chosen if isinstance(chosen, str) else ""
    log("browsed \"{0}\": {1}".format(heading, picked or "nothing"))
    return picked


def report(heading: str, lines: List[str]) -> None:
    """Report what an action did in a viewer the user can scroll and dismiss."""
    xbmcgui.Dialog().textviewer(heading, "\n".join(lines), usemono=True)

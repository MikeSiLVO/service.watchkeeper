"""One writer at a time, shared between the service and the scripts."""

import json
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

import xbmcgui

from lib.kodi.client import log

HEARTBEAT = 5.0
STALE = 15.0
POLL = 1.0
WAIT = 10.0

_HOME = xbmcgui.Window(10000)
_HELD = "Watchkeeper.Writing"
_STOP = "Watchkeeper.Stop"

_depth = 0
_beating: Optional[threading.Event] = None
_asked = False
_asked_at = 0.0


class SlotTaken(Exception):
    """Raised when another run is already writing to the library."""


@contextmanager
def claim(name: str, wait: float = 0.0) -> Iterator[None]:
    """Hold the one writing slot, first giving whoever has it this long to stand down."""
    global _depth
    if not _depth:
        _take(name, wait)
    _depth += 1
    try:
        yield
    finally:
        _depth -= 1
        if not _depth:
            _drop()


def holder() -> str:
    """Name the run holding the slot, or clear it when the holder's heartbeat has stopped."""
    raw = _HOME.getProperty(_HELD)
    if not raw:
        return ""
    try:
        held = json.loads(raw)
    except ValueError:
        _HOME.clearProperty(_HELD)
        return ""
    late = time.time() - (held.get("beat") or 0)
    if late <= STALE:
        return held.get("name") or ""
    log("clearing the writing slot, {0} stopped beating {1:.0f}s ago".format(
        held.get("name"), late))
    _clear()
    return ""


def stopping() -> bool:
    """True once the holder has been asked to stand down, read no more than once a second."""
    global _asked, _asked_at
    now = time.monotonic()
    if now - _asked_at < POLL:
        return _asked
    _asked_at = now
    _asked = bool(_HOME.getProperty(_STOP))
    return _asked


def _take(name: str, wait: float) -> None:
    """Take the slot for this run and start the heartbeat, or raise when someone else keeps it."""
    global _beating
    if holder() and wait:
        _stand_down(wait)
    held = holder()
    if held:
        _HOME.clearProperty(_STOP)
        raise SlotTaken(held)
    _write(name)
    _beating = threading.Event()
    beat = threading.Thread(target=_beat, args=(name, _beating))
    beat.daemon = True
    beat.start()


def _drop() -> None:
    """Give the slot back and stop the heartbeat."""
    global _beating
    if _beating is not None:
        _beating.set()
        _beating = None
    _clear()


def _stand_down(wait: float) -> None:
    """Ask the holder to give the slot up and wait this long for it."""
    log("asking {0} to stand down, waiting up to {1:.0f}s".format(holder(), wait))
    _HOME.setProperty(_STOP, "1")
    until = time.time() + wait
    while time.time() < until and holder():
        time.sleep(0.25)


def _write(name: str) -> None:
    """Write who holds the slot and when they last said so."""
    _HOME.setProperty(_HELD, json.dumps({"name": name, "beat": time.time()}))


def _clear() -> None:
    """Clear the slot and any request to stand down."""
    global _asked, _asked_at
    _HOME.clearProperty(_HELD)
    _HOME.clearProperty(_STOP)
    _asked, _asked_at = False, 0.0


def _beat(name: str, stop: threading.Event) -> None:
    """Beat every few seconds so a run that dies does not leave the slot stuck."""
    while not stop.wait(HEARTBEAT):
        if not stop.is_set():
            _write(name)

"""Where the database lives, which device this is, and waiting for Kodi to be ready."""

import os

import xbmc
import xbmcvfs

from lib.kodi.client import ADDON

DATABASE_FILENAME = "watchkeeper.db"

_device = ""


def database_path() -> str:
    """Where the database lives, inside the addon's own profile directory."""
    profile = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    return os.path.join(profile, DATABASE_FILENAME)


def device_id() -> str:
    """Id of this install, stamped on every record it writes, made once and kept in settings."""
    global _device
    if not _device:
        _device = ADDON.getSetting("device_id")
    if not _device:
        import uuid

        _device = uuid.uuid4().hex[:12]
        ADDON.setSetting("device_id", _device)
    return _device


def wait_for_kodi_ready(monitor: xbmc.Monitor, initial_wait: float = 0.5,
                        check_interval: float = 0.5) -> bool:
    """Poll JSONRPC.Ping until Kodi answers, or return False when asked to stop first."""
    if monitor.waitForAbort(initial_wait):
        return False
    while not monitor.waitForAbort(check_interval):
        try:
            answer = xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"JSONRPC.Ping","id":1}')
        except Exception:
            continue
        if "pong" in answer.lower():
            return True
    return False

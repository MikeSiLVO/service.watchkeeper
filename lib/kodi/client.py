"""JSON-RPC client and Kodi notification handling."""

import json
from typing import Any, Optional

import xbmc
import xbmcaddon

ADDON_ID = "service.watchkeeper"
ADDON = xbmcaddon.Addon(ADDON_ID)

_verbose = ADDON.getSettingBool("debug")


def refresh_verbosity() -> None:
    """Refresh the cached verbose setting."""
    global _verbose
    _verbose = ADDON.getSettingBool("debug")


def log(message: str, level: int = xbmc.LOGDEBUG) -> None:
    """Log a message, promoting debug lines to info while verbose logging is on."""
    if level == xbmc.LOGDEBUG and _verbose:
        level = xbmc.LOGINFO
    xbmc.log("[{0}] {1}".format(ADDON_ID, message), level)


class Listener(xbmc.Monitor):
    """Kodi announcement listener that queues what arrives for the service loop."""

    def __init__(self, queue: Any) -> None:
        xbmc.Monitor.__init__(self)
        self._queue = queue

    def onNotification(self, sender: str, method: str, data: str) -> None:
        """Queue one announcement for the service loop."""
        self._queue.put((method, data))

    def onSettingsChanged(self) -> None:
        """Pick up a verbose setting the user changed."""
        refresh_verbosity()


def request(method: str, **params: Any) -> Optional[Any]:
    """Send a JSON-RPC request and return its result, or None when Kodi reports an error."""
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    answer = json.loads(xbmc.executeJSONRPC(json.dumps(payload)))
    error = answer.get("error")
    if error:
        log("{0} failed: {1}".format(method, error), xbmc.LOGERROR)
        return None
    return answer.get("result")

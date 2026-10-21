"""
Per-device debug output for diagnostic reports.

hoymiles_wifi's debug lines don't say which DTU they belong to, so each device's
poll loop runs with `scope` set to its device id; a filter on the one stdout
handler passes DEBUG/INFO only for scopes whose "Debug logging" setting is on (or
everything while DEBUG_LOG is set). Warnings and errors always pass. A ContextVar
follows the poll task through every await, including library code.

hoymiles_wifi also logs secrets, always dropped: crypt_util's encryption key and
nonce, and every raw "Response:" in hex — the GetConfig reply holds the DTU's
Wi-Fi SSID and password in clear.
"""

import contextvars
import logging
import sys

scope: contextvars.ContextVar[str | None] = contextvars.ContextVar("debug_scope", default=None)

_enabled: set[str] = set()
_force = False


class _Filter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.module == "crypt_util" or str(record.msg).lstrip("[*] ").startswith("Response:"):
            return False
        return record.levelno >= logging.WARNING or _force or scope.get() in _enabled


def install(force: bool = False) -> None:
    """Route app.* and hoymiles_wifi logging through one stdout handler (stdout
    reaches the Homey diagnostic report). Without a handler Python drops anything
    below WARNING."""
    global _force
    _force = force
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    handler.addFilter(_Filter())
    for name in ("app", "hoymiles_wifi"):
        logger = logging.getLogger(name)
        if any(isinstance(f, _Filter) for h in logger.handlers for f in h.filters):
            continue
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False


def set_enabled(key: str, on: bool) -> None:
    (_enabled.add if on else _enabled.discard)(key)


def active(key: str | None) -> bool:
    return _force or key in _enabled

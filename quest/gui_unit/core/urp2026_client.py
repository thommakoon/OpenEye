"""Client for the URP2026 phone PC-bridge (:8765).

Mirrors motorola/pc/urp2026_qtpy_cmd.py so the OpenEye GUI can drive the same
QT Py CMD:* firmware state machine and "Start/Stop both recording" flow.

    PC --HTTP Wi-Fi--> URP2026 (:8765) --USB serial CMD:*--> QT Py

Kept self-contained (stdlib only) to avoid coupling the OpenEye submodule to the
motorola/ CLI across the repo boundary.
"""

from __future__ import annotations

import urllib.error
import urllib.request

DEFAULT_PORT = 8765

# command -> bridge path
COMMAND_PATHS = {
    "health": "/health",
    "calibrate": "/qtpy/calibrate",
    "start": "/qtpy/start",
    "stop": "/qtpy/stop",
    "status": "/qtpy/status",
    "next": "/qtpy/next",
    "record_start": "/record/start",
    "record_stop": "/record/stop",
}

COMMANDS = tuple(COMMAND_PATHS.keys())


class Urp2026Error(Exception):
    """Raised when the bridge is unreachable or returns an HTTP error."""


def command_path(command: str) -> str:
    try:
        return COMMAND_PATHS[command]
    except KeyError as e:
        raise Urp2026Error(f"Unknown command: {command}") from e


def build_url(host: str, port: int, command: str) -> str:
    return f"http://{host}:{port}{command_path(command)}"


def send_command(
    host: str,
    command: str,
    port: int = DEFAULT_PORT,
    timeout: float = 30.0,
) -> tuple[int, str]:
    """Send one bridge command. Returns (http_status, body).

    Raises Urp2026Error if the phone bridge cannot be reached at all.
    """
    url = build_url(host, port, command)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body.rstrip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body.rstrip()
    except Exception as e:
        raise Urp2026Error(
            f"Request failed: {e}. Check: phone+PC same Wi-Fi, URP2026 PC bridge "
            f"running, QT Py USB connected."
        ) from e

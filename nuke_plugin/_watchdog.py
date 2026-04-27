"""Crash watchdog for the Nuke addon.

Tracks consecutive handler failures inside ``_dispatch``. After
``CRASH_THRESHOLD`` failures in a row we atomically write a marker file
at ``~/.nuke_mcp/crash_marker.json`` so the MCP-side ``connection``
layer can surface a "session lost ~Xm ago" warning on the next
reconnect.

Phase A5 of the cuddly-cray plan. Two non-obvious choices documented:

* No auto-save of Nuke script state. The whole point of the marker is
  that we *don't* know whether the in-process state is corrupt; saving
  it would risk overwriting a good ``.nk`` with a half-mutated graph.
* Path is overridable via ``NUKE_MCP_MARKER_DIR`` so tests can isolate
  without monkey-patching ``os.path.expanduser`` (which is awkward
  cross-platform). Production code never sets the env var.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import traceback as _tb_mod
from pathlib import Path
from typing import Any

log = logging.getLogger("nuke_mcp.watchdog")

CRASH_THRESHOLD = 3
MARKER_FILENAME = "crash_marker.json"
_MARKER_DIR_ENV = "NUKE_MCP_MARKER_DIR"

_lock = threading.Lock()
_consecutive_failures = 0


def _marker_dir() -> Path:
    """Resolve the marker directory.

    Honours ``NUKE_MCP_MARKER_DIR`` for tests; defaults to
    ``~/.nuke_mcp``. Created on demand by ``_write_marker``.
    """
    override = os.environ.get(_MARKER_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".nuke_mcp"


def marker_path() -> Path:
    """Public accessor used by tests and ``connection.connect``."""
    return _marker_dir() / MARKER_FILENAME


def _write_marker(payload: dict[str, Any]) -> None:
    """Atomically write the marker file. Never raises.

    Uses ``tempfile.NamedTemporaryFile`` in the marker directory + an
    ``os.replace`` so a partially-written marker is never visible to
    ``connection.connect``.
    """
    target = marker_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=".crash_marker.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, separators=(",", ":"))
            tmp_path = tmp.name
        os.replace(tmp_path, target)
    except OSError:
        log.exception("watchdog: failed to write crash marker at %s", target)


def record_failure(tool_name: str, request_id: str | None, exc: BaseException) -> None:
    """Note a handler failure. On the ``CRASH_THRESHOLD``-th consecutive
    failure, write the crash marker.

    Safe to call from any thread; the addon dispatch loop runs handlers
    on Nuke's main thread but the watchdog state is shared.
    """
    global _consecutive_failures
    with _lock:
        _consecutive_failures += 1
        count = _consecutive_failures

    if count < CRASH_THRESHOLD:
        return

    payload = {
        "last_tool": tool_name,
        "last_request_id": request_id,
        "traceback": "".join(_tb_mod.format_exception(type(exc), exc, exc.__traceback__)),
        "timestamp": time.time(),
        "consecutive_failures": count,
    }
    _write_marker(payload)


def record_success() -> None:
    """Reset the consecutive-failure counter. Called on every successful
    ``_dispatch`` so a flaky handler doesn't trip the watchdog over time.
    """
    global _consecutive_failures
    with _lock:
        _consecutive_failures = 0


def reset_for_tests() -> None:
    """Test-only helper: clear in-process state between cases."""
    global _consecutive_failures
    with _lock:
        _consecutive_failures = 0


def consecutive_failures() -> int:
    """Read the counter under the lock. Test-only."""
    with _lock:
        return _consecutive_failures

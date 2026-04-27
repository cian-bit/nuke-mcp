"""Tests for the A5 crash watchdog + reconnect-warning injection.

Covers two halves:
* ``nuke_plugin/_watchdog.py`` -- consecutive-failure counter + atomic
  marker write at ~/.nuke_mcp/crash_marker.json (overridable via
  ``NUKE_MCP_MARKER_DIR`` for tests).
* ``src/nuke_mcp/connection.py`` -- crash-marker pickup on connect()
  and one-shot warning injection on the next send() response.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time

import pytest

from nuke_mcp import connection

# Load the watchdog module the same way conftest.py loads addon.py: by
# file path, so we don't need the nuke_plugin package on sys.path.
_WD_PATH = pathlib.Path(__file__).resolve().parents[1] / "nuke_plugin" / "_watchdog.py"
_wd_spec = importlib.util.spec_from_file_location("_nuke_watchdog_for_tests", _WD_PATH)
assert _wd_spec is not None and _wd_spec.loader is not None
_watchdog = importlib.util.module_from_spec(_wd_spec)
_wd_spec.loader.exec_module(_watchdog)


@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    """Point both watchdog and connection at an isolated marker dir."""
    monkeypatch.setenv("NUKE_MCP_MARKER_DIR", str(tmp_path))
    _watchdog.reset_for_tests()
    # connection's pending-warning state is module-global -- clear it so
    # cross-test pollution doesn't leak in.
    connection._pending_warning = None
    yield tmp_path
    connection._pending_warning = None
    _watchdog.reset_for_tests()


# ---------------------------------------------------------------------------
# watchdog: failure counter + marker write
# ---------------------------------------------------------------------------


def test_first_two_failures_do_not_write_marker(marker_dir):
    """Counter increments but no marker until the third strike."""
    marker = marker_dir / "crash_marker.json"
    _watchdog.record_failure("create_node", "rid-1", RuntimeError("boom"))
    assert _watchdog.consecutive_failures() == 1
    assert not marker.exists()

    _watchdog.record_failure("create_node", "rid-2", RuntimeError("boom"))
    assert _watchdog.consecutive_failures() == 2
    assert not marker.exists()


def test_third_consecutive_failure_writes_marker(marker_dir):
    """3rd consecutive failure writes a complete, valid marker payload."""
    marker = marker_dir / "crash_marker.json"
    for i in range(3):
        try:
            raise ValueError(f"failure-{i}")
        except ValueError as exc:
            _watchdog.record_failure("set_knob", f"rid-{i}", exc)

    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["last_tool"] == "set_knob"
    assert data["last_request_id"] == "rid-2"
    assert data["consecutive_failures"] == 3
    assert "ValueError" in data["traceback"]
    assert "failure-2" in data["traceback"]
    assert isinstance(data["timestamp"], int | float)


def test_record_success_resets_counter(marker_dir):
    """A success between two failures keeps the counter from tripping."""
    marker = marker_dir / "crash_marker.json"
    _watchdog.record_failure("a", "1", RuntimeError("x"))
    _watchdog.record_failure("a", "2", RuntimeError("x"))
    _watchdog.record_success()
    assert _watchdog.consecutive_failures() == 0

    _watchdog.record_failure("a", "3", RuntimeError("x"))
    _watchdog.record_failure("a", "4", RuntimeError("x"))
    assert not marker.exists(), "two post-success failures should not trip the watchdog"


def test_marker_write_is_atomic(marker_dir, monkeypatch):
    """The marker is written via a temp file + os.replace.

    We patch ``os.replace`` to record what's getting moved into place; the
    target stays absent until the rename, so a partially-written marker
    is never observable. We also assert the temp filename lives in the
    same dir (so os.replace can be atomic on Windows).
    """
    import os as os_mod

    marker = marker_dir / "crash_marker.json"
    seen: list[tuple[str, str]] = []
    real_replace = os_mod.replace

    def fake_replace(src, dst):
        seen.append((str(src), str(dst)))
        # Confirm the temp file existed *and* the target did not, before
        # we performed the rename.
        assert pathlib.Path(src).exists(), "temp file should exist before rename"
        assert not pathlib.Path(dst).exists(), "target should not exist before atomic rename"
        return real_replace(src, dst)

    monkeypatch.setattr(_watchdog.os, "replace", fake_replace)

    for i in range(3):
        _watchdog.record_failure("op", f"rid-{i}", RuntimeError("x"))

    assert len(seen) == 1, "exactly one rename for the threshold-tripping write"
    src, dst = seen[0]
    assert pathlib.Path(src).parent == marker_dir, "temp file must be in marker dir for atomicity"
    assert pathlib.Path(dst) == marker
    assert marker.exists()


def test_failed_marker_replace_removes_temp_file(marker_dir, monkeypatch):
    """If os.replace fails, the temp marker is cleaned up."""

    def fake_replace(_src, _dst):
        raise PermissionError("destination is locked")

    monkeypatch.setattr(_watchdog.os, "replace", fake_replace)

    for i in range(3):
        _watchdog.record_failure("op", f"rid-{i}", RuntimeError("x"))

    leftovers = list(marker_dir.glob(".crash_marker.*.tmp"))
    assert leftovers == []


def test_addon_fallback_watchdog_module_is_singleton():
    """The addon fallback registers _watchdog in sys.modules."""
    module = sys.modules.get("nuke_mcp_addon._watchdog")
    assert module is not None
    assert module is sys.modules["nuke_mcp_addon._watchdog"]


# ---------------------------------------------------------------------------
# connection: marker pickup + warning injection
# ---------------------------------------------------------------------------


def test_connect_with_no_marker_yields_no_warning(marker_dir, connected):
    """No marker file => no warning stashed, no warning injected."""
    assert connection._pending_warning is None
    result = connection.send("ping", _class="ping")
    assert "warning" not in result


def test_connect_with_stale_marker_ignores_and_deletes(marker_dir, mock_server):
    """A marker older than 1h is dropped without producing a warning."""
    marker = marker_dir / "crash_marker.json"
    payload = {
        "last_tool": "render",
        "last_request_id": "old-rid",
        "traceback": "old",
        "timestamp": time.time() - 7200,
        "consecutive_failures": 3,
    }
    marker.write_text(json.dumps(payload), encoding="utf-8")
    # backdate mtime so connection's freshness check sees it as stale
    stale = time.time() - 7200
    import os as os_mod

    os_mod.utime(marker, (stale, stale))

    _, port = mock_server
    connection.connect("localhost", port)
    try:
        assert connection._pending_warning is None
        assert not marker.exists(), "stale marker should be deleted on connect"
        result = connection.send("ping", _class="ping")
        assert "warning" not in result
    finally:
        connection.disconnect()


def test_fresh_marker_injects_warning_into_next_send(marker_dir, mock_server):
    """A fresh marker => warning lands on the next send() response, exactly once."""
    marker = marker_dir / "crash_marker.json"
    payload = {
        "last_tool": "create_node",
        "last_request_id": "rid-abc",
        "traceback": "Traceback...",
        "timestamp": time.time() - 90,  # 1.5 minutes ago
        "consecutive_failures": 3,
    }
    marker.write_text(json.dumps(payload), encoding="utf-8")
    import os as os_mod

    fresh = time.time() - 90
    os_mod.utime(marker, (fresh, fresh))

    _, port = mock_server
    connection.connect("localhost", port)
    try:
        assert not marker.exists(), "marker should be consumed on connect"
        assert connection._pending_warning is not None
        assert connection._pending_warning["last_request_id"] == "rid-abc"

        first = connection.send("get_script_info")
        assert "warning" in first
        assert "create_node" in first["warning"]
        assert first["last_request_id"] == "rid-abc"

        # warning is one-shot: the second call no longer carries it
        second = connection.send("get_script_info")
        assert "warning" not in second
        assert "last_request_id" not in second
        assert connection._pending_warning is None
    finally:
        connection.disconnect()


def test_marker_missing_means_no_warning(marker_dir, mock_server):
    """Sanity: an absent marker file is the no-op path (not an error)."""
    _, port = mock_server
    connection.connect("localhost", port)
    try:
        assert connection._pending_warning is None
    finally:
        connection.disconnect()


def test_non_dict_response_does_not_consume_pending_warning(marker_dir):
    """A scalar/list result cannot carry a warning, so replay it later."""
    connection._pending_warning = {"warning": "session lost", "last_request_id": "rid"}
    assert connection._consume_pending_warning(["not", "a", "dict"]) == ["not", "a", "dict"]
    assert connection._pending_warning is not None

    merged = connection._consume_pending_warning({"ok": True})
    assert merged["warning"] == "session lost"
    assert merged["last_request_id"] == "rid"
    assert connection._pending_warning is None

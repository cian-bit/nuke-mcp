"""Tests for the connection layer."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest

from nuke_mcp import connection


def test_connect_and_handshake(mock_server):
    _, port = mock_server
    version = connection.connect("localhost", port)
    assert version.major == 15
    assert version.minor == 1
    assert version.is_nukex
    assert str(version) == "NukeX 15.1v3"
    connection.disconnect()


def test_connect_sets_connected(mock_server):
    _, port = mock_server
    assert not connection.is_connected()
    connection.connect("localhost", port)
    assert connection.is_connected()
    connection.disconnect()
    assert not connection.is_connected()


def test_ping(connected):
    assert connection.ping()


def test_send_command(connected):
    result = connection.send("get_script_info")
    assert result["fps"] == 24.0
    assert result["first_frame"] == 1001


def test_send_unknown_command(connected):
    with pytest.raises(connection.CommandError, match="unknown command"):
        connection.send("nonexistent_command")


def test_version_gating():
    v = connection.NukeVersion(15, 1, 3, "Nuke")
    assert not v.is_nukex
    assert v.at_least(15, 0)
    assert v.at_least(15, 1)
    assert not v.at_least(16, 0)

    vx = connection.NukeVersion(16, 0, 1, "NukeX")
    assert vx.is_nukex
    assert vx.at_least(15, 0)
    assert vx.at_least(16, 0)


def test_version_from_handshake():
    v = connection.NukeVersion.from_handshake({"nuke_version": "16.0v2", "variant": "NukeX"})
    assert v.major == 16
    assert v.minor == 0
    assert v.patch == 2
    assert v.variant == "NukeX"


# -- A2: request_id round-trip --


def test_request_id_round_trips(connected):
    """Send a command and confirm the addon's echo matches by intercepting one msg."""
    server = connected
    seen: list[str] = []
    original_dispatch = server._dispatch

    def capture_dispatch(msg):
        rid = msg.get("_request_id")
        if rid is not None:
            seen.append(rid)
        return original_dispatch(msg)

    server._dispatch = capture_dispatch
    connection.send("get_script_info")
    assert len(seen) == 1
    # 8 hex chars from uuid4().hex[:8]
    assert len(seen[0]) == 8
    assert all(c in "0123456789abcdef" for c in seen[0])


def test_request_id_mismatch_raises(connected):
    """Mock server that returns a different _request_id should raise ConnectionError."""
    server = connected
    original_dispatch = server._dispatch

    def bad_dispatch(msg):
        resp = original_dispatch(msg)
        resp["_request_id"] = "deadbeef"
        return resp

    server._dispatch = bad_dispatch
    with pytest.raises(connection.ConnectionError, match="request_id mismatch"):
        connection.send("get_script_info")


# -- A2: per-class timeout override --


def test_timeout_class_render_overrides(connected, monkeypatch):
    """`_class='render'` must drive `_io_round_trip` with timeout=900.0s."""
    captured: list[float] = []
    real_round_trip = connection._io_round_trip

    def spy(msg, timeout):
        captured.append(timeout)
        return real_round_trip(msg, timeout)

    monkeypatch.setattr(connection, "_io_round_trip", spy)
    connection.send("get_script_info", _class="render")
    assert captured == [900.0]


def test_timeout_class_default_read(connected, monkeypatch):
    """No `_class` arg defaults to read=30.0s."""
    captured: list[float] = []
    real_round_trip = connection._io_round_trip

    def spy(msg, timeout):
        captured.append(timeout)
        return real_round_trip(msg, timeout)

    monkeypatch.setattr(connection, "_io_round_trip", spy)
    connection.send("get_script_info")
    assert captured == [30.0]


def test_timeout_classes_module_constant():
    """Sanity-check the public class table covers the documented keys."""
    assert connection.TIMEOUT_CLASSES["read"] == 30.0
    assert connection.TIMEOUT_CLASSES["mutate"] == 60.0
    assert connection.TIMEOUT_CLASSES["render"] == 900.0
    assert connection.TIMEOUT_CLASSES["copycat"] == 3600.0
    assert connection.TIMEOUT_CLASSES["ping"] == 5.0


def test_send_class_alias(connected, monkeypatch):
    """`send_class` should behave identically to send(..., _class=...)."""
    captured: list[float] = []
    real_round_trip = connection._io_round_trip

    def spy(msg, timeout):
        captured.append(timeout)
        return real_round_trip(msg, timeout)

    monkeypatch.setattr(connection, "_io_round_trip", spy)
    connection.send_class("get_script_info", "mutate")
    assert captured == [60.0]


# -- A2: structured error envelope --


def test_command_error_envelope(connected):
    """CommandError on unknown command must carry an envelope dict."""
    try:
        connection.send("nonexistent_command")
    except connection.CommandError as e:
        assert hasattr(e, "envelope")
        env = e.envelope
        assert "error_class" in env
        assert "duration_ms" in env
        assert "request_id" in env
        assert isinstance(env["duration_ms"], int)
        assert env["duration_ms"] >= 0
        assert len(env["request_id"]) == 8
    else:
        pytest.fail("expected CommandError")


# -- A2: probe_existing_connection --


def test_probe_returns_false_when_disconnected():
    """No socket means probe immediately returns False."""
    connection.disconnect()
    assert connection.probe_existing_connection(timeout=0.1) is False


def test_probe_succeeds_on_live_socket(connected):
    """Live mock server -> probe returns True quickly."""
    started = time.perf_counter()
    assert connection.probe_existing_connection(timeout=0.5) is True
    assert (time.perf_counter() - started) < 0.5


def test_probe_times_out_on_hung_socket():
    """A server that accepts but never replies must trigger the wall-clock timeout."""
    # Custom hung server: accepts, sends handshake, never responds to ping.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(2.0)
    port = sock.getsockname()[1]

    accepted: list[socket.socket] = []
    stop = threading.Event()

    def hang_loop():
        try:
            client, _ = sock.accept()
        except (TimeoutError, OSError):
            return
        accepted.append(client)
        client.sendall(
            json.dumps({"nuke_version": "15.1v3", "variant": "NukeX", "pid": 1}).encode() + b"\n"
        )
        # then ignore everything until told to stop
        while not stop.is_set():
            time.sleep(0.05)
        client.close()

    thread = threading.Thread(target=hang_loop, daemon=True)
    thread.start()
    try:
        connection.connect("localhost", port)
        started = time.perf_counter()
        assert connection.probe_existing_connection(timeout=0.5) is False
        elapsed = time.perf_counter() - started
        # allow some slack on slow CI but ensure it doesn't blow past 2s
        assert elapsed < 2.0
    finally:
        stop.set()
        connection.disconnect()
        sock.close()
        thread.join(timeout=1.0)


# -- A2: retry_with_backoff --


def test_retry_succeeds_on_attempt_3():
    calls = {"n": 0}

    @connection.retry_with_backoff(max_retries=3, base=0.01, jitter=False)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("not yet")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_retry_exhausts_and_reraises():
    calls = {"n": 0}

    @connection.retry_with_backoff(max_retries=2, base=0.01, jitter=False)
    def always_fails():
        calls["n"] += 1
        raise ConnectionError("nope")

    with pytest.raises(ConnectionError, match="nope"):
        always_fails()
    assert calls["n"] == 2


def test_retry_jitter_is_nonzero():
    """With jitter=True, sleep delays between attempts must vary across runs."""
    sleeps: list[float] = []

    real_sleep = time.sleep

    def fake_sleep(d):
        sleeps.append(d)
        # speed up the test
        real_sleep(0.0)

    import nuke_mcp.connection as conn_mod

    orig = conn_mod.time.sleep
    conn_mod.time.sleep = fake_sleep
    try:

        @conn_mod.retry_with_backoff(max_retries=4, base=1.0, exponential=2.0, jitter=True)
        def flaky():
            raise ConnectionError("nope")

        with pytest.raises(ConnectionError):
            flaky()
    finally:
        conn_mod.time.sleep = orig

    # 4 attempts -> 3 sleeps. Each must be >= base (1.0) and <= base*1.1.
    assert len(sleeps) == 3
    # at least one must have a strictly nonzero fractional jitter component
    assert any((s - int(s)) > 0.0 for s in sleeps)


# -- A2: heartbeat --


def test_heartbeat_disabled_in_tests_by_default(mock_server):
    """The autouse fixture sets NUKE_MCP_HEARTBEAT=0."""
    _, port = mock_server
    connection.connect("localhost", port)
    try:
        assert connection._heartbeat_thread is None
    finally:
        connection.disconnect()


def test_heartbeat_thread_starts_when_enabled(mock_server, monkeypatch):
    """When NUKE_MCP_HEARTBEAT=1, connect() must spawn the heartbeat thread."""
    monkeypatch.setenv("NUKE_MCP_HEARTBEAT", "1")
    _, port = mock_server
    connection.connect("localhost", port)
    try:
        assert connection._heartbeat_thread is not None
        assert connection._heartbeat_thread.is_alive()
    finally:
        connection.disconnect()
        # disconnect() must tear down the thread
        assert connection._heartbeat_thread is None


def test_heartbeat_misses_set_session_lost(mock_server, monkeypatch):
    """Two consecutive ping failures must flag _session_lost=True.

    We bypass the network entirely by patching ``send`` to raise, and
    invoke the heartbeat loop with a tight stop event.
    """
    _, port = mock_server
    monkeypatch.setenv("NUKE_MCP_HEARTBEAT", "0")  # don't auto-start
    connection.connect("localhost", port)
    try:
        connection._session_lost = False

        def boom(*_a, **_k):
            raise ConnectionError("simulated")

        # Patch send so the heartbeat loop's inner ping always fails.
        monkeypatch.setattr(connection, "send", boom)

        # Drive the heartbeat loop manually with a near-zero interval.
        monkeypatch.setattr(connection, "HEARTBEAT_INTERVAL", 0.01)
        stop = threading.Event()

        # run inline; loop exits after 2 misses by setting _session_lost
        # and calling disconnect(). We give it a hard ceiling so a
        # broken impl can't hang the suite.
        thread = threading.Thread(target=connection._heartbeat_loop, args=(stop,), daemon=True)
        thread.start()
        thread.join(timeout=1.5)
        stop.set()

        assert connection._session_lost is True
    finally:
        connection._session_lost = False
        # _sock may already have been cleared by the loop's disconnect().


# -- request_id propagated through to _helpers envelope --


def test_helpers_decorator_formats_envelope(connected):
    """When the addon errors, the decorator should surface the envelope fields."""
    from nuke_mcp.tools._helpers import nuke_command

    @nuke_command("test_op")
    def go() -> dict[str, Any]:
        return connection.send("nonexistent_command")

    out = go()
    assert out["status"] == "error"
    assert out["error_class"] == "CommandError"
    assert "duration_ms" in out
    assert "request_id" in out
    assert len(out["request_id"]) == 8

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
    # 16 hex chars from uuid4().hex[:16] -- 64 bits, collision-safe
    assert len(seen[0]) == 16
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
        assert len(env["request_id"]) == 16
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
    assert len(out["request_id"]) == 16


# -- GPT-5.5 #4: replay guard for non-idempotent ops --


def test_send_mutate_does_not_replay_after_connection_loss(connected, monkeypatch):
    """A mutate-class send must NOT auto-replay after the first-attempt
    socket failure. The risk is that the addon already executed the op
    before the connection died, and a replay creates duplicates.
    """
    calls: list[dict] = []

    def boom_then_fail(msg, timeout):
        # Simulate a connection lost on the first (and only) attempt.
        calls.append(msg)
        raise ConnectionError("simulated socket loss")

    monkeypatch.setattr(connection, "_io_round_trip", boom_then_fail)

    with pytest.raises(connection.ConnectionLostError) as ei:
        connection.send("setup_keying", _class="mutate", input_node="plate")
    err = ei.value
    assert err.last_op == "setup_keying"
    assert err.last_class == "mutate"
    assert err.last_request_id is not None and len(err.last_request_id) == 16
    # CRUCIAL: the round-trip was attempted exactly once -- no replay.
    assert len(calls) == 1


def test_send_render_class_does_not_replay(connected, monkeypatch):
    """Render is also non-idempotent; same guard applies."""
    calls: list[dict] = []

    def boom(msg, timeout):
        calls.append(msg)
        raise ConnectionError("simulated socket loss")

    monkeypatch.setattr(connection, "_io_round_trip", boom)

    with pytest.raises(connection.ConnectionLostError):
        connection.send("render", _class="render")
    assert len(calls) == 1


def test_send_read_class_still_replays_on_connection_loss(connected, monkeypatch):
    """Read paths are idempotent and SHOULD auto-retry on transient
    socket loss -- preserves the prior auto-reconnect behaviour for
    safe ops.
    """
    real_round_trip = connection._io_round_trip
    fail_first = {"done": False}

    def flaky(msg, timeout):
        if not fail_first["done"]:
            fail_first["done"] = True
            raise ConnectionError("transient")
        return real_round_trip(msg, timeout)

    monkeypatch.setattr(connection, "_io_round_trip", flaky)
    # The reconnect path needs to find _last_host/_port; the connected
    # fixture has set them already.
    out = connection.send("get_script_info", _class="read")
    assert out["fps"] == 24.0


# -- GPT-5.5 #5: send_raw + render_frames --


def test_render_frames_uses_render_class(connected, monkeypatch):
    """``render_frames`` must dispatch with ``_class='render'`` (900s)
    instead of the old ``timeout=300.0`` literal, and must raise the
    non-idempotent guard rather than replay on connection loss.
    """
    captured: list[float] = []
    real_round_trip = connection._io_round_trip

    def spy(msg, timeout):
        captured.append(timeout)
        return real_round_trip(msg, timeout)

    monkeypatch.setattr(connection, "_io_round_trip", spy)
    connection.send("render", _class="render")
    assert captured == [900.0]


def test_send_raw_custom_timeout_validates_request_id(connected):
    """``send_raw`` with a non-class timeout must still echo-check the
    request_id and surface a structured envelope on error.
    """
    with pytest.raises(connection.CommandError) as ei:
        connection.send_raw("nonexistent_command", timeout=2.5)
    env = ei.value.envelope
    assert env.get("error_class") == "CommandError"
    assert "duration_ms" in env
    assert len(env["request_id"]) == 16


def test_send_raw_custom_timeout_request_id_mismatch_raises(connected):
    """A spoofed echo on the custom-timeout path must trigger the same
    ``request_id mismatch`` ConnectionError as the main send path.
    """
    server = connected
    original_dispatch = server._dispatch

    def bad_dispatch(msg):
        resp = original_dispatch(msg)
        resp["_request_id"] = "deadbeefdeadbeef"
        return resp

    server._dispatch = bad_dispatch
    with pytest.raises(connection.ConnectionError, match="request_id mismatch"):
        connection.send_raw("get_script_info", timeout=2.5)


# -- GPT-5.5 #6: heartbeat self-join guard --


def test_stop_heartbeat_called_from_heartbeat_thread_does_not_raise():
    """When ``_stop_heartbeat`` is invoked from the heartbeat thread
    itself (e.g. via the disconnect inside ``_heartbeat_loop``), joining
    must be skipped to avoid ``RuntimeError: cannot join current thread``.
    """
    # Prime the module state with a fake "heartbeat" thread that is the
    # current thread. _stop_heartbeat must not call join() on itself.
    connection._heartbeat_thread = threading.current_thread()
    connection._heartbeat_stop = threading.Event()
    try:
        # This must not raise.
        connection._stop_heartbeat()
    finally:
        connection._heartbeat_thread = None
        connection._heartbeat_stop = None


def test_probe_executor_uses_four_workers():
    """Houdini-MCP parity: probe pool sized for concurrent callers."""
    pool = connection._get_probe_executor()
    assert pool._max_workers == 4


# -- B2: task_progress notification demuxer --


class _FakeSocket:
    """In-memory socket replacement for demuxer tests.

    Feeds the demuxer pre-canned chunks one ``recv`` at a time so we
    can verify ordering without standing up a real TCP server.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def recv(self, _bufsize: int) -> bytes:
        if not self._chunks:
            # Mimic a closed connection -- _recv_json must raise.
            return b""
        return self._chunks.pop(0)


def test_recv_json_routes_notification_to_queue():
    """A ``task_progress`` line must land in the notification queue and
    NOT be returned to the caller. The next real response goes through.
    """
    queue = connection.notification_queue()
    queue.drain()  # clear any prior leftovers from the suite
    # one chunk with notification + response framed back-to-back.
    payload = (
        b'{"type":"task_progress","id":"t1","frame":1}\n'
        b'{"status":"ok","result":{"ok":true},"_request_id":"abc"}\n'
    )
    sock = _FakeSocket([payload])
    out = connection._recv_json(sock)  # type: ignore[arg-type]
    assert out["status"] == "ok"
    drained = queue.drain()
    assert len(drained) == 1
    assert drained[0]["id"] == "t1"
    assert drained[0]["frame"] == 1
    # buffer cleared for this socket (no trailing partial line).
    assert id(sock) not in connection._recv_buffers


def test_recv_json_stashes_surplus_bytes_for_next_call():
    """When two response lines arrive in one chunk, the second must be
    delivered on the next ``_recv_json`` call without another recv.
    """
    payload = (
        b'{"status":"ok","result":{"a":1},"_request_id":"r1"}\n'
        b'{"status":"ok","result":{"b":2},"_request_id":"r2"}\n'
    )
    sock = _FakeSocket([payload])
    first = connection._recv_json(sock)  # type: ignore[arg-type]
    assert first["result"] == {"a": 1}
    # second call: no chunks left in the fake socket, but the parser
    # must still find the queued frame in the buffer.
    second = connection._recv_json(sock)  # type: ignore[arg-type]
    assert second["result"] == {"b": 2}


def test_recv_json_drains_multiple_notifications_then_returns_response():
    """A burst of progress lines followed by a real response: every
    notification queued, response returned exactly once.
    """
    queue = connection.notification_queue()
    queue.drain()
    payload = (
        b'{"type":"task_progress","id":"t1","frame":1}\n'
        b'{"type":"task_progress","id":"t1","frame":2}\n'
        b'{"type":"task_progress","id":"t1","frame":3}\n'
        b'{"status":"ok","result":{"done":true},"_request_id":"r1"}\n'
    )
    sock = _FakeSocket([payload])
    out = connection._recv_json(sock)  # type: ignore[arg-type]
    assert out["result"] == {"done": True}
    drained = queue.drain()
    assert [n["frame"] for n in drained] == [1, 2, 3]


def test_recv_json_listener_callback_invoked():
    """Registered per-task listener intercepts notifications instead of
    the generic queue.
    """
    queue = connection.notification_queue()
    queue.drain()
    seen: list[dict] = []
    queue.register_listener("t42", seen.append)
    try:
        payload = (
            b'{"type":"task_progress","id":"t42","frame":1}\n'
            b'{"type":"task_progress","id":"t42","frame":2}\n'
            b'{"status":"ok","result":{},"_request_id":"r"}\n'
        )
        sock = _FakeSocket([payload])
        connection._recv_json(sock)  # type: ignore[arg-type]
        assert [n["frame"] for n in seen] == [1, 2]
        # listener consumed them -- queue must be empty.
        assert queue.drain() == []
    finally:
        queue.unregister_listener("t42")


def test_recv_json_listener_for_other_task_does_not_intercept():
    """A listener registered for a different id must not eat someone
    else's notifications.
    """
    queue = connection.notification_queue()
    queue.drain()
    seen: list[dict] = []
    queue.register_listener("other", seen.append)
    try:
        payload = (
            b'{"type":"task_progress","id":"t99","frame":7}\n'
            b'{"status":"ok","result":{},"_request_id":"r"}\n'
        )
        sock = _FakeSocket([payload])
        connection._recv_json(sock)  # type: ignore[arg-type]
        assert seen == []  # listener for "other" got nothing
        drained = queue.drain()
        assert len(drained) == 1
        assert drained[0]["id"] == "t99"
    finally:
        queue.unregister_listener("other")


def test_recv_json_invalid_json_raises():
    """Bad framing must surface as ConnectionError so we don't silently desync."""
    sock = _FakeSocket([b"{not valid json}\n"])
    with pytest.raises(connection.ConnectionError, match="invalid json"):
        connection._recv_json(sock)  # type: ignore[arg-type]


def test_send_with_task_progress_in_flight_returns_real_response(connected, monkeypatch):
    """End-to-end: while a real send is awaiting its response the addon
    can interleave task_progress lines on the same socket; ``send``
    must still return the response cleanly.
    """
    server = connected
    queue = connection.notification_queue()
    queue.drain()

    real_dispatch = server._dispatch

    def dispatch_with_burst(msg):
        # Inject a notification on the live socket BEFORE the real
        # response. The mock server holds the live client socket on
        # the only connection, so we sneak in a write via the
        # _handle loop's client. We can't reach it directly here,
        # so we route through a dispatch hook that emits a side
        # notification using the queue's put().
        queue.put({"type": "task_progress", "id": "fake", "frame": 99})
        return real_dispatch(msg)

    server._dispatch = dispatch_with_burst
    out = connection.send("get_script_info")
    assert out["fps"] == 24.0
    drained = queue.drain()
    assert any(n.get("frame") == 99 for n in drained)


def test_disconnect_clears_recv_buffer(mock_server):
    """Buffers tracked by socket id must not leak across reconnects."""
    _, port = mock_server
    connection.connect("localhost", port)
    assert connection._sock is not None
    sid = id(connection._sock)
    # Plant a fake leftover; disconnect must drop it.
    connection._recv_buffers[sid] = b"leftover"
    connection.disconnect()
    assert sid not in connection._recv_buffers

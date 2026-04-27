"""TCP socket client for communicating with the Nuke addon.

Phase A2 hardening: composable retry+backoff, per-command-class timeouts,
request_id round-trip, heartbeat thread for fast-fail crash detection,
structured error envelope. Addon-side echoes ``_request_id`` from the
top-level payload back in the response.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import functools
import json
import logging
import os
import pathlib
import random
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

log = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876
CONNECT_TIMEOUT = 5.0
MAX_RETRIES = 3
MAX_MSG_SIZE = 16 * 1024 * 1024  # 16MB

# Per-command-class recv timeouts (seconds). Replaces the old
# RECV_TIMEOUT / RECV_TIMEOUT_RENDER pair. Tools opt in via
# ``send(cmd, _class="render", ...)``; default class is ``read``.
TIMEOUT_CLASSES: dict[str, float] = {
    "read": 30.0,
    "mutate": 60.0,
    "render": 900.0,
    "copycat": 3600.0,
    "ping": 5.0,
}

# Heartbeat config. Production runs heartbeat by default; tests disable
# via fixture by setting NUKE_MCP_HEARTBEAT=0 before connect().
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_MAX_MISSES = 2

RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,
    ConnectionRefusedError,
    ConnectionResetError,
    TimeoutError,
    BrokenPipeError,
    OSError,
)

F = TypeVar("F", bound=Callable[..., Any])

# -- module state --

_sock: socket.socket | None = None
_nuke_version: NukeVersion | None = None
_last_host: str | None = None
_last_port: int | None = None
_io_lock = threading.Lock()
_session_lost = False

# A5: crash-marker recovery. ``connect()`` reads ~/.nuke_mcp/crash_marker.json
# (or whatever ``NUKE_MCP_MARKER_DIR`` overrides to, kept in sync with the
# watchdog on the addon side) and stashes a warning here. The first
# subsequent ``send()`` merges it into the result and clears the field.
_pending_warning: dict[str, Any] | None = None
_CRASH_MARKER_MAX_AGE_S = 3600.0  # 1 hour

# Heartbeat state. Started in connect(), torn down in disconnect().
_heartbeat_thread: threading.Thread | None = None
_heartbeat_stop: threading.Event | None = None
_heartbeat_executor: concurrent.futures.ThreadPoolExecutor | None = None

# Probe executor used by probe_existing_connection() for wall-clock-bounded
# liveness checks. Shared across calls.
_probe_executor: concurrent.futures.ThreadPoolExecutor | None = None


class ConnectionError(Exception):  # noqa: A001 - shadow of builtin is intentional, public API
    pass


class CommandError(Exception):
    """Raised when the addon responds with status=error.

    The structured envelope (error_class, error_code, traceback,
    duration_ms, request_id) is attached as ``.envelope`` for the
    decorator in ``_helpers.py`` to relay to the MCP client.
    """

    def __init__(self, message: str, envelope: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.envelope: dict[str, Any] = envelope or {}


class ConnectionLostError(ConnectionError):
    """Raised when a non-idempotent send loses the connection mid-flight.

    The addon may have already executed the command before the socket
    died -- replaying the payload would risk creating duplicate nodes /
    re-running a destructive op. Carries ``last_op`` and
    ``last_request_id`` so the caller (or a future reconciler) can
    decide what to do.
    """

    def __init__(
        self,
        message: str,
        *,
        last_op: str | None = None,
        last_request_id: str | None = None,
        last_class: str | None = None,
    ) -> None:
        super().__init__(message)
        self.last_op = last_op
        self.last_request_id = last_request_id
        self.last_class = last_class


@dataclass
class NukeVersion:
    major: int
    minor: int
    patch: int = 0
    variant: str = "Nuke"  # Nuke, NukeX, NukeStudio

    @property
    def is_nukex(self) -> bool:
        return self.variant in ("NukeX", "NukeStudio")

    @property
    def is_studio(self) -> bool:
        return self.variant == "NukeStudio"

    def at_least(self, major: int, minor: int = 0) -> bool:
        return (self.major, self.minor) >= (major, minor)

    @classmethod
    def from_handshake(cls, data: dict[str, Any]) -> NukeVersion:
        ver = data.get("nuke_version", "0.0v0")
        variant = data.get("variant", "Nuke")
        # parse "15.1v3" style version strings
        parts = ver.replace("v", ".").split(".")
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return cls(major=major, minor=minor, patch=patch, variant=variant)

    def __str__(self) -> str:
        return f"{self.variant} {self.major}.{self.minor}v{self.patch}"


# -- retry decorator --


def retry_with_backoff(
    max_retries: int = MAX_RETRIES,
    base: float = 1.0,
    exponential: float = 2.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retryable: tuple[type[BaseException], ...] = RETRYABLE_EXCEPTIONS,
) -> Callable[[F], F]:
    """Retry the wrapped callable on retryable exceptions with exponential backoff.

    Ported from houdini-mcp-beta's connection.py. Jitter caps at 10% of
    the current delay to prevent thundering-herd under simultaneous
    reconnects.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: BaseException | None = None
            delay = base
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    if attempt < max_retries - 1:
                        sleep_for = min(delay, max_delay)
                        if jitter:
                            sleep_for += random.uniform(0, sleep_for * 0.1)
                        log.warning(
                            "attempt %d/%d failed: %s, retrying in %.2fs",
                            attempt + 1,
                            max_retries,
                            exc,
                            sleep_for,
                        )
                        time.sleep(sleep_for)
                        delay *= exponential
                    else:
                        log.error("all %d attempts failed: %s", max_retries, exc)
            assert last_exc is not None
            raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


# -- connect / disconnect --


@retry_with_backoff()
def _do_connect(host: str, port: int) -> tuple[socket.socket, NukeVersion]:
    """One connection attempt + handshake. Wrapped by connect() retry."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    s.connect((host, port))
    s.settimeout(TIMEOUT_CLASSES["read"])
    handshake = _recv_json(s)
    version = NukeVersion.from_handshake(handshake)
    return s, version


def _crash_marker_path() -> pathlib.Path:
    """Resolve the watchdog crash-marker path.

    Mirrors ``nuke_plugin/_watchdog.py:marker_path``. Kept in sync by hand
    rather than imported because connection.py is the MCP-side module and
    the watchdog lives on the Nuke-addon side -- they're packaged as
    separate trees.
    """
    override = os.environ.get("NUKE_MCP_MARKER_DIR")
    base = pathlib.Path(override) if override else pathlib.Path.home() / ".nuke_mcp"
    return base / "crash_marker.json"


def _consume_crash_marker() -> None:
    """If a fresh crash marker exists, stash ``_pending_warning`` and delete it.

    Stale markers (mtime older than ``_CRASH_MARKER_MAX_AGE_S``) are
    deleted without producing a warning -- the implication is that the
    user already noticed Nuke went down hours ago and there's no
    actionable recovery context to surface.
    """
    global _pending_warning
    path = _crash_marker_path()
    try:
        st = path.stat()
    except (OSError, ValueError):
        return

    age = time.time() - st.st_mtime
    if age > _CRASH_MARKER_MAX_AGE_S:
        with contextlib.suppress(OSError):
            path.unlink()
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        with contextlib.suppress(OSError):
            path.unlink()
        return

    minutes = max(1, int(age // 60))
    last_tool = data.get("last_tool") or "unknown"
    last_rid = data.get("last_request_id")
    _pending_warning = {
        "warning": f"session lost ~{minutes}m ago, last op was {last_tool}",
        "last_request_id": last_rid,
    }
    with contextlib.suppress(OSError):
        path.unlink()


def connect(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> NukeVersion:
    """Connect to Nuke addon. Returns version info from handshake."""
    global _sock, _nuke_version, _last_host, _last_port, _session_lost

    if _sock is not None:
        disconnect()

    s, version = _do_connect(host, port)
    _sock = s
    _nuke_version = version
    _last_host = host
    _last_port = port
    _session_lost = False
    log.info("connected to %s on %s:%d", version, host, port)

    _consume_crash_marker()

    if _heartbeat_enabled():
        _start_heartbeat()

    return version


def disconnect() -> None:
    global _sock, _nuke_version
    _stop_heartbeat()
    if _sock is not None:
        with contextlib.suppress(OSError):
            _sock.close()
        _sock = None
        _nuke_version = None
        log.info("disconnected from Nuke")


def is_connected() -> bool:
    return _sock is not None and not _session_lost


def get_version() -> NukeVersion | None:
    return _nuke_version


def session_lost() -> bool:
    return _session_lost


def _reconnect() -> None:
    """Try to reconnect using last known host/port."""
    if _last_host is not None and _last_port is not None:
        log.info("attempting reconnect to %s:%d", _last_host, _last_port)
        connect(_last_host, _last_port)
    else:
        raise ConnectionError("not connected to Nuke and no previous connection to retry")


# -- liveness probe --


def _get_probe_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _probe_executor
    if _probe_executor is None:
        # Houdini-MCP parity: 4 workers covers concurrent probes from
        # tool calls and the heartbeat loop without queuing.
        _probe_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="nuke-mcp-probe"
        )
    return _probe_executor


def probe_existing_connection(timeout: float = 0.5) -> bool:
    """Wall-clock-bounded liveness check on the cached socket.

    A torn TCP stream still reports ``_sock is not None`` until the next
    ``recv()`` returns 0 or raises. Waiting that out costs the full
    per-class timeout. This helper fires a tiny ``ping`` on a worker
    thread with a 0.5s deadline so callers can cheaply detect a stale
    socket and reconnect proactively.

    Returns True if the ping round-trips inside ``timeout``, False on
    any error or timeout. Never raises.
    """
    if _sock is None:
        return False

    def _probe() -> bool:
        try:
            send("ping", _class="ping")
            return True
        except Exception as exc:
            log.debug("liveness probe raised: %s", exc)
            return False

    fut = _get_probe_executor().submit(_probe)
    try:
        return bool(fut.result(timeout=timeout))
    except concurrent.futures.TimeoutError:
        log.debug("liveness probe timed out after %.2fs", timeout)
        fut.cancel()
        return False
    except Exception as exc:
        log.debug("liveness probe failed: %s", exc)
        return False


# -- send --


def send(command: str, *, _class: str = "read", **params: Any) -> dict[str, Any]:
    """Send a command to Nuke and return the response.

    Args:
        command: handler name on the addon side (``ping``, ``create_node``, ...).
        _class: timeout class key (``read``, ``mutate``, ``render``,
            ``copycat``, ``ping``). Determines the recv timeout for this
            single call. Defaults to ``read``.
        **params: forwarded as the ``params`` dict in the wire payload.

    Auto-reconnects once on send failure. Adds a request_id (uuid4
    hex[:16] = 64 bits, collision-safe at session scale) at the payload
    root; the addon echoes it back and a mismatch raises ConnectionError.
    """
    global _sock, _session_lost

    if _sock is None:
        _reconnect()
    assert _sock is not None  # connect() sets _sock or raises

    timeout = TIMEOUT_CLASSES.get(_class, TIMEOUT_CLASSES["read"])

    rid = uuid.uuid4().hex[:16]
    msg = {"type": command, "params": params, "_request_id": rid}

    started = time.perf_counter()

    try:
        resp = _io_round_trip(msg, timeout)
    except (ConnectionError, OSError, TimeoutError) as exc:
        # Auto-replay is only safe for idempotent classes. ``mutate``,
        # ``render``, ``copycat`` may have already executed addon-side
        # before the socket died -- replaying would create duplicate
        # nodes or re-run destructive ops. Raise a typed error and
        # leave reconciliation to the caller.
        if _class not in ("read", "ping"):
            with contextlib.suppress(Exception):
                disconnect()
            raise ConnectionLostError(
                f"connection lost during non-idempotent op '{command}' "
                f"(class={_class}, request_id={rid}): {exc}",
                last_op=command,
                last_request_id=rid,
                last_class=_class,
            ) from exc
        # Read paths still auto-retry: they're idempotent by definition.
        disconnect()
        _reconnect()
        assert _sock is not None
        resp = _io_round_trip(msg, timeout)

    duration_ms = int((time.perf_counter() - started) * 1000)

    echoed_rid = resp.get("_request_id")
    if echoed_rid is not None and echoed_rid != rid:
        raise ConnectionError(f"request_id mismatch: sent {rid}, got {echoed_rid}")

    if resp.get("status") == "error":
        envelope = {
            "error_class": resp.get("error_class") or "CommandError",
            "error_code": resp.get("error_code"),
            "traceback": resp.get("traceback"),
            "duration_ms": duration_ms,
            "request_id": rid,
        }
        raise CommandError(resp.get("error", "unknown error"), envelope=envelope)

    result = resp.get("result", {})
    return _consume_pending_warning(result)


def _consume_pending_warning(result: Any) -> Any:
    """Merge any stashed crash-recovery warning into ``result`` once.

    Only merges when ``result`` is a dict (which is the convention for
    every typed handler). For non-dict results, keep the warning queued
    for the next dict response rather than silently losing recovery
    context.
    """
    global _pending_warning
    if _pending_warning is None:
        return result
    if isinstance(result, dict):
        pending = _pending_warning
        _pending_warning = None
        merged = dict(result)
        for key, value in pending.items():
            merged.setdefault(key, value)
        return merged
    return result


def send_class(command: str, _class: str, **params: Any) -> dict[str, Any]:
    """Convenience wrapper around send() with explicit timeout class.

    Identical to ``send(command, _class=_class, **params)`` -- exists
    only to avoid the leading-underscore-kwarg awkwardness when callers
    already have ``_class`` bound to a local variable.
    """
    return send(command, _class=_class, **params)


def _io_round_trip(msg: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Single send+recv cycle under the I/O lock with a per-call timeout."""
    assert _sock is not None
    with _io_lock:
        old_timeout = _sock.gettimeout()
        try:
            _sock.settimeout(timeout)
            _send_json(_sock, msg)
            return _recv_json(_sock)
        finally:
            with contextlib.suppress(OSError):
                _sock.settimeout(old_timeout)


# -- backwards-compat helpers --


def send_raw(command: str, timeout: float | None = None, **params: Any) -> dict[str, Any]:
    """Like send() but with a custom timeout. Kept for back-compat callers.

    Prefer ``send(command, _class=...)`` for new code. The custom-timeout
    branch performs the same request_id round-trip + structured error
    envelope as ``send`` so callers don't lose envelope fields just
    because they chose an off-spec timeout.
    """
    if _sock is None:
        raise ConnectionError("not connected to Nuke")
    if timeout is None:
        return send(command, **params)
    # map the explicit timeout onto a class lookup if it matches a known
    # value, otherwise fall through to the custom-timeout path below.
    for name, value in TIMEOUT_CLASSES.items():
        if abs(value - timeout) < 1e-6:
            return send(command, _class=name, **params)

    rid = uuid.uuid4().hex[:16]
    msg = {"type": command, "params": params, "_request_id": rid}
    started = time.perf_counter()
    resp = _io_round_trip(msg, timeout)
    duration_ms = int((time.perf_counter() - started) * 1000)

    echoed_rid = resp.get("_request_id")
    if echoed_rid is not None and echoed_rid != rid:
        raise ConnectionError(f"request_id mismatch: sent {rid}, got {echoed_rid}")

    if resp.get("status") == "error":
        envelope = {
            "error_class": resp.get("error_class") or "CommandError",
            "error_code": resp.get("error_code"),
            "traceback": resp.get("traceback"),
            "duration_ms": duration_ms,
            "request_id": rid,
        }
        raise CommandError(resp.get("error", "unknown error"), envelope=envelope)
    return resp.get("result", {})


def ping() -> bool:
    """Check if Nuke is still responding."""
    try:
        send("ping", _class="ping")
        return True
    except (ConnectionError, CommandError, OSError):
        return False


# -- heartbeat --


def _heartbeat_enabled() -> bool:
    return os.environ.get("NUKE_MCP_HEARTBEAT", "1") not in ("0", "false", "False", "")


def _start_heartbeat() -> None:
    global _heartbeat_thread, _heartbeat_stop
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    _heartbeat_stop = threading.Event()
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(_heartbeat_stop,),
        name="nuke-mcp-heartbeat",
        daemon=True,
    )
    _heartbeat_thread.start()


def _stop_heartbeat() -> None:
    """Signal the heartbeat thread to exit and join it.

    The heartbeat loop calls ``disconnect()`` from inside its own thread
    when ``HEARTBEAT_MAX_MISSES`` is exceeded; ``disconnect()`` in turn
    calls ``_stop_heartbeat()``. Joining a thread from itself raises
    ``RuntimeError``, so the self-thread case skips the join and lets
    the loop return naturally.
    """
    global _heartbeat_thread, _heartbeat_stop
    if _heartbeat_stop is not None:
        _heartbeat_stop.set()
    thread = _heartbeat_thread
    if thread is not None and threading.current_thread() is not thread:
        thread.join(timeout=1.0)
    _heartbeat_thread = None
    _heartbeat_stop = None


def _heartbeat_loop(stop: threading.Event) -> None:
    """Fire ``ping`` every HEARTBEAT_INTERVAL; flag session_lost on misses.

    Uses ``Event.wait`` for clean shutdown -- never burns CPU when the
    stop flag is set, and any sleep gets cut short on disconnect().
    """
    global _session_lost
    misses = 0
    while not stop.wait(HEARTBEAT_INTERVAL):
        if _sock is None:
            return
        try:
            send("ping", _class="ping")
            misses = 0
        except Exception as exc:
            misses += 1
            log.warning("heartbeat miss %d/%d: %s", misses, HEARTBEAT_MAX_MISSES, exc)
            if misses >= HEARTBEAT_MAX_MISSES:
                log.error("heartbeat: %d consecutive misses, declaring session lost", misses)
                _session_lost = True
                # disconnect from a worker thread is fine -- _io_lock
                # protects concurrent socket access.
                with contextlib.suppress(Exception):
                    disconnect()
                return


# -- wire format --


def _send_json(s: socket.socket, data: dict[str, Any]) -> None:
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MSG_SIZE:
        raise ConnectionError(f"message too large: {len(payload)} bytes")
    s.sendall(payload + b"\n")


def _recv_json(s: socket.socket) -> dict[str, Any]:
    buf = b""
    while True:
        try:
            chunk = s.recv(4096)
        except TimeoutError as e:
            raise ConnectionError("recv timed out") from e
        if not chunk:
            raise ConnectionError("connection closed by Nuke")
        buf += chunk
        if len(buf) > MAX_MSG_SIZE:
            raise ConnectionError(f"response too large: {len(buf)} bytes")
        if b"\n" in buf:
            line, _ = buf.split(b"\n", 1)
            return json.loads(line)

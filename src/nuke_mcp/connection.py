"""TCP socket client for communicating with the Nuke addon."""

from __future__ import annotations

import contextlib
import json
import logging
import random
import socket
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876
CONNECT_TIMEOUT = 5.0
RECV_TIMEOUT = 30.0
RECV_TIMEOUT_RENDER = 300.0
MAX_RETRIES = 3
MAX_MSG_SIZE = 16 * 1024 * 1024  # 16MB

_sock: socket.socket | None = None
_nuke_version: NukeVersion | None = None


class ConnectionError(Exception):
    pass


class CommandError(Exception):
    pass


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


def connect(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> NukeVersion:
    """Connect to Nuke addon. Returns version info from handshake."""
    global _sock, _nuke_version

    if _sock is not None:
        disconnect()

    delay = 1.0
    last_err = None

    for attempt in range(MAX_RETRIES):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(CONNECT_TIMEOUT)
            s.connect((host, port))
            s.settimeout(RECV_TIMEOUT)

            # read handshake
            handshake = _recv_json(s)
            _nuke_version = NukeVersion.from_handshake(handshake)
            _sock = s
            log.info("connected to %s on %s:%d", _nuke_version, host, port)
            return _nuke_version

        except (OSError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < MAX_RETRIES - 1:
                jitter = random.uniform(0, delay * 0.1)
                log.warning(
                    "connection attempt %d failed: %s, retrying in %.1fs",
                    attempt + 1,
                    e,
                    delay + jitter,
                )
                time.sleep(delay + jitter)
                delay *= 2

    raise ConnectionError(f"failed to connect after {MAX_RETRIES} attempts: {last_err}")


def disconnect() -> None:
    global _sock, _nuke_version
    if _sock is not None:
        with contextlib.suppress(OSError):
            _sock.close()
        _sock = None
        _nuke_version = None
        log.info("disconnected from Nuke")


def is_connected() -> bool:
    return _sock is not None


def get_version() -> NukeVersion | None:
    return _nuke_version


def send(command: str, **params: Any) -> dict[str, Any]:
    """Send a command to Nuke and return the response.

    Raises ConnectionError if not connected or connection drops.
    Raises CommandError if Nuke reports an error.
    """
    if _sock is None:
        raise ConnectionError("not connected to Nuke")

    msg = {"type": command, "params": params}
    _send_json(_sock, msg)
    resp = _recv_json(_sock)

    if resp.get("status") == "error":
        raise CommandError(resp.get("error", "unknown error"))

    return resp.get("result", {})


def send_raw(command: str, timeout: float | None = None, **params: Any) -> dict[str, Any]:
    """Like send() but with a custom timeout. Used for renders."""
    if _sock is None:
        raise ConnectionError("not connected to Nuke")

    old_timeout = _sock.gettimeout()
    try:
        if timeout is not None:
            _sock.settimeout(timeout)
        return send(command, **params)
    finally:
        _sock.settimeout(old_timeout)


def ping() -> bool:
    """Check if Nuke is still responding."""
    try:
        send_raw("ping", timeout=5.0)
        return True
    except (ConnectionError, CommandError, OSError):
        return False


# -- wire format --


def _send_json(s: socket.socket, data: dict) -> None:
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MSG_SIZE:
        raise ConnectionError(f"message too large: {len(payload)} bytes")
    s.sendall(payload + b"\n")


def _recv_json(s: socket.socket) -> dict:
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

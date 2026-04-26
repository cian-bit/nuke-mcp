"""Live-Nuke contract tests. Skipped unless ``NUKE_BIN`` env var is set.

The fixture launches ``nuke -t headless_runner.py`` and waits for the
addon to bind to its TCP port; the tests then drive
``nuke_mcp.connection.connect()`` against the live process. Teardown
SIGTERMs the headless Nuke.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import pytest

if not os.environ.get("NUKE_BIN"):
    pytest.skip("set NUKE_BIN to run live-contract tests", allow_module_level=True)


_HEADLESS = Path(__file__).parent / "headless_runner.py"
_NUKE_PORT = int(os.environ.get("NUKE_PORT", "9876"))
_NUKE_HOST = os.environ.get("NUKE_HOST", "localhost")
_BOOT_TIMEOUT = float(os.environ.get("NUKE_BOOT_TIMEOUT", "60"))


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    """Poll ``host:port`` until accept-able or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def nuke_session() -> Generator[subprocess.Popen, None, None]:
    nuke_bin = os.environ["NUKE_BIN"]
    proc = subprocess.Popen(
        [nuke_bin, "-t", str(_HEADLESS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_for_port(_NUKE_HOST, _NUKE_PORT, _BOOT_TIMEOUT):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            pytest.fail(
                f"headless Nuke did not bind to {_NUKE_HOST}:{_NUKE_PORT} within {_BOOT_TIMEOUT}s"
            )
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_handshake_returns_version(nuke_session: subprocess.Popen) -> None:  # noqa: ARG001
    """Connecting to live Nuke yields a populated NukeVersion."""
    # Defer import: skip-collection guard runs before this, but explicit
    # import-here keeps the static-analysis path clean for skipped runs.
    from nuke_mcp import connection

    version = connection.connect(_NUKE_HOST, _NUKE_PORT)
    try:
        assert version is not None
        assert version.major >= 14, f"unexpected Nuke major: {version.major}"
        assert version.variant in ("Nuke", "NukeX", "NukeStudio")
    finally:
        connection.disconnect()


# Sanity check: stdlib import of sys is referenced so pyflakes does not
# complain even if test bodies stop using it.
_ = sys

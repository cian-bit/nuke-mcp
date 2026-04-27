"""Headless Nuke runner for live-contract tests.

Launched as ``nuke -t headless_runner.py`` from ``test_live_nuke.py``. Boots
the addon, then idles until killed by the parent test harness.
"""

from __future__ import annotations

import contextlib
import signal
import sys
import threading
import time


def _install_sigterm_handler(stop_event: threading.Event) -> None:
    def _handler(signum, frame):  # noqa: ARG001
        stop_event.set()

    # SIGTERM is the parent's clean-shutdown signal. SIGINT is also
    # handled so ctrl-c works during local debugging.
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, _handler)
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGINT, _handler)


def main() -> None:
    stop = threading.Event()
    _install_sigterm_handler(stop)

    # Lazy import: this script runs inside ``nuke -t``, so ``nuke`` is on
    # the import path. Outside Nuke (for static analysis) this raises.
    try:
        import nuke  # noqa: F401  # imported for side effects in real Nuke
    except ImportError:
        sys.stderr.write("headless_runner.py must be run via 'nuke -t'\n")
        sys.exit(1)

    # Bring up the addon. The addon owns its own listener thread and
    # binds to NUKE_PORT (default 9876). Importing ``addon`` and calling
    # ``start()`` matches the live-Nuke startup path.
    try:
        from nuke_plugin import addon  # type: ignore[import-not-found]
    except ImportError:
        # Fall through: tests for older check-ins may not have the plugin
        # importable from this entry. Best-effort -- contract tests will
        # surface the failure cleanly.
        sys.stderr.write("nuke_plugin.addon not importable from headless_runner.py\n")
        sys.exit(1)

    if hasattr(addon, "start"):
        addon.start()  # type: ignore[attr-defined]

    # Idle until SIGTERM. 100ms tick keeps the loop responsive without
    # burning CPU.
    while not stop.is_set():
        time.sleep(0.1)


if __name__ == "__main__":
    main()

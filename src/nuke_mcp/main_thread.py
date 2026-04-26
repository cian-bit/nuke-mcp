"""Main-thread executor abstraction over connection.send().

This module exists for two reasons:

1. **A3 typed handlers.** Phase A3 will migrate the ad-hoc f-string
   ``execute_python`` calls in ``comp.py`` / ``render.py`` /
   ``channels.py`` to typed addon handlers. Routing every tool through
   ``run_on_main`` gives that migration a single seam to widen instead
   of touching 38 call sites.

2. **Mocking.** Tests can patch ``main_thread.run_on_main`` to capture
   the (handler, params, timeout_class) triple without standing up a
   socket server. Cheaper than monkeypatching ``connection.send``.

The helper itself is a thin wrapper -- no retry logic of its own, no
schema validation. ``connection.send`` already handles request_id,
heartbeat, structured envelope, per-class timeout.
"""

from __future__ import annotations

from typing import Any

from nuke_mcp import connection


def run_on_main(
    handler_name: str,
    params: dict[str, Any] | None = None,
    timeout_class: str = "mutate",
) -> dict[str, Any]:
    """Dispatch a typed handler through the addon's main-thread executor.

    Args:
        handler_name: registered handler key on the addon side.
        params: keyword arguments forwarded as the ``params`` dict in
            the wire payload. Defaults to an empty dict.
        timeout_class: one of ``connection.TIMEOUT_CLASSES`` keys. New
            tools should pick the lowest class that fits their
            longest-known cook -- ``read`` / ``mutate`` cover most
            graph operations, ``render`` for Write executes,
            ``copycat`` for ML training.

    Returns:
        The ``result`` dict from the addon's response. Errors raise
        ``connection.CommandError`` with the structured envelope
        attached; let the ``nuke_command`` decorator catch and format.
    """
    return connection.send(handler_name, _class=timeout_class, **(params or {}))

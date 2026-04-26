"""Shared utilities for tool modules.

The ``nuke_command`` decorator is the single funnel for tool returns:
catches ConnectionError / CommandError / generic Exception, formats the
A2 structured error envelope into the result dict, and emits a duration
log line on success.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any

from nuke_mcp import connection
from nuke_mcp.response import apply_response_shape

log = logging.getLogger(__name__)


def nuke_command(operation: str) -> Callable[..., Callable[..., dict[str, Any]]]:
    """Decorator that wraps a tool function with connection error handling.

    The wrapped function should call connection.send() and return its result.
    Errors get caught and returned as structured error dicts instead of
    raising exceptions into the MCP layer. Successful calls log at INFO
    with structured ``extra`` carrying tool name, duration_ms, and (if
    available) request_id.
    """

    def decorator(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except connection.ConnectionError as e:
                duration_ms = int((time.perf_counter() - started) * 1000)
                log.error("%s: connection error: %s", operation, e)
                return {
                    "status": "error",
                    "error": f"not connected to Nuke: {e}",
                    "error_class": "ConnectionError",
                    "duration_ms": duration_ms,
                }
            except connection.CommandError as e:
                envelope = getattr(e, "envelope", {}) or {}
                duration_ms = envelope.get(
                    "duration_ms", int((time.perf_counter() - started) * 1000)
                )
                payload: dict[str, Any] = {
                    "status": "error",
                    "error": str(e),
                    "error_class": envelope.get("error_class", "CommandError"),
                    "duration_ms": duration_ms,
                }
                if envelope.get("request_id"):
                    payload["request_id"] = envelope["request_id"]
                if envelope.get("traceback"):
                    payload["traceback"] = envelope["traceback"]
                if envelope.get("error_code"):
                    payload["error_code"] = envelope["error_code"]
                return payload
            except Exception as e:
                duration_ms = int((time.perf_counter() - started) * 1000)
                log.error("%s: unexpected error: %s", operation, e)
                return {
                    "status": "error",
                    "error": str(e),
                    "error_class": type(e).__name__,
                    "duration_ms": duration_ms,
                }

            duration_ms = int((time.perf_counter() - started) * 1000)
            log.info(
                "%s ok in %dms",
                operation,
                duration_ms,
                extra={"tool": operation, "duration_ms": duration_ms},
            )

            # B1 response shape: estimate -> truncate -> stamp _meta.
            # Non-dict returns (rare -- a few tools return primitives) skip
            # the wrap. apply_response_shape merges into existing _meta so
            # any duration_ms / request_id another layer added survives.
            if isinstance(result, dict):
                result = apply_response_shape(result, operation)
            return result

        return wrapper

    return decorator

"""Shared utilities for tool modules."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from nuke_mcp import connection

log = logging.getLogger(__name__)


def nuke_command(operation: str):
    """Decorator that wraps a tool function with connection error handling.

    The wrapped function should call connection.send() and return its result.
    Errors get caught and returned as structured error dicts instead of
    raising exceptions into the MCP layer.
    """

    def decorator(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return func(*args, **kwargs)
            except connection.ConnectionError as e:
                log.error("%s: connection error: %s", operation, e)
                return {"status": "error", "error": f"not connected to Nuke: {e}"}
            except connection.CommandError as e:
                return {"status": "error", "error": str(e)}
            except Exception as e:
                log.error("%s: unexpected error: %s", operation, e)
                return {"status": "error", "error": str(e)}

        return wrapper

    return decorator

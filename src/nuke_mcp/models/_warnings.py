"""Small logging helpers for best-effort model validation."""

from __future__ import annotations

import logging

_seen: set[str] = set()


def warn_once(log: logging.Logger, key: str, message: str, *args: object) -> None:
    """Emit one warning per validation path.

    B5 deliberately keeps malformed addon payloads flowing, but silent
    fallback makes production schema drift invisible. This helper keeps
    logs useful without spamming every node in a large comp.
    """
    if key in _seen:
        return
    _seen.add(key)
    log.warning(message, *args)


def reset_for_tests() -> None:
    _seen.clear()

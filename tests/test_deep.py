"""Placeholder tests for deep.py.

The module is currently empty -- C1 will fill it (create_deep_recolor,
create_deep_merge, create_deep_holdout, create_deep_transform,
deep_to_image). The xfail enumerates expected public symbols so future
regressions on shape are caught early.
"""

from __future__ import annotations

import pytest

from nuke_mcp.tools import deep


def test_module_empty() -> None:
    """Module is importable. Shape will be filled by C1."""
    assert deep is not None


_EXPECTED_SYMBOLS = (
    "create_deep_recolor",
    "create_deep_merge",
    "create_deep_holdout",
    "create_deep_transform",
    "deep_to_image",
)


@pytest.mark.xfail(reason="C1 will implement deep.py", strict=True)
@pytest.mark.parametrize("symbol", _EXPECTED_SYMBOLS)
def test_expected_symbols_present(symbol: str) -> None:
    assert hasattr(deep, symbol), f"deep.{symbol} missing"

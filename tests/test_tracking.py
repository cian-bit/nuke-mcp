"""Placeholder tests for tracking.py.

The module is currently empty -- C1 will fill it (setup_camera_tracker,
setup_planar_tracker, setup_tracker4, bake_tracker_to_corner_pin,
solve_3d_camera, bake_camera_to_card). The xfail enumerates expected
public symbols so future regressions on shape are caught early.
"""

from __future__ import annotations

import pytest

from nuke_mcp.tools import tracking


def test_module_empty() -> None:
    """Module is importable. Shape will be filled by C1."""
    assert tracking is not None


_EXPECTED_SYMBOLS = (
    "setup_camera_tracker",
    "setup_planar_tracker",
    "setup_tracker4",
    "bake_tracker_to_corner_pin",
    "solve_3d_camera",
    "bake_camera_to_card",
)


@pytest.mark.xfail(reason="C1 will implement tracking.py", strict=True)
@pytest.mark.parametrize("symbol", _EXPECTED_SYMBOLS)
def test_expected_symbols_present(symbol: str) -> None:
    assert hasattr(tracking, symbol), f"tracking.{symbol} missing"

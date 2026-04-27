"""Placeholder tests for tracking.py.

The module is currently empty -- C1 will fill it (setup_camera_tracker,
setup_planar_tracker, setup_tracker4, bake_tracker_to_corner_pin,
solve_3d_camera, bake_camera_to_card). The xfails enumerate expected
public symbols AND expected function signatures so future regressions
on shape are caught early. When C1 lands the implementation the xfails
flip to xpassed and the strict marker forces an explicit unmark.
"""

from __future__ import annotations

import inspect

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


# Pinned signatures C1 must satisfy. Each entry is
# (name, leading_positional_param_names). Only positional-or-keyword
# parameters in declaration order are checked; trailing optional params
# may be added later without breaking the pin.
_EXPECTED_SIGNATURES: dict[str, tuple[str, ...]] = {
    "setup_camera_tracker": ("input_node",),
    "setup_planar_tracker": ("input_node",),
    "setup_tracker4": ("input_node",),
    "bake_tracker_to_corner_pin": ("tracker_node",),
    "solve_3d_camera": ("camera_tracker_node",),
    "bake_camera_to_card": ("camera_node",),
}


@pytest.mark.xfail(reason="C1 will implement tracking.py", strict=True)
@pytest.mark.parametrize("symbol", _EXPECTED_SYMBOLS)
def test_expected_symbols_present(symbol: str) -> None:
    assert hasattr(tracking, symbol), f"tracking.{symbol} missing"


@pytest.mark.xfail(reason="C1 will implement tracking.py with these signatures", strict=True)
@pytest.mark.parametrize(("name", "expected_params"), list(_EXPECTED_SIGNATURES.items()))
def test_expected_signatures(name: str, expected_params: tuple[str, ...]) -> None:
    fn = getattr(tracking, name, None)
    assert fn is not None, f"tracking.{name} missing"
    sig = inspect.signature(fn)
    actual = tuple(
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    )
    assert (
        actual[: len(expected_params)] == expected_params
    ), f"tracking.{name} signature drift: expected {expected_params}, got {actual}"

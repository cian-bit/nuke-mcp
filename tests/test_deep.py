"""Placeholder tests for deep.py.

The module is currently empty -- C1 will fill it (create_deep_recolor,
create_deep_merge, create_deep_holdout, create_deep_transform,
deep_to_image). The xfails enumerate expected public symbols AND
expected function signatures so future regressions on shape are
caught early. When C1 lands the implementation the xfails flip to
xpassed and the strict marker forces an explicit unmark.
"""

from __future__ import annotations

import inspect

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


# Pinned signatures C1 must satisfy. Each entry is
# (name, ordered_param_names). Only positional-or-keyword parameters
# count; *args / **kwargs are ignored. The entries are deliberate
# minimums -- C1 may add KW-only optional params later, but the listed
# names + order must match.
_EXPECTED_SIGNATURES: dict[str, tuple[str, ...]] = {
    "create_deep_recolor": ("deep_node", "color_node"),
    "create_deep_merge": ("a_node", "b_node"),
    "create_deep_holdout": ("subject_node", "holdout_node"),
    "create_deep_transform": ("input_node",),
    "deep_to_image": ("input_node",),
}


@pytest.mark.xfail(reason="C1 will implement deep.py", strict=True)
@pytest.mark.parametrize("symbol", _EXPECTED_SYMBOLS)
def test_expected_symbols_present(symbol: str) -> None:
    assert hasattr(deep, symbol), f"deep.{symbol} missing"


@pytest.mark.xfail(reason="C1 will implement deep.py with these signatures", strict=True)
@pytest.mark.parametrize(("name", "expected_params"), list(_EXPECTED_SIGNATURES.items()))
def test_expected_signatures(name: str, expected_params: tuple[str, ...]) -> None:
    fn = getattr(deep, name, None)
    assert fn is not None, f"deep.{name} missing"
    sig = inspect.signature(fn)
    actual = tuple(
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    )
    # Allow extra trailing optional params -- only assert the leading
    # subset matches the pin.
    assert (
        actual[: len(expected_params)] == expected_params
    ), f"deep.{name} signature drift: expected {expected_params}, got {actual}"

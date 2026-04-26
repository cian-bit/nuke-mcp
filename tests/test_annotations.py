"""B6 annotation snapshot test.

Builds the FastMCP server, walks every registered tool, and asserts:
  1. Every tool carries at least one of the four MCP hints (readOnlyHint,
     destructiveHint, idempotentHint, openWorldHint) explicitly set
     (non-None). False is acceptable -- it's an explicit "I am benign"
     stamp on tools that create new state but don't destroy.
  2. The full ``(name, hints)`` table matches the snapshot fixture below.

Update ``EXPECTED_HINTS`` deliberately when the audit changes.
"""

from __future__ import annotations

import asyncio

import pytest

from nuke_mcp.annotations import (
    DESTRUCTIVE,
    DESTRUCTIVE_OPEN,
    IDEMPOTENT,
    OPEN_WORLD,
    READ_AND_IDEMPOTENT,
    READ_ONLY,
)
from nuke_mcp.server import build_server

_HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


# ---------------------------------------------------------------------------
# Snapshot fixture: every tool's expected hint dict.
# ---------------------------------------------------------------------------
# Tools that create brand-new nodes (create_*, shuffle_channels, setup_precomp,
# create_roto) carry only ``destructiveHint=False`` -- they don't fit any of
# the four named presets.

_BENIGN_NEW = {"destructiveHint": False}

EXPECTED_HINTS: dict[str, dict[str, bool]] = {
    # read.py
    "read_comp": READ_ONLY,
    "read_node_detail": READ_ONLY,
    "read_selected": READ_ONLY,
    "snapshot_comp": READ_ONLY,
    "diff_comp": READ_ONLY,
    # graph.py
    "create_node": _BENIGN_NEW,
    "delete_node": DESTRUCTIVE,
    "find_nodes": READ_ONLY,
    "list_nodes": READ_ONLY,
    "connect_nodes": IDEMPOTENT,
    "auto_layout": IDEMPOTENT,
    "modify_node": IDEMPOTENT,
    "create_nodes": _BENIGN_NEW,
    "disconnect_node_input": IDEMPOTENT,
    "set_node_position": IDEMPOTENT,
    # knobs.py
    "get_knob": READ_ONLY,
    "set_knob": IDEMPOTENT,
    "set_knobs": IDEMPOTENT,
    # script.py
    "get_script_info": READ_ONLY,
    "save_script": OPEN_WORLD,
    "load_script": DESTRUCTIVE_OPEN,
    "set_frame_range": IDEMPOTENT,
    # render.py
    "setup_write": IDEMPOTENT,
    "render_frames": DESTRUCTIVE_OPEN,
    "setup_precomp": _BENIGN_NEW,
    "list_precomps": READ_ONLY,
    # channels.py
    "list_channels": READ_ONLY,
    "shuffle_channels": _BENIGN_NEW,
    "setup_aov_merge": IDEMPOTENT,
    # viewer.py
    "view_node": _BENIGN_NEW,
    "set_viewer_lut": IDEMPOTENT,
    # code.py
    "execute_python": DESTRUCTIVE_OPEN,
    # comp.py
    "setup_keying": IDEMPOTENT,
    "setup_color_correction": IDEMPOTENT,
    "setup_merge": IDEMPOTENT,
    "setup_transform": IDEMPOTENT,
    "setup_denoise": IDEMPOTENT,
    # expressions.py
    "set_expression": IDEMPOTENT,
    "clear_expression": IDEMPOTENT,
    "set_keyframe": IDEMPOTENT,
    "list_keyframes": READ_ONLY,
    # roto.py
    "create_roto": _BENIGN_NEW,
    "list_roto_shapes": READ_ONLY,
}


def _hint_dict(annotations) -> dict[str, bool]:
    """Pull the four hint keys out of a FastMCP ToolAnnotations.

    Returns only keys whose value is non-None. ``False`` is meaningful and
    is preserved.
    """
    if annotations is None:
        return {}
    out: dict[str, bool] = {}
    for k in _HINT_KEYS:
        v = getattr(annotations, k, None)
        if v is not None:
            out[k] = v
    return out


@pytest.fixture(scope="module")
def all_tools() -> list:
    mcp = build_server(mock=True)
    tools = asyncio.run(mcp.list_tools())
    return tools


def test_every_tool_has_at_least_one_hint(all_tools: list) -> None:
    """No tool may register without a hint on its annotations object.

    ``False`` qualifies -- explicit benign-new stamp is still informative.
    """
    missing = []
    for tool in all_tools:
        hints = _hint_dict(tool.annotations)
        if not hints:
            missing.append(tool.name)
    assert not missing, f"tools missing all four hints: {missing}"


def test_every_tool_has_at_least_one_true_or_explicit_false(all_tools: list) -> None:
    """Defensive: every tool has at least one hint key set, of either polarity."""
    for tool in all_tools:
        hints = _hint_dict(tool.annotations)
        assert hints, f"{tool.name} has no hint keys"


def test_no_unexpected_tools(all_tools: list) -> None:
    """If a new tool is added, the fixture must be updated alongside."""
    registered = {t.name for t in all_tools}
    expected = set(EXPECTED_HINTS.keys())
    extra = registered - expected
    missing = expected - registered
    assert not extra, f"new tools without snapshot entries: {sorted(extra)}"
    assert not missing, f"snapshot lists tools that no longer exist: {sorted(missing)}"


def test_snapshot_hints(all_tools: list) -> None:
    """Per-tool hint snapshot. Update EXPECTED_HINTS deliberately on audit changes."""
    diffs: list[str] = []
    for tool in all_tools:
        actual = _hint_dict(tool.annotations)
        expected = EXPECTED_HINTS.get(tool.name, {})
        if actual != expected:
            diffs.append(f"{tool.name}: expected {expected}, got {actual}")
    assert not diffs, "annotation drift:\n  " + "\n  ".join(diffs)


def test_read_only_preset_shape() -> None:
    assert READ_ONLY == {"readOnlyHint": True}


def test_idempotent_preset_shape() -> None:
    assert IDEMPOTENT == {"idempotentHint": True, "destructiveHint": False}


def test_destructive_preset_shape() -> None:
    assert DESTRUCTIVE == {"destructiveHint": True}


def test_open_world_preset_shape() -> None:
    assert OPEN_WORLD == {"openWorldHint": True}


def test_combined_presets_merge() -> None:
    assert DESTRUCTIVE_OPEN == {"destructiveHint": True, "openWorldHint": True}
    assert READ_AND_IDEMPOTENT == {
        "readOnlyHint": True,
        "idempotentHint": True,
        "destructiveHint": False,
    }

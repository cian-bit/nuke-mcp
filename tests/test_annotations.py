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
    BENIGN_NEW,
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
# Tools that create brand-new nodes (create_*, shuffle_channels, setup_*,
# create_roto) carry only ``destructiveHint=False`` -- they don't fit any of
# the four named presets. Calling them twice creates duplicate nodes
# (Foo1, Foo2, ...), so they are NOT idempotent in the MCP sense even
# though the addon-side validation is deterministic.

EXPECTED_HINTS: dict[str, dict[str, bool]] = {
    # read.py
    "read_comp": READ_ONLY,
    "read_node_detail": READ_ONLY,
    "read_selected": READ_ONLY,
    "snapshot_comp": READ_ONLY,
    "diff_comp": READ_ONLY,
    # graph.py
    "create_node": BENIGN_NEW,
    "delete_node": DESTRUCTIVE,
    "find_nodes": READ_ONLY,
    "list_nodes": READ_ONLY,
    "connect_nodes": IDEMPOTENT,
    "auto_layout": IDEMPOTENT,
    "modify_node": IDEMPOTENT,
    "create_nodes": BENIGN_NEW,
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
    "setup_write": BENIGN_NEW,
    "render_frames": DESTRUCTIVE_OPEN,
    "setup_precomp": BENIGN_NEW,
    "list_precomps": READ_ONLY,
    # channels.py
    "list_channels": READ_ONLY,
    "shuffle_channels": BENIGN_NEW,
    "setup_aov_merge": BENIGN_NEW,
    # viewer.py
    "view_node": BENIGN_NEW,
    "set_viewer_lut": IDEMPOTENT,
    # code.py
    "execute_python": DESTRUCTIVE_OPEN,
    # comp.py
    "setup_keying": BENIGN_NEW,
    "setup_color_correction": BENIGN_NEW,
    "setup_merge": BENIGN_NEW,
    "setup_transform": BENIGN_NEW,
    "setup_denoise": BENIGN_NEW,
    # expressions.py
    "set_expression": IDEMPOTENT,
    "clear_expression": IDEMPOTENT,
    "set_keyframe": IDEMPOTENT,
    "list_keyframes": READ_ONLY,
    # roto.py
    "create_roto": BENIGN_NEW,
    "list_roto_shapes": READ_ONLY,
    # digest.py (B7)
    "scene_digest": READ_ONLY,
    "scene_delta": READ_ONLY,
    # tracking.py (C1)
    "setup_camera_tracker": BENIGN_NEW,
    "setup_planar_tracker": BENIGN_NEW,
    "setup_tracker4": BENIGN_NEW,
    "bake_tracker_to_corner_pin": BENIGN_NEW,
    "solve_3d_camera": BENIGN_NEW,
    "bake_camera_to_card": BENIGN_NEW,
    # deep.py (C1)
    "create_deep_recolor": BENIGN_NEW,
    "create_deep_merge": BENIGN_NEW,
    "create_deep_holdout": BENIGN_NEW,
    "create_deep_transform": BENIGN_NEW,
    "deep_to_image": BENIGN_NEW,
    # tasks.py (B2 -- MCP 2025-11-25 Tasks primitive)
    "tasks_list": READ_ONLY,
    "tasks_get": READ_ONLY,
    "tasks_cancel": DESTRUCTIVE,
    "tasks_resume": READ_ONLY,
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


def test_benign_new_preset_shape() -> None:
    """BENIGN_NEW carries only ``destructiveHint=False`` -- no idempotent
    claim, since duplicate calls produce duplicate nodes.
    """
    assert BENIGN_NEW == {"destructiveHint": False}
    assert "idempotentHint" not in BENIGN_NEW


def test_setup_keying_no_longer_claims_idempotent(all_tools: list) -> None:
    """GPT-5.5 finding #8 regression check: setup_keying creates new
    nodes per call, so it must not advertise idempotentHint=True.
    """
    by_name = {t.name: t for t in all_tools}
    keying = by_name["setup_keying"]
    hints = _hint_dict(keying.annotations)
    assert hints.get("idempotentHint") is not True
    assert hints == BENIGN_NEW


def test_all_setup_tools_advertise_benign_new(all_tools: list) -> None:
    """Every setup_* tool that creates new nodes must use BENIGN_NEW."""
    by_name = {t.name: t for t in all_tools}
    new_node_setups = (
        "setup_keying",
        "setup_color_correction",
        "setup_merge",
        "setup_transform",
        "setup_denoise",
        "setup_write",
        "setup_aov_merge",
    )
    for name in new_node_setups:
        hints = _hint_dict(by_name[name].annotations)
        assert hints == BENIGN_NEW, f"{name} expected BENIGN_NEW, got {hints}"

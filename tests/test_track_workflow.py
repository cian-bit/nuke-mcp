"""Tests for track_workflow.py (Phase C5).

The C5 ``setup_spaceship_track_patch`` workflow macro composes the C1
atomic tracking primitives into a Group-wrapped multi-node graph.
These tests assert the graph shape (member node classes) and the
underlying C1 calls the macro dispatched -- they do NOT re-test C1
itself; that's covered in ``test_tracking.py``.

Mock-side dispatch: ``conftest.MockNukeServer._setup_spaceship_track_patch``
mirrors the addon-side handler, calls the C1 sub-handlers internally,
and records every leg in ``server.typed_calls`` so we can assert which
primitives the macro pulled.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from nuke_mcp.tools import track_workflow


def test_module_importable() -> None:
    """Module is importable and registers tools."""
    assert track_workflow is not None
    assert hasattr(track_workflow, "register")


# ---------------------------------------------------------------------------
# Stub MCP infrastructure (mirrors test_tracking.py)
# ---------------------------------------------------------------------------


class _StubMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def decorator(func):
            self.registered[func.__name__] = func
            return func

        return decorator


class _StubCtx:
    def __init__(self) -> None:
        self.mcp = _StubMCP()
        self.version = None
        self.mock = True


@pytest.fixture
def patch_tools(mock_script):
    """Register C5 macros against a connected mock server.

    Seeds a plate node so the macro has a valid input to point at,
    plus an extra ``custom_paint`` node tests can pass as
    ``patch_source=`` to exercise the BYO-source branch.
    """
    server, script = mock_script
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    server.nodes["custom_paint"] = {"type": "RotoPaint", "knobs": {}, "x": 0, "y": 0}
    server.connections["custom_paint"] = []
    ctx = _StubCtx()
    track_workflow.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# Signature pin
# ---------------------------------------------------------------------------


def test_macro_registered_with_pinned_signature(patch_tools) -> None:
    _server, _script, tools = patch_tools
    fn = tools.get("setup_spaceship_track_patch")
    assert fn is not None, "setup_spaceship_track_patch not registered"
    sig = inspect.signature(fn)
    leading = tuple(
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    )
    # Pin the first three (plate, ref_frame, surface_type); trailing
    # optional params may grow without breaking the pin.
    assert leading[:3] == ("plate", "ref_frame", "surface_type")


# ---------------------------------------------------------------------------
# surface_type="planar" branch
# ---------------------------------------------------------------------------


def test_planar_branch_produces_expected_chain(patch_tools) -> None:
    """Planar branch: PlanarTracker + RotoPaint + CornerPin chain in Group."""
    server, _script, tools = patch_tools
    result = tools["setup_spaceship_track_patch"]("plate", ref_frame=1010, surface_type="planar")
    assert result.get("status") != "error", result
    assert result["type"] == "Group"
    assert result["surface_type"] == "planar"
    member_classes = {server.nodes[m]["type"] for m in result["members"]}
    # PlanarTracker is the tracker, RotoPaint is the default patch
    # source, CornerPin2D appears twice (forward + restore) -- the
    # set collapses to one entry.
    assert "PlanarTracker" in member_classes
    assert "RotoPaint" in member_classes
    assert "CornerPin2D" in member_classes
    # 3D-branch nodes must NOT appear in the planar graph.
    assert "CameraTracker" not in member_classes
    assert "Card3D" not in member_classes
    assert "Project3D" not in member_classes
    assert "ScanlineRender" not in member_classes


# ---------------------------------------------------------------------------
# surface_type="3d" branch
# ---------------------------------------------------------------------------


def test_threed_branch_produces_expected_chain(patch_tools) -> None:
    """3d branch: CameraTracker + Card3D + Project3D + ScanlineRender + Merge in Group."""
    server, _script, tools = patch_tools
    result = tools["setup_spaceship_track_patch"]("plate", ref_frame=1010, surface_type="3d")
    assert result.get("status") != "error", result
    assert result["type"] == "Group"
    assert result["surface_type"] == "3d"
    member_classes = {server.nodes[m]["type"] for m in result["members"]}
    assert "CameraTracker" in member_classes
    assert "Card3D" in member_classes
    assert "Project3D" in member_classes
    assert "ScanlineRender" in member_classes
    assert "Merge2" in member_classes
    # Planar-branch markers absent.
    assert "PlanarTracker" not in member_classes
    assert "CornerPin2D" not in member_classes


# ---------------------------------------------------------------------------
# patch_source plumbing
# ---------------------------------------------------------------------------


def test_default_patch_source_uses_rotopaint(patch_tools) -> None:
    """When ``patch_source`` is None, a default RotoPaint is created."""
    server, _script, tools = patch_tools
    result = tools["setup_spaceship_track_patch"]("plate", ref_frame=1010, surface_type="planar")
    member_classes = [server.nodes[m]["type"] for m in result["members"]]
    # default planar patch source should be a RotoPaint inside the group
    assert "RotoPaint" in member_classes
    # custom_paint must NOT have been used as a member -- it's outside
    # the macro's invocation scope.
    assert "custom_paint" not in result["members"]


def test_explicit_patch_source_is_wired(patch_tools) -> None:
    """When ``patch_source`` is supplied, it appears in the member list."""
    server, _script, tools = patch_tools
    result = tools["setup_spaceship_track_patch"](
        "plate",
        ref_frame=1010,
        surface_type="planar",
        patch_source="custom_paint",
    )
    assert result.get("status") != "error", result
    assert "custom_paint" in result["members"]
    # No default RotoPaint should have been auto-created since the
    # caller supplied their own. Sanity-check that the only RotoPaint
    # in the member graph is the seeded ``custom_paint`` one.
    rotopaint_members = [m for m in result["members"] if server.nodes[m]["type"] == "RotoPaint"]
    assert rotopaint_members == ["custom_paint"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_on_explicit_name(patch_tools) -> None:
    """A re-call with the same ``name=`` returns the existing Group."""
    server, _script, tools = patch_tools
    first = tools["setup_spaceship_track_patch"](
        "plate", ref_frame=1010, surface_type="planar", name="MyPatch"
    )
    second = tools["setup_spaceship_track_patch"](
        "plate", ref_frame=1010, surface_type="planar", name="MyPatch"
    )
    assert first["name"] == "MyPatch"
    assert second["name"] == "MyPatch"
    groups = [n for n, d in server.nodes.items() if d["type"] == "Group"]
    assert groups == ["MyPatch"], f"expected exactly one Group named MyPatch, got {groups}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_bad_surface_type_raises_clean_error(patch_tools) -> None:
    """An unknown surface_type returns a structured error dict."""
    _server, _script, tools = patch_tools
    result = tools["setup_spaceship_track_patch"](
        "plate", ref_frame=1010, surface_type="cylindrical"
    )
    assert result.get("status") == "error"
    assert "surface_type" in result["error"].lower()


# ---------------------------------------------------------------------------
# Group naming -- shot-tag derivation
# ---------------------------------------------------------------------------


def test_group_name_uses_ss_shot_env(patch_tools, monkeypatch) -> None:
    """``$SS_SHOT`` env var drives the Group name when set."""
    server, _script, tools = patch_tools
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    result = tools["setup_spaceship_track_patch"]("plate", ref_frame=1010, surface_type="planar")
    assert result.get("status") != "error", result
    assert result["name"] == "SpaceshipPatch_ss_0170"
    assert result["name"] in server.nodes


def test_group_name_falls_back_to_script_stem(patch_tools, monkeypatch) -> None:
    """With no ``$SS_SHOT``, the Group name comes from the script stem."""
    server, _script, tools = patch_tools
    monkeypatch.delenv("SS_SHOT", raising=False)
    # ``mock_script`` defaults to script_info["script"] = "/tmp/test.nk"
    # -> stem ``test``.
    server.script_info["script"] = "/tmp/test.nk"
    result = tools["setup_spaceship_track_patch"]("plate", ref_frame=1010, surface_type="3d")
    assert result.get("status") != "error", result
    assert result["name"] == "SpaceshipPatch_test"

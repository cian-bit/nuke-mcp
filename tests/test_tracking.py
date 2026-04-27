"""Tests for tracking.py.

C1 filled the module: setup_camera_tracker, setup_planar_tracker,
setup_tracker4, bake_tracker_to_corner_pin, solve_3d_camera,
bake_camera_to_card. The signature pins below are no longer xfail --
they assert the public surface as shipped. Keep them; they catch
regressions on parameter order or rename.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from nuke_mcp.tools import tracking


def test_module_importable() -> None:
    """Module is importable and registers tools."""
    assert tracking is not None
    assert hasattr(tracking, "register")


_EXPECTED_SYMBOLS = ("register",)


# Pinned signatures C1 must satisfy. Each entry is
# (name, leading_positional_param_names). Only positional-or-keyword
# parameters in declaration order are checked; trailing optional params
# may be added later without breaking the pin.
#
# These are the inner tool callables registered onto MCP. They aren't
# module-level attributes any more (registration happens inside
# ``register(ctx)``), so the signature checks pull them from a stub
# context fixture below.
_EXPECTED_SIGNATURES: dict[str, tuple[str, ...]] = {
    "setup_camera_tracker": ("input_node",),
    "setup_planar_tracker": ("input_node",),
    "setup_tracker4": ("input_node",),
    "bake_tracker_to_corner_pin": ("tracker_node",),
    "solve_3d_camera": ("camera_tracker_node",),
    "bake_camera_to_card": ("camera_node",),
}


# ---------------------------------------------------------------------------
# Stub MCP infrastructure (mirrors test_comp.py)
# ---------------------------------------------------------------------------


class _StubMCP:
    """Captures registered tool callables."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, *args, **kwargs):
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
def tracking_tools(mock_script):
    """Register tracking tools against a connected mock server.

    Seeds a plate, a Roto plane, a tracker, and a solved CameraTracker
    so every primitive has a valid input to point at.
    """
    server, script = mock_script
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    server.nodes["plane_roto"] = {"type": "Roto", "knobs": {}, "x": 0, "y": 0}
    server.connections["plane_roto"] = []
    server.nodes["mask_node"] = {"type": "Roto", "knobs": {}, "x": 0, "y": 0}
    server.connections["mask_node"] = []
    server.nodes["existing_tracker"] = {"type": "Tracker4", "knobs": {}, "x": 0, "y": 0}
    server.connections["existing_tracker"] = ["plate"]
    server.nodes["existing_camtrack"] = {"type": "CameraTracker", "knobs": {}, "x": 0, "y": 0}
    server.connections["existing_camtrack"] = ["plate"]
    ctx = _StubCtx()
    tracking.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# Signature pin checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "expected_params"), list(_EXPECTED_SIGNATURES.items()))
def test_registered_tool_signatures(
    tracking_tools, name: str, expected_params: tuple[str, ...]
) -> None:
    """Each registered tool advertises the pinned leading params."""
    _server, _script, tools = tracking_tools
    fn = tools.get(name)
    assert fn is not None, f"tracking tool {name!r} not registered"
    sig = inspect.signature(fn)
    actual = tuple(
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    )
    assert (
        actual[: len(expected_params)] == expected_params
    ), f"tracking.{name} signature drift: expected {expected_params}, got {actual}"


# ---------------------------------------------------------------------------
# setup_camera_tracker
# ---------------------------------------------------------------------------


def test_setup_camera_tracker_happy_path(tracking_tools):
    server, _script, tools = tracking_tools
    result = tools["setup_camera_tracker"]("plate")
    assert isinstance(result, dict)
    assert result.get("status") != "error"
    assert result["type"] == "CameraTracker"
    assert "plate" in result["inputs"]
    cmd, params = server.typed_calls[0]
    assert cmd == "setup_camera_tracker"
    assert params["input_node"] == "plate"
    assert params["features"] == 300
    assert params["solve_method"] == "Match-Move"


def test_setup_camera_tracker_with_mask(tracking_tools):
    server, _script, tools = tracking_tools
    result = tools["setup_camera_tracker"]("plate", mask="mask_node")
    assert "plate" in result["inputs"]
    assert "mask_node" in result["inputs"]
    _cmd, params = server.typed_calls[0]
    assert params["mask"] == "mask_node"


def test_setup_camera_tracker_idempotent(tracking_tools):
    """Re-calling with the same name returns the existing NodeRef."""
    server, _script, tools = tracking_tools
    first = tools["setup_camera_tracker"]("plate", name="myCamTrack")
    second = tools["setup_camera_tracker"]("plate", name="myCamTrack")
    assert first["name"] == "myCamTrack"
    assert second["name"] == "myCamTrack"
    cam_trackers = [n for n, d in server.nodes.items() if d["type"] == "CameraTracker"]
    # one seeded existing + one we just made = 2; idempotent re-call did
    # not bump that count.
    assert len(cam_trackers) == 2


def test_setup_camera_tracker_unknown_node(tracking_tools):
    _server, _script, tools = tracking_tools
    result = tools["setup_camera_tracker"]("does_not_exist")
    assert result.get("status") == "error"


def test_setup_camera_tracker_rejects_bad_solve_method(tracking_tools):
    _server, _script, tools = tracking_tools
    result = tools["setup_camera_tracker"]("plate", solve_method="DROP TABLE; --")
    assert result.get("status") == "error"
    assert "invalid" in result["error"].lower()


# ---------------------------------------------------------------------------
# setup_planar_tracker
# ---------------------------------------------------------------------------


def test_setup_planar_tracker_happy_path(tracking_tools):
    server, _script, tools = tracking_tools
    result = tools["setup_planar_tracker"]("plate", "plane_roto")
    assert result["type"] == "PlanarTrackerNode"
    assert result["inputs"][:2] == ["plate", "plane_roto"]
    _cmd, params = server.typed_calls[0]
    assert params["input_node"] == "plate"
    assert params["plane_roto"] == "plane_roto"
    assert params["ref_frame"] == 1


def test_setup_planar_tracker_idempotent(tracking_tools):
    server, _script, tools = tracking_tools
    first = tools["setup_planar_tracker"]("plate", "plane_roto", name="myPlanar")
    second = tools["setup_planar_tracker"]("plate", "plane_roto", name="myPlanar")
    assert first["name"] == second["name"] == "myPlanar"
    planar_count = [n for n, d in server.nodes.items() if d["type"] == "PlanarTrackerNode"]
    assert len(planar_count) == 1


def test_setup_planar_tracker_missing_input(tracking_tools):
    _server, _script, tools = tracking_tools
    result = tools["setup_planar_tracker"]("nope", "plane_roto")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# setup_tracker4
# ---------------------------------------------------------------------------


def test_setup_tracker4_happy_path(tracking_tools):
    server, _script, tools = tracking_tools
    result = tools["setup_tracker4"]("plate", num_tracks=6)
    assert result["type"] == "Tracker4"
    assert result["inputs"] == ["plate"]
    _cmd, params = server.typed_calls[0]
    assert params["num_tracks"] == 6


def test_setup_tracker4_idempotent(tracking_tools):
    server, _script, tools = tracking_tools
    first = tools["setup_tracker4"]("plate", name="myTracker")
    second = tools["setup_tracker4"]("plate", name="myTracker")
    assert first["name"] == second["name"] == "myTracker"


def test_setup_tracker4_rejects_bad_num_tracks(tracking_tools):
    _server, _script, tools = tracking_tools
    result = tools["setup_tracker4"]("plate", num_tracks=0)
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# bake_tracker_to_corner_pin
# ---------------------------------------------------------------------------


def test_bake_tracker_to_corner_pin_happy_path(tracking_tools):
    server, _script, tools = tracking_tools
    result = tools["bake_tracker_to_corner_pin"]("existing_tracker", ref_frame=1010)
    assert result["type"] == "CornerPin2D"
    assert result["inputs"] == ["existing_tracker"]
    _cmd, params = server.typed_calls[0]
    assert params["tracker_node"] == "existing_tracker"
    assert params["ref_frame"] == 1010


def test_bake_tracker_to_corner_pin_idempotent(tracking_tools):
    server, _script, tools = tracking_tools
    first = tools["bake_tracker_to_corner_pin"]("existing_tracker", name="myPin")
    second = tools["bake_tracker_to_corner_pin"]("existing_tracker", name="myPin")
    assert first["name"] == second["name"] == "myPin"
    pins = [n for n, d in server.nodes.items() if d["type"] == "CornerPin2D"]
    assert len(pins) == 1


def test_bake_tracker_to_corner_pin_unknown_tracker(tracking_tools):
    _server, _script, tools = tracking_tools
    result = tools["bake_tracker_to_corner_pin"]("not_real")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# solve_3d_camera
# ---------------------------------------------------------------------------


def test_solve_3d_camera_happy_path(tracking_tools):
    server, _script, tools = tracking_tools
    result = tools["solve_3d_camera"]("existing_camtrack")
    assert result["type"] == "CameraTracker"
    assert result["name"] == "existing_camtrack"
    _cmd, params = server.typed_calls[0]
    assert params["camera_tracker_node"] == "existing_camtrack"


def test_solve_3d_camera_rejects_non_camera_tracker(tracking_tools):
    _server, _script, tools = tracking_tools
    result = tools["solve_3d_camera"]("plate")
    assert result.get("status") == "error"


def test_solve_3d_camera_unknown_node(tracking_tools):
    _server, _script, tools = tracking_tools
    result = tools["solve_3d_camera"]("nope")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# bake_camera_to_card
# ---------------------------------------------------------------------------


def test_bake_camera_to_card_happy_path(tracking_tools):
    server, _script, tools = tracking_tools
    result = tools["bake_camera_to_card"]("existing_camtrack", frame=1024)
    assert result["type"] == "Card3D"
    assert result["inputs"] == ["existing_camtrack"]
    _cmd, params = server.typed_calls[0]
    assert params["camera_node"] == "existing_camtrack"
    assert params["frame"] == 1024


def test_bake_camera_to_card_idempotent(tracking_tools):
    server, _script, tools = tracking_tools
    first = tools["bake_camera_to_card"]("existing_camtrack", name="myCard")
    second = tools["bake_camera_to_card"]("existing_camtrack", name="myCard")
    assert first["name"] == second["name"] == "myCard"
    cards = [n for n, d in server.nodes.items() if d["type"] == "Card3D"]
    assert len(cards) == 1


def test_bake_camera_to_card_unknown_node(tracking_tools):
    _server, _script, tools = tracking_tools
    result = tools["bake_camera_to_card"]("nope")
    assert result.get("status") == "error"

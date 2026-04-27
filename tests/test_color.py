"""Tests for color.py (Phase C2 OCIO/ACEScct).

Five tools: ``get_color_management``, ``set_working_space``,
``audit_acescct_consistency``, ``convert_node_colorspace``,
``create_ocio_colorspace``. Tests follow the tracking.py / deep.py
pattern: stub MCP context registers tool callables, mock_script
fixture seeds inputs, and ``server.typed_calls`` records the wire
shape so we can assert without inspecting f-string blobs.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from nuke_mcp.tools import color


def test_module_importable() -> None:
    """Module is importable and exposes ``register``."""
    assert color is not None
    assert hasattr(color, "register")


# Pinned signatures the tools must satisfy. Only the leading
# positional-or-keyword params are checked; trailing optional params
# may be added later without breaking the pin.
_EXPECTED_SIGNATURES: dict[str, tuple[str, ...]] = {
    "get_color_management": (),
    "set_working_space": ("space",),
    "audit_acescct_consistency": (),
    "convert_node_colorspace": ("node", "in_cs", "out_cs"),
    "create_ocio_colorspace": ("input_node", "in_cs", "out_cs"),
}


# ---------------------------------------------------------------------------
# Stub MCP infrastructure (mirrors tracking.py / deep.py)
# ---------------------------------------------------------------------------


class _StubMCP:
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
def color_tools(mock_script):
    """Register color tools against a connected mock server.

    Seeds a Read plate (linear EXR, default colorspace) plus an extra
    Read for the audit nonlinear-extension scenarios. Tests opt in to
    Grade/Write nodes by mutating ``server.nodes`` directly.
    """
    server, script = mock_script
    server.nodes["plate"] = {
        "type": "Read",
        "knobs": {"colorspace": "ACES - ACEScg", "file": "/tmp/plate.exr"},
        "x": 0,
        "y": 0,
    }
    server.connections["plate"] = []
    ctx = _StubCtx()
    color.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# Signature pin checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "expected_params"), list(_EXPECTED_SIGNATURES.items()))
def test_registered_tool_signatures(
    color_tools, name: str, expected_params: tuple[str, ...]
) -> None:
    """Each registered tool advertises the pinned leading params."""
    _server, _script, tools = color_tools
    fn = tools.get(name)
    assert fn is not None, f"color tool {name!r} not registered"
    sig = inspect.signature(fn)
    actual = tuple(
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    )
    assert (
        actual[: len(expected_params)] == expected_params
    ), f"color.{name} signature drift: expected {expected_params}, got {actual}"


# ---------------------------------------------------------------------------
# get_color_management
# ---------------------------------------------------------------------------


def test_get_color_management_returns_root_state(color_tools):
    server, _script, tools = color_tools
    result = tools["get_color_management"]()
    assert result.get("status") != "error"
    assert result["color_management"] == "OCIO"
    assert result["ocio_config"] == "aces_1.3"
    assert result["working_space"] == "ACES - ACEScg"
    assert result["default_view"] == "ACES/sRGB"
    assert result["monitor_lut"] == "sRGB"
    cmd, params = server.typed_calls[0]
    assert cmd == "get_color_management"
    assert params == {}


def test_get_color_management_has_stable_keys(color_tools):
    """Wire shape stays stable even if mock state is mutated mid-test."""
    server, _script, tools = color_tools
    server.color_management["working_space"] = "ACES - ACEScct"
    result = tools["get_color_management"]()
    assert set(result) >= {
        "color_management",
        "ocio_config",
        "working_space",
        "default_view",
        "monitor_lut",
    }
    assert result["working_space"] == "ACES - ACEScct"


# ---------------------------------------------------------------------------
# set_working_space
# ---------------------------------------------------------------------------


def test_set_working_space_happy_path(color_tools):
    server, _script, tools = color_tools
    result = tools["set_working_space"]("ACES - ACEScct")
    assert result.get("status") != "error"
    assert result["working_space"] == "ACES - ACEScct"
    assert server.color_management["working_space"] == "ACES - ACEScct"
    cmd, params = server.typed_calls[0]
    assert cmd == "set_working_space"
    assert params == {"space": "ACES - ACEScct"}


def test_set_working_space_rejects_unknown_value(color_tools):
    """Allowlist check runs addon-side. A bogus space returns a structured error."""
    _server, _script, tools = color_tools
    result = tools["set_working_space"]("BOGUS - DropTable")
    assert result.get("status") == "error"
    assert "invalid" in result["error"].lower()


# ---------------------------------------------------------------------------
# audit_acescct_consistency
# ---------------------------------------------------------------------------


def test_audit_clean_scene_no_findings(color_tools):
    _server, _script, tools = color_tools
    result = tools["audit_acescct_consistency"]()
    assert result.get("status") != "error"
    assert result["findings"] == []


def test_audit_flags_read_with_srgb_path(color_tools):
    server, _script, tools = color_tools
    server.nodes["sRGB_tex"] = {
        "type": "Read",
        "knobs": {"colorspace": "default", "file": "/tmp/wall_sRGB.exr"},
        "x": 0,
        "y": 0,
    }
    server.connections["sRGB_tex"] = []
    result = tools["audit_acescct_consistency"]()
    findings = result["findings"]
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == "warning"
    assert f["node"] == "sRGB_tex"
    assert "non-linear" in f["message"]
    assert "sRGB - Texture" in f["fix_suggestion"]


def test_audit_flags_read_with_png_extension(color_tools):
    server, _script, tools = color_tools
    server.nodes["png_tex"] = {
        "type": "Read",
        "knobs": {"colorspace": "default", "file": "/tmp/concrete.png"},
        "x": 0,
        "y": 0,
    }
    server.connections["png_tex"] = []
    result = tools["audit_acescct_consistency"]()
    findings = [f for f in result["findings"] if f["node"] == "png_tex"]
    assert len(findings) == 1


def test_audit_flags_grade_without_acescct_conversion(color_tools):
    """Grade downstream of ACEScg working space without ACEScct converter."""
    server, _script, tools = color_tools
    # Working space is ACES - ACEScg by default. Build: plate -> grade.
    server.nodes["my_grade"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["my_grade"] = ["plate"]
    result = tools["audit_acescct_consistency"]()
    findings = [f for f in result["findings"] if f["node"] == "my_grade"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "warning"
    assert "ACEScct" in findings[0]["message"]


def test_audit_grade_satisfied_by_upstream_acescct(color_tools):
    """Upstream OCIOColorSpace -> ACEScct silences the Grade warning."""
    server, _script, tools = color_tools
    server.nodes["to_acescct"] = {
        "type": "OCIOColorSpace",
        "knobs": {"in_colorspace": "ACES - ACEScg", "out_colorspace": "ACES - ACEScct"},
        "x": 0,
        "y": 0,
    }
    server.connections["to_acescct"] = ["plate"]
    server.nodes["my_grade"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["my_grade"] = ["to_acescct"]
    result = tools["audit_acescct_consistency"]()
    grade_findings = [f for f in result["findings"] if f["node"] == "my_grade"]
    assert grade_findings == []


def test_audit_strict_false_demotes_grade_to_info(color_tools):
    server, _script, tools = color_tools
    server.nodes["my_grade"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["my_grade"] = ["plate"]
    result = tools["audit_acescct_consistency"](strict=False)
    findings = [f for f in result["findings"] if f["node"] == "my_grade"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "info"


def test_audit_flags_write_srgb_to_exr(color_tools):
    """Linear EXR delivery with sRGB tag is an error."""
    server, _script, tools = color_tools
    server.nodes["bad_write"] = {
        "type": "Write",
        "knobs": {"colorspace": "sRGB", "file": "/tmp/out_v001.exr"},
        "x": 0,
        "y": 0,
    }
    server.connections["bad_write"] = ["plate"]
    result = tools["audit_acescct_consistency"]()
    findings = [f for f in result["findings"] if f["node"] == "bad_write"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "error"


def test_audit_acescct_grade_skipped_when_not_acescg_pipe(color_tools):
    """Grade rule fires only when working space is ACEScg-family."""
    server, _script, tools = color_tools
    server.color_management["working_space"] = "Output - sRGB"
    server.nodes["my_grade"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["my_grade"] = ["plate"]
    result = tools["audit_acescct_consistency"]()
    findings = [f for f in result["findings"] if f["node"] == "my_grade"]
    assert findings == []


# ---------------------------------------------------------------------------
# convert_node_colorspace
# ---------------------------------------------------------------------------


def test_convert_node_colorspace_inserts_pair(color_tools):
    server, _script, tools = color_tools
    # Build: plate -> consumer (consumer feeds from plate at slot 0).
    server.nodes["consumer"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["consumer"] = ["plate"]

    result = tools["convert_node_colorspace"]("consumer", "ACES - ACEScg", "ACES - ACEScct")
    assert result.get("status") != "error"
    assert result["wrapped"] == "consumer"
    leading = result["leading"]
    trailing = result["trailing"]
    assert leading["type"] == "OCIOColorSpace"
    assert trailing["type"] == "OCIOColorSpace"
    # The wire shape gives both converters back as NodeRef dicts; the
    # leading converter sits between plate and consumer, the trailing
    # converter sits after consumer (no consumers in this scenario, so
    # trailing has no downstream).
    assert leading["inputs"] == ["plate"]
    assert trailing["inputs"] == ["consumer"]
    # Consumer's input 0 should now point at leading.
    assert server.connections["consumer"] == [leading["name"]]


def test_convert_node_colorspace_rewires_downstream_consumers(color_tools):
    """Existing downstream consumers should re-feed from trailing converter."""
    server, _script, tools = color_tools
    server.nodes["mid"] = {"type": "ColorCorrect", "knobs": {}, "x": 0, "y": 0}
    server.connections["mid"] = ["plate"]
    server.nodes["downstream"] = {"type": "Merge2", "knobs": {}, "x": 0, "y": 0}
    server.connections["downstream"] = ["mid", "plate"]

    result = tools["convert_node_colorspace"]("mid", "ACES - ACEScg", "ACES - ACEScct")
    trailing_name = result["trailing"]["name"]
    # Downstream's input 0 (which used to point at mid) should now
    # point at the trailing converter.
    assert server.connections["downstream"][0] == trailing_name
    # Slot 1 (plate) is untouched.
    assert server.connections["downstream"][1] == "plate"


def test_convert_node_colorspace_unknown_node(color_tools):
    _server, _script, tools = color_tools
    result = tools["convert_node_colorspace"]("does_not_exist", "ACES - ACEScg", "ACES - ACEScct")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# create_ocio_colorspace
# ---------------------------------------------------------------------------


def test_create_ocio_colorspace_happy_path(color_tools):
    server, _script, tools = color_tools
    result = tools["create_ocio_colorspace"]("plate", "ACES - ACEScg", "ACES - ACEScct")
    assert result.get("status") != "error"
    assert result["type"] == "OCIOColorSpace"
    assert result["inputs"] == ["plate"]
    cmd, params = server.typed_calls[0]
    assert cmd == "create_ocio_colorspace"
    assert params["input_node"] == "plate"
    assert params["in_cs"] == "ACES - ACEScg"
    assert params["out_cs"] == "ACES - ACEScct"


def test_create_ocio_colorspace_idempotent_on_name(color_tools):
    """Re-call with the same ``name=`` returns the existing NodeRef."""
    server, _script, tools = color_tools
    first = tools["create_ocio_colorspace"](
        "plate", "ACES - ACEScg", "ACES - ACEScct", name="plate_to_acescct"
    )
    second = tools["create_ocio_colorspace"](
        "plate", "ACES - ACEScg", "ACES - ACEScct", name="plate_to_acescct"
    )
    assert first["name"] == "plate_to_acescct"
    assert second["name"] == "plate_to_acescct"
    ocio_nodes = [n for n, d in server.nodes.items() if d["type"] == "OCIOColorSpace"]
    # Idempotent re-call must NOT have created a duplicate.
    assert len(ocio_nodes) == 1


def test_create_ocio_colorspace_unknown_input(color_tools):
    _server, _script, tools = color_tools
    result = tools["create_ocio_colorspace"]("nope", "ACES - ACEScg", "ACES - ACEScct")
    assert result.get("status") == "error"


def test_create_ocio_colorspace_idempotent_class_mismatch(color_tools):
    """Name collision with a different class raises a structured error."""
    server, _script, tools = color_tools
    # Pre-seed a Grade with the target name.
    server.nodes["already_used"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["already_used"] = ["plate"]
    result = tools["create_ocio_colorspace"](
        "plate", "ACES - ACEScg", "ACES - ACEScct", name="already_used"
    )
    assert result.get("status") == "error"
    assert "Grade" in result["error"]

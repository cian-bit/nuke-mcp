"""Tests for render.py setup_write / render_frames / setup_precomp / list_precomps.

A3 migrated ``setup_write`` to a typed addon handler with file_type +
path-traversal allowlists. ``setup_precomp`` and ``list_precomps`` are
out of A3 scope and still ship f-string ``execute_python`` payloads.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp import connection
from nuke_mcp.tools import render


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
def render_tools(mock_script):
    server, script = mock_script
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    ctx = _StubCtx()
    render.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# setup_write -- A3 typed dispatch
# ---------------------------------------------------------------------------


def test_setup_write_happy_path(render_tools):
    server, _script, tools = render_tools
    result = tools["setup_write"]("plate", "/tmp/out/plate.####.exr")
    assert result.get("status") != "error"
    assert server.executed_code == []
    assert len(server.typed_calls) == 1
    cmd, params = server.typed_calls[0]
    assert cmd == "setup_write"
    assert params == {
        "input_node": "plate",
        "path": "/tmp/out/plate.####.exr",
        "file_type": "exr",
        "colorspace": "scene_linear",
    }


def test_setup_write_path_traversal_rejected(render_tools):
    """A3 closes the f-string hazard: ``..`` in a path raises ValueError addon-side."""
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", "../../../etc/passwd")
    assert result.get("status") == "error"
    assert "path traversal" in result["error"].lower() or "invalid path" in result["error"].lower()


def test_setup_write_invalid_file_type_rejected(render_tools):
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", "/tmp/out.bad", file_type="badtype")
    assert result.get("status") == "error"
    assert "invalid" in result["error"].lower()


def test_setup_write_alternate_format(render_tools):
    server, _script, tools = render_tools
    tools["setup_write"]("plate", "/tmp/out.png", file_type="png", colorspace="sRGB")
    _cmd, params = server.typed_calls[0]
    assert params["file_type"] == "png"
    assert params["colorspace"] == "sRGB"


# ---------------------------------------------------------------------------
# render_frames -- destructive, gated on confirm
# ---------------------------------------------------------------------------


def test_render_frames_preview_without_confirm(render_tools):
    server, _script, tools = render_tools
    result = tools["render_frames"](write_node="MainWrite", first_frame=1001, last_frame=1010)
    assert "preview" in result
    assert "MainWrite" in result["preview"]
    assert "1001-1010" in result["preview"]
    # no execute_python / no render call without confirm
    assert server.executed_code == []


def test_render_frames_with_confirm_calls_render(render_tools):
    server, _script, tools = render_tools
    result = tools["render_frames"](
        write_node="MainWrite",
        first_frame=1001,
        last_frame=1005,
        confirm=True,
    )
    assert result.get("rendered") == "Write1"
    assert result.get("frames") == [1001, 1100]


def test_render_frames_no_args_preview_message(render_tools):
    """Calling with neither node nor frames produces a generic preview msg."""
    _server, _script, tools = render_tools
    result = tools["render_frames"]()
    assert "preview" in result
    assert "will render" in result["preview"]


# ---------------------------------------------------------------------------
# setup_precomp
# ---------------------------------------------------------------------------


def test_setup_precomp_basic(render_tools):
    server, _script, tools = render_tools
    tools["setup_precomp"]("plate")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when render.py migrates to typed handlers.
    assert "'plate'" in code
    assert "Write" in code
    assert "Read" in code


def test_setup_precomp_explicit_path(render_tools):
    server, _script, tools = render_tools
    tools["setup_precomp"]("plate", path="/tmp/precomp.####.exr")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when render.py migrates to typed handlers.
    assert "/tmp/precomp.####.exr" in code


def test_setup_precomp_with_name(render_tools):
    server, _script, tools = render_tools
    tools["setup_precomp"]("plate", name="bg_precomp")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when render.py migrates to typed handlers.
    assert "bg_precomp" in code


# ---------------------------------------------------------------------------
# list_precomps
# ---------------------------------------------------------------------------


def test_list_precomps_invokes_python(render_tools):
    server, _script, tools = render_tools
    tools["list_precomps"]()
    code = server.executed_code[0]
    # # A3: rewrite this assertion when render.py migrates to typed handlers.
    assert "allNodes" in code
    assert "Write" in code
    assert "Read" in code


def test_list_precomps_no_inputs(render_tools):
    """list_precomps takes no arguments. Confirm shape."""
    _server, _script, tools = render_tools
    result = tools["list_precomps"]()
    # mock _execute_python returns {} -- the wrapper just relays it
    assert isinstance(result, dict)


def test_list_precomps_disconnected(render_tools):
    _server, _script, tools = render_tools
    connection.disconnect()
    result = tools["list_precomps"]()
    assert isinstance(result, dict)

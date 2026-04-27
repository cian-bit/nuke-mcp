"""Tests for viewer.py: view_node and set_viewer_lut.

view_node is a typed handler; set_viewer_lut still ships f-string Python.

# A3: rewrite the set_viewer_lut assertions when viewer.py migrates to typed handlers.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp import connection
from nuke_mcp.tools import viewer


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
def viewer_tools(mock_script):
    server, script = mock_script
    ctx = _StubCtx()
    viewer.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# view_node  (typed handler)
# ---------------------------------------------------------------------------


def test_view_node_happy(viewer_tools):
    _server, _script, tools = viewer_tools
    connection.send("create_node", type="Grade", name="g")
    result = tools["view_node"]("g")
    assert result.get("viewing") == "g"


def test_view_node_missing_returns_error(viewer_tools):
    _server, _script, tools = viewer_tools
    result = tools["view_node"]("nope")
    assert result.get("status") == "error"
    assert "node not found" in result.get("error", "")


# ---------------------------------------------------------------------------
# set_viewer_lut  (string injection)
# ---------------------------------------------------------------------------


def test_set_viewer_lut_happy(viewer_tools):
    server, _script, tools = viewer_tools
    tools["set_viewer_lut"]("sRGB")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when viewer.py migrates to typed handlers.
    assert "'sRGB'" in code
    assert "viewerProcess" in code
    assert "activeViewer" in code


def test_set_viewer_lut_alternate(viewer_tools):
    server, _script, tools = viewer_tools
    tools["set_viewer_lut"]("Cineon")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when viewer.py migrates to typed handlers.
    assert "'Cineon'" in code

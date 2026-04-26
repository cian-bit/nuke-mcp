"""Tests for roto.py: create_roto and list_roto_shapes.

# A3: rewrite this module's assertions when roto.py migrates to typed handlers.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp.tools import roto


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
def roto_tools(mock_script):
    server, script = mock_script
    ctx = _StubCtx()
    roto.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# create_roto
# ---------------------------------------------------------------------------


def test_create_roto_default_type(roto_tools):
    server, _script, tools = roto_tools
    tools["create_roto"]("plate")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when roto.py migrates to typed handlers.
    assert "'plate'" in code
    assert "'Roto'" in code


def test_create_roto_rotopaint(roto_tools):
    server, _script, tools = roto_tools
    tools["create_roto"]("plate", roto_type="RotoPaint")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when roto.py migrates to typed handlers.
    assert "'RotoPaint'" in code


def test_create_roto_repeat_invocation(roto_tools):
    server, _script, tools = roto_tools
    tools["create_roto"]("plate")
    tools["create_roto"]("plate")
    # # A3: rewrite this assertion when roto.py migrates to typed handlers.
    # Each call ships its own payload -- caller is responsible for de-duping.
    assert len(server.executed_code) == 2


# ---------------------------------------------------------------------------
# list_roto_shapes
# ---------------------------------------------------------------------------


def test_list_roto_shapes_happy(roto_tools):
    server, _script, tools = roto_tools
    tools["list_roto_shapes"]("Roto1")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when roto.py migrates to typed handlers.
    assert "'Roto1'" in code
    assert "rootLayer" in code
    assert "curves" in code


def test_list_roto_shapes_walks_recursively(roto_tools):
    server, _script, tools = roto_tools
    tools["list_roto_shapes"]("Roto1")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when roto.py migrates to typed handlers.
    # Recursive walk has a function defined and called.
    assert "def walk(" in code
    assert "walk(root_layer)" in code


def test_list_roto_shapes_unique_node(roto_tools):
    server, _script, tools = roto_tools
    tools["list_roto_shapes"]("MyRoto")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when roto.py migrates to typed handlers.
    assert "'MyRoto'" in code

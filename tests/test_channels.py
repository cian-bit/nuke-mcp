"""Tests for channels.py: list_channels / shuffle_channels / setup_aov_merge.

# A3: rewrite this module's assertions when channels.py migrates to typed handlers.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp import connection
from nuke_mcp.tools import channels


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
def channel_tools(mock_script):
    server, script = mock_script
    ctx = _StubCtx()
    channels.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# list_channels  (typed handler -- not via execute_python)
# ---------------------------------------------------------------------------


def test_list_channels_happy(channel_tools):
    server, _script, tools = channel_tools
    # need a node in the dict-graph for list_channels handler
    connection.send("create_node", type="Read", name="plate")
    result = tools["list_channels"]("plate")
    assert "layers" in result
    assert "rgba" in result["layers"]


def test_list_channels_missing_node(channel_tools):
    server, _script, tools = channel_tools
    # Connect, but no node -- handler raises ValueError.
    result = tools["list_channels"]("nope")
    # nuke_command catches CommandError and returns structured error dict.
    assert result.get("status") == "error"
    assert "node not found" in result.get("error", "")


def test_list_channels_returns_layer_dict(channel_tools):
    server, _script, tools = channel_tools
    connection.send("create_node", type="Read", name="aov_plate")
    result = tools["list_channels"]("aov_plate")
    assert isinstance(result["layers"], dict)
    assert isinstance(result["layers"]["rgba"], list)


# ---------------------------------------------------------------------------
# shuffle_channels  (string-injection)
# ---------------------------------------------------------------------------


def test_shuffle_channels_happy(channel_tools):
    server, _script, tools = channel_tools
    tools["shuffle_channels"]("plate", "diffuse")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when channels.py migrates to typed handlers.
    assert "'plate'" in code
    assert "'diffuse'" in code
    assert "'rgba'" in code  # default to_layer
    assert "Shuffle2" in code


def test_shuffle_channels_explicit_target(channel_tools):
    server, _script, tools = channel_tools
    tools["shuffle_channels"]("plate", "specular", to_layer="rgba")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when channels.py migrates to typed handlers.
    assert "'specular'" in code
    assert "'rgba'" in code


def test_shuffle_channels_depth_to_alpha(channel_tools):
    server, _script, tools = channel_tools
    tools["shuffle_channels"]("plate", "depth", to_layer="alpha")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when channels.py migrates to typed handlers.
    assert "'depth'" in code
    assert "'alpha'" in code


# ---------------------------------------------------------------------------
# setup_aov_merge  (string-injection, comma-split)
# ---------------------------------------------------------------------------


def test_setup_aov_merge_two_reads(channel_tools):
    server, _script, tools = channel_tools
    tools["setup_aov_merge"]("diffuse,specular")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when channels.py migrates to typed handlers.
    assert "'diffuse'" in code
    assert "'specular'" in code
    assert "Merge2" in code
    assert "plus" in code  # additive merge default (double-quoted in source)


def test_setup_aov_merge_three_reads(channel_tools):
    server, _script, tools = channel_tools
    tools["setup_aov_merge"]("diffuse,specular,emission")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when channels.py migrates to typed handlers.
    for name in ["diffuse", "specular", "emission"]:
        assert f"'{name}'" in code


def test_setup_aov_merge_strips_whitespace(channel_tools):
    """Comma split should trim whitespace -- locking in current behaviour."""
    server, _script, tools = channel_tools
    tools["setup_aov_merge"]("diffuse , specular ")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when channels.py migrates to typed handlers.
    # whitespace stripped before names land in the payload list literal
    assert "'diffuse'" in code
    assert "'specular'" in code

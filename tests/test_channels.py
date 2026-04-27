"""Tests for channels.py: list_channels / shuffle_channels.

C3 migrated ``setup_aov_merge`` to ``tools/aov.py`` alongside the new
``detect_aov_layers`` and ``setup_karma_aov_pipeline`` tools.
``test_aov.py`` covers the migrated tool plus the new pipeline. Tests
in this file no longer touch ``setup_aov_merge``.

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
# setup_aov_merge migrated to tools/aov.py in Phase C3 -- coverage lives
# in tests/test_aov.py now. This module no longer references it.
# ---------------------------------------------------------------------------

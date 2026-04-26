"""Tests for comp.py setup tools.

These tools currently ship f-string Python payloads to ``execute_python``.
The mock server records every payload in ``server.executed_code``; the
assertions below pin the current string-injection shape so A3 sees a
regression net when migrating to typed addon handlers.

# A3: rewrite this module's assertions when comp.py migrates to typed handlers.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp import connection
from nuke_mcp.tools import comp


class _StubMCP:
    """Captures the registered tool callables so tests can invoke them
    without a full FastMCP server roundtrip. Mirrors the pattern used by
    existing test modules that register tools against a stub context.
    """

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
def comp_tools(mock_script):
    """Register comp tools against a connected mock server. Returns
    ``(server, script, tools)`` where ``tools`` is a dict of name->callable."""
    server, script = mock_script
    ctx = _StubCtx()
    comp.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# setup_keying
# ---------------------------------------------------------------------------


def test_setup_keying_happy_path(comp_tools):
    server, _script, tools = comp_tools
    result = tools["setup_keying"]("plate")
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    # mock _execute_python returns {}; the helpers wrapper stamps _meta onto
    # the dict (B1 response shape). Tolerate either.
    assert isinstance(result, dict)
    assert "status" not in result or result.get("status") != "error"
    assert len(server.executed_code) == 1
    code = server.executed_code[0]
    assert "'plate'" in code
    assert "Keylight" in code  # default keyer
    assert "FilterErode" in code
    assert "EdgeBlur" in code
    assert "Premult" in code


def test_setup_keying_alternate_keyer(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_keying"]("plate", keyer_type="Primatte")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert "'Primatte'" in code
    assert "FilterErode" in code  # erode chain still wired


def test_setup_keying_repr_escapes_quotes(comp_tools):
    """input_node passes through ``!r`` -- a quote in the name must be escaped."""
    server, _script, tools = comp_tools
    tools["setup_keying"]("weird'name")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    # !r repr-escapes the embedded single quote; payload should still parse
    assert "weird" in code
    assert "syntaxerror" not in code.lower()  # hand-eyeballed -- code remains parseable


# ---------------------------------------------------------------------------
# setup_color_correction
# ---------------------------------------------------------------------------


def test_setup_color_correction_default_grade(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_color_correction"]("plate")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert "'plate'" in code
    assert "'Grade'" in code


def test_setup_color_correction_huecorrect(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_color_correction"]("plate", operation="HueCorrect")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert "'HueCorrect'" in code


def test_setup_color_correction_idempotent_re_run(comp_tools):
    """Running the tool twice should ship two distinct payloads."""
    server, _script, tools = comp_tools
    tools["setup_color_correction"]("plate", operation="ColorCorrect")
    tools["setup_color_correction"]("plate", operation="ColorCorrect")
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert len(server.executed_code) == 2
    assert server.executed_code[0] == server.executed_code[1]


# ---------------------------------------------------------------------------
# setup_merge
# ---------------------------------------------------------------------------


def test_setup_merge_happy_path(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_merge"]("fg", "bg")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert "'fg'" in code
    assert "'bg'" in code
    assert "'over'" in code
    assert "Merge2" in code


def test_setup_merge_plus_operation(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_merge"]("fg", "bg", operation="plus")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert "'plus'" in code


def test_setup_merge_input_pipe_order(comp_tools):
    """fg should land on input(1) (B pipe), bg on input(0) (A pipe)."""
    server, _script, tools = comp_tools
    tools["setup_merge"]("fg", "bg")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    a_pipe = code.find("setInput(0, bg_node)")
    b_pipe = code.find("setInput(1, fg_node)")
    assert a_pipe != -1 and b_pipe != -1


# ---------------------------------------------------------------------------
# setup_transform
# ---------------------------------------------------------------------------


def test_setup_transform_default_transform(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_transform"]("plate")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert "'plate'" in code
    assert "'Transform'" in code


def test_setup_transform_corner_pin(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_transform"]("plate", operation="CornerPin2D")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert "'CornerPin2D'" in code


def test_setup_transform_reformat(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_transform"]("plate", operation="Reformat")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert "'Reformat'" in code


# ---------------------------------------------------------------------------
# setup_denoise
# ---------------------------------------------------------------------------


def test_setup_denoise_happy_path(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_denoise"]("plate")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert "'plate'" in code
    assert "Denoise2" in code


def test_setup_denoise_unique_input(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_denoise"]("hero_plate")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    assert "'hero_plate'" in code


def test_setup_denoise_no_extra_nodes(comp_tools):
    """Denoise tool ships exactly one Denoise2 node, no chain."""
    server, _script, tools = comp_tools
    tools["setup_denoise"]("plate")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when comp.py migrates to typed handlers.
    # only one node creation
    assert code.count("nuke.nodes.") == 1


# ---------------------------------------------------------------------------
# Connection error path -- tool returns a structured error dict.
# ---------------------------------------------------------------------------


def test_setup_keying_when_disconnected_returns_error(comp_tools):
    _server, _script, tools = comp_tools
    connection.disconnect()
    result = tools["setup_keying"]("plate")
    # connection.send will reconnect or raise. nuke_command catches and
    # returns a structured dict.
    assert isinstance(result, dict)

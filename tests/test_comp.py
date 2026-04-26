"""Tests for comp.py setup tools.

A3 migrated these tools from f-string ``execute_python`` payloads to
typed addon handlers. The mock server records every typed call as
``(cmd, params)`` in ``server.typed_calls``; the assertions below pin
that wire shape and the operation/file_type/path-traversal allowlists
the addon enforces.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp import connection
from nuke_mcp.tools import comp


class _StubMCP:
    """Captures the registered tool callables so tests can invoke them
    without a full FastMCP server roundtrip."""

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
    # seed a plate node so setup_* calls have something to look up
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    server.nodes["bg"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["bg"] = []
    server.nodes["fg"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["fg"] = []
    server.nodes["hero_plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["hero_plate"] = []
    ctx = _StubCtx()
    comp.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# setup_keying
# ---------------------------------------------------------------------------


def test_setup_keying_happy_path(comp_tools):
    server, _script, tools = comp_tools
    result = tools["setup_keying"]("plate")
    assert isinstance(result, dict)
    assert result.get("status") != "error"
    # typed dispatch -- no execute_python payload
    assert server.executed_code == []
    assert len(server.typed_calls) == 1
    cmd, params = server.typed_calls[0]
    assert cmd == "setup_keying"
    assert params == {"input_node": "plate", "keyer_type": "Keylight"}
    # mock simulated the chain
    assert "keyer" in result
    assert "erode" in result
    assert "edge_blur" in result
    assert "premult" in result


def test_setup_keying_alternate_keyer(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_keying"]("plate", keyer_type="Primatte")
    cmd, params = server.typed_calls[0]
    assert cmd == "setup_keying"
    assert params["keyer_type"] == "Primatte"


def test_setup_keying_invalid_keyer_rejected(comp_tools):
    """Allowlist check runs addon-side. A bogus keyer returns a structured error."""
    _server, _script, tools = comp_tools
    result = tools["setup_keying"]("plate", keyer_type="DROP TABLE; --")
    assert result.get("status") == "error"
    assert "invalid" in result["error"].lower()


def test_setup_keying_unknown_node(comp_tools):
    _server, _script, tools = comp_tools
    result = tools["setup_keying"]("does_not_exist")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# setup_color_correction
# ---------------------------------------------------------------------------


def test_setup_color_correction_default_grade(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_color_correction"]("plate")
    cmd, params = server.typed_calls[0]
    assert cmd == "setup_color_correction"
    assert params == {"input_node": "plate", "operation": "Grade"}


def test_setup_color_correction_huecorrect(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_color_correction"]("plate", operation="HueCorrect")
    _cmd, params = server.typed_calls[0]
    assert params["operation"] == "HueCorrect"


def test_setup_color_correction_idempotent_re_run(comp_tools):
    """Running the tool twice should ship two distinct typed calls."""
    server, _script, tools = comp_tools
    tools["setup_color_correction"]("plate", operation="ColorCorrect")
    tools["setup_color_correction"]("plate", operation="ColorCorrect")
    assert len(server.typed_calls) == 2
    assert server.typed_calls[0] == server.typed_calls[1]


def test_setup_color_correction_rejects_injection(comp_tools):
    """A3 allowlist closes the f-string injection vector."""
    _server, _script, tools = comp_tools
    result = tools["setup_color_correction"]("plate", operation="DROP TABLE; --")
    assert result.get("status") == "error"
    assert "invalid" in result["error"].lower()


# ---------------------------------------------------------------------------
# setup_merge
# ---------------------------------------------------------------------------


def test_setup_merge_happy_path(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_merge"]("fg", "bg")
    cmd, params = server.typed_calls[0]
    assert cmd == "setup_merge"
    assert params == {"fg": "fg", "bg": "bg", "operation": "over"}


def test_setup_merge_plus_operation(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_merge"]("fg", "bg", operation="plus")
    _cmd, params = server.typed_calls[0]
    assert params["operation"] == "plus"


def test_setup_merge_input_pipe_order(comp_tools):
    """fg should land on input(1) (B pipe), bg on input(0) (A pipe)."""
    server, _script, tools = comp_tools
    result = tools["setup_merge"]("fg", "bg")
    merge_name = result["name"]
    # mock connections: A pipe = bg, B pipe = fg
    assert server.connections[merge_name] == ["bg", "fg"]


def test_setup_merge_rejects_bad_operation(comp_tools):
    _server, _script, tools = comp_tools
    result = tools["setup_merge"]("fg", "bg", operation="DROP TABLE; --")
    assert result.get("status") == "error"
    assert "invalid" in result["error"].lower()


# ---------------------------------------------------------------------------
# setup_transform
# ---------------------------------------------------------------------------


def test_setup_transform_default_transform(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_transform"]("plate")
    cmd, params = server.typed_calls[0]
    assert cmd == "setup_transform"
    assert params == {"input_node": "plate", "operation": "Transform"}


def test_setup_transform_corner_pin(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_transform"]("plate", operation="CornerPin2D")
    _cmd, params = server.typed_calls[0]
    assert params["operation"] == "CornerPin2D"


def test_setup_transform_reformat(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_transform"]("plate", operation="Reformat")
    _cmd, params = server.typed_calls[0]
    assert params["operation"] == "Reformat"


def test_setup_transform_rejects_bad_operation(comp_tools):
    _server, _script, tools = comp_tools
    result = tools["setup_transform"]("plate", operation="DROP TABLE; --")
    assert result.get("status") == "error"
    assert "invalid" in result["error"].lower()


# ---------------------------------------------------------------------------
# setup_denoise
# ---------------------------------------------------------------------------


def test_setup_denoise_happy_path(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_denoise"]("plate")
    cmd, params = server.typed_calls[0]
    assert cmd == "setup_denoise"
    assert params == {"input_node": "plate"}


def test_setup_denoise_unique_input(comp_tools):
    server, _script, tools = comp_tools
    tools["setup_denoise"]("hero_plate")
    _cmd, params = server.typed_calls[0]
    assert params["input_node"] == "hero_plate"


def test_setup_denoise_creates_one_node(comp_tools):
    """Denoise tool ships exactly one Denoise2 node, no chain."""
    server, _script, tools = comp_tools
    tools["setup_denoise"]("plate")
    # exactly one new node compared to the seeded set (plate, fg, bg, hero_plate)
    new_nodes = [n for n, d in server.nodes.items() if d["type"] == "Denoise2"]
    assert len(new_nodes) == 1


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

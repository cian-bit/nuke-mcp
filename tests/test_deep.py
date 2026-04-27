"""Tests for deep.py.

C1 filled the module: create_deep_recolor, create_deep_merge,
create_deep_holdout, create_deep_transform, deep_to_image. The
signature pins below assert the public surface as shipped.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from nuke_mcp.tools import deep


def test_module_importable() -> None:
    """Module is importable and exposes register()."""
    assert deep is not None
    assert hasattr(deep, "register")


# Pinned signatures C1 must satisfy. Only positional-or-keyword
# parameters count; *args / **kwargs are ignored. The entries are
# deliberate minimums -- the implementation may add KW-only optional
# params later, but the listed names + order must match.
_EXPECTED_SIGNATURES: dict[str, tuple[str, ...]] = {
    "create_deep_recolor": ("deep_node", "color_node"),
    "create_deep_merge": ("a_node", "b_node"),
    "create_deep_holdout": ("subject_node", "holdout_node"),
    "create_deep_transform": ("input_node",),
    "deep_to_image": ("input_node",),
}


# ---------------------------------------------------------------------------
# Stub MCP infrastructure
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
def deep_tools(mock_script):
    """Register deep tools against a connected mock server.

    Seeds two deep nodes, a 2D colour node, and a holdout deep node so
    every primitive has a valid input to point at.
    """
    server, script = mock_script
    server.nodes["deepA"] = {"type": "DeepRead", "knobs": {}, "x": 0, "y": 0}
    server.connections["deepA"] = []
    server.nodes["deepB"] = {"type": "DeepRead", "knobs": {}, "x": 0, "y": 0}
    server.connections["deepB"] = []
    server.nodes["holdoutDeep"] = {"type": "DeepRead", "knobs": {}, "x": 0, "y": 0}
    server.connections["holdoutDeep"] = []
    server.nodes["beauty"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["beauty"] = []
    ctx = _StubCtx()
    deep.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# Signature pin checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "expected_params"), list(_EXPECTED_SIGNATURES.items()))
def test_registered_tool_signatures(
    deep_tools, name: str, expected_params: tuple[str, ...]
) -> None:
    """Each registered tool advertises the pinned leading params."""
    _server, _script, tools = deep_tools
    fn = tools.get(name)
    assert fn is not None, f"deep tool {name!r} not registered"
    sig = inspect.signature(fn)
    actual = tuple(
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    )
    assert (
        actual[: len(expected_params)] == expected_params
    ), f"deep.{name} signature drift: expected {expected_params}, got {actual}"


# ---------------------------------------------------------------------------
# create_deep_recolor
# ---------------------------------------------------------------------------


def test_create_deep_recolor_happy_path(deep_tools):
    server, _script, tools = deep_tools
    result = tools["create_deep_recolor"]("deepA", "beauty")
    assert isinstance(result, dict)
    assert result.get("status") != "error"
    assert result["type"] == "DeepRecolor"
    assert result["inputs"][:2] == ["deepA", "beauty"]
    cmd, params = server.typed_calls[0]
    assert cmd == "create_deep_recolor"
    assert params["deep_node"] == "deepA"
    assert params["color_node"] == "beauty"
    assert params["target_input_alpha"] is True


def test_create_deep_recolor_idempotent(deep_tools):
    server, _script, tools = deep_tools
    first = tools["create_deep_recolor"]("deepA", "beauty", name="myRecolor")
    second = tools["create_deep_recolor"]("deepA", "beauty", name="myRecolor")
    assert first["name"] == second["name"] == "myRecolor"
    nodes = [n for n, d in server.nodes.items() if d["type"] == "DeepRecolor"]
    assert len(nodes) == 1


def test_create_deep_recolor_unknown_deep_node(deep_tools):
    _server, _script, tools = deep_tools
    result = tools["create_deep_recolor"]("nope", "beauty")
    assert result.get("status") == "error"


def test_create_deep_recolor_unknown_color_node(deep_tools):
    _server, _script, tools = deep_tools
    result = tools["create_deep_recolor"]("deepA", "nope")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# create_deep_merge
# ---------------------------------------------------------------------------


def test_create_deep_merge_happy_path(deep_tools):
    server, _script, tools = deep_tools
    result = tools["create_deep_merge"]("deepA", "deepB")
    assert result["type"] == "DeepMerge"
    assert result["inputs"][:2] == ["deepA", "deepB"]
    _cmd, params = server.typed_calls[0]
    assert params["op"] == "over"


def test_create_deep_merge_holdout_op(deep_tools):
    server, _script, tools = deep_tools
    result = tools["create_deep_merge"]("deepA", "deepB", op="holdout")
    assert result["type"] == "DeepMerge"
    _cmd, params = server.typed_calls[0]
    assert params["op"] == "holdout"


def test_create_deep_merge_idempotent(deep_tools):
    server, _script, tools = deep_tools
    first = tools["create_deep_merge"]("deepA", "deepB", name="myDeepMerge")
    second = tools["create_deep_merge"]("deepA", "deepB", name="myDeepMerge")
    assert first["name"] == second["name"] == "myDeepMerge"
    nodes = [n for n, d in server.nodes.items() if d["type"] == "DeepMerge"]
    assert len(nodes) == 1


def test_create_deep_merge_rejects_bad_op(deep_tools):
    _server, _script, tools = deep_tools
    result = tools["create_deep_merge"]("deepA", "deepB", op="DROP TABLE; --")
    assert result.get("status") == "error"
    assert "invalid" in result["error"].lower()


def test_create_deep_merge_unknown_input(deep_tools):
    _server, _script, tools = deep_tools
    result = tools["create_deep_merge"]("nope", "deepB")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# create_deep_holdout
# ---------------------------------------------------------------------------


def test_create_deep_holdout_happy_path(deep_tools):
    server, _script, tools = deep_tools
    result = tools["create_deep_holdout"]("deepA", "holdoutDeep")
    assert result["type"] == "DeepHoldout2"
    assert result["inputs"][:2] == ["deepA", "holdoutDeep"]
    cmd, params = server.typed_calls[0]
    assert cmd == "create_deep_holdout"
    assert params["subject_node"] == "deepA"
    assert params["holdout_node"] == "holdoutDeep"


def test_create_deep_holdout_idempotent(deep_tools):
    server, _script, tools = deep_tools
    first = tools["create_deep_holdout"]("deepA", "holdoutDeep", name="myHoldout")
    second = tools["create_deep_holdout"]("deepA", "holdoutDeep", name="myHoldout")
    assert first["name"] == second["name"] == "myHoldout"
    nodes = [n for n, d in server.nodes.items() if d["type"] == "DeepHoldout2"]
    assert len(nodes) == 1


def test_create_deep_holdout_unknown_subject(deep_tools):
    _server, _script, tools = deep_tools
    result = tools["create_deep_holdout"]("nope", "holdoutDeep")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# create_deep_transform
# ---------------------------------------------------------------------------


def test_create_deep_transform_happy_path(deep_tools):
    server, _script, tools = deep_tools
    result = tools["create_deep_transform"]("deepA", translate=(1.0, 2.0, 3.0))
    assert result["type"] == "DeepTransform"
    assert result["inputs"] == ["deepA"]
    _cmd, params = server.typed_calls[0]
    assert params["translate"] == [1.0, 2.0, 3.0]


def test_create_deep_transform_default_translate(deep_tools):
    server, _script, tools = deep_tools
    tools["create_deep_transform"]("deepA")
    _cmd, params = server.typed_calls[0]
    assert params["translate"] == [0.0, 0.0, 0.0]


def test_create_deep_transform_idempotent(deep_tools):
    server, _script, tools = deep_tools
    first = tools["create_deep_transform"]("deepA", name="myDT")
    second = tools["create_deep_transform"]("deepA", name="myDT")
    assert first["name"] == second["name"] == "myDT"
    nodes = [n for n, d in server.nodes.items() if d["type"] == "DeepTransform"]
    assert len(nodes) == 1


def test_create_deep_transform_unknown_input(deep_tools):
    _server, _script, tools = deep_tools
    result = tools["create_deep_transform"]("nope")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# deep_to_image
# ---------------------------------------------------------------------------


def test_deep_to_image_happy_path(deep_tools):
    server, _script, tools = deep_tools
    result = tools["deep_to_image"]("deepA")
    assert result["type"] == "DeepToImage"
    assert result["inputs"] == ["deepA"]
    cmd, params = server.typed_calls[0]
    assert cmd == "deep_to_image"
    assert params["input_node"] == "deepA"


def test_deep_to_image_idempotent(deep_tools):
    server, _script, tools = deep_tools
    first = tools["deep_to_image"]("deepA", name="myFlatten")
    second = tools["deep_to_image"]("deepA", name="myFlatten")
    assert first["name"] == second["name"] == "myFlatten"
    nodes = [n for n, d in server.nodes.items() if d["type"] == "DeepToImage"]
    assert len(nodes) == 1


def test_deep_to_image_unknown_input(deep_tools):
    _server, _script, tools = deep_tools
    result = tools["deep_to_image"]("nope")
    assert result.get("status") == "error"

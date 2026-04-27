"""Tests for tools/aov.py (Phase C3).

Coverage targets:

* ``detect_aov_layers`` -- parses pre-seeded ``aov_channels_per_layer``
  off the mock Read, returns the canonical Karma ordering.
* ``setup_karma_aov_pipeline`` -- builds Shuffle-per-layer, the
  reconstruction Merge2 chain, the Remove cleanup, and the QC
  Switch / diff / Grade triple. Idempotent on the ``name`` kwarg.
* ``setup_aov_merge`` -- migrated from channels.py. Same
  comma-separated wire shape, now driven by a typed addon handler.

A real Karma EXR fixture would weigh ~30MB, force Git LFS, and lock
us out of the standard fast test path. Instead we mock the
``Read.metadata()`` shape via the mock-server's ``aov_template_layers``
hook -- the addon-side handler reads the same channel dictionary off
the mock that the live addon would pull off ``Read.channels()``.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from nuke_mcp.tools import aov


def test_module_importable() -> None:
    """Module is importable and registers tools."""
    assert aov is not None
    assert hasattr(aov, "register")


# Pinned signatures C3 must satisfy. Trailing optional params are not
# included so future additions don't break the pin.
_EXPECTED_SIGNATURES: dict[str, tuple[str, ...]] = {
    "detect_aov_layers": ("read_node",),
    "setup_karma_aov_pipeline": ("read_path",),
    "setup_aov_merge": ("read_nodes",),
}


# ---------------------------------------------------------------------------
# Stub MCP infrastructure (mirrors test_tracking.py / test_deep.py)
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


# Canonical Karma EXR layer fixture. Mirrors what Solaris ships out of
# the box: beauty plus 8 light layers (the additive rebuild base) plus
# utility layers and one cryptomatte slot.
_KARMA_FIXTURE_LAYERS: dict[str, list[str]] = {
    "rgba": ["red", "green", "blue", "alpha"],
    "diffuse_direct": ["red", "green", "blue"],
    "diffuse_indirect": ["red", "green", "blue"],
    "specular_direct": ["red", "green", "blue"],
    "specular_indirect": ["red", "green", "blue"],
    "sss": ["red", "green", "blue"],
    "transmission": ["red", "green", "blue"],
    "emission": ["red", "green", "blue"],
    "volume": ["red", "green", "blue"],
    "P": ["x", "y", "z"],
    "N": ["x", "y", "z"],
    "depth": ["z"],
    "motion": ["u", "v"],
    "cryptomatte_object00": ["red", "green", "blue", "alpha"],
}


@pytest.fixture
def aov_tools(mock_script):
    """Register aov tools against a connected mock server.

    Seeds a Read node carrying the canonical Karma layer fixture so
    every detect/pipeline call has something concrete to introspect.
    """
    server, script = mock_script
    server.nodes["plate"] = {
        "type": "Read",
        "knobs": {"file": "/tmp/plate.exr", "format": "HD 1920x1080"},
        "x": 0,
        "y": 0,
        "aov_channels_per_layer": dict(_KARMA_FIXTURE_LAYERS),
        "aov_format": "HD 1920x1080",
    }
    server.connections["plate"] = []
    # Seed the karma template so setup_karma_aov_pipeline finds layers
    # on freshly-created Read nodes too.
    server.aov_template_layers = dict(_KARMA_FIXTURE_LAYERS)
    server.aov_template_format = "HD 1920x1080"
    ctx = _StubCtx()
    aov.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# Signature pin checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "expected_params"), list(_EXPECTED_SIGNATURES.items()))
def test_registered_tool_signatures(aov_tools, name: str, expected_params: tuple[str, ...]) -> None:
    """Each registered tool advertises the pinned leading params."""
    _server, _script, tools = aov_tools
    fn = tools.get(name)
    assert fn is not None, f"aov tool {name!r} not registered"
    sig = inspect.signature(fn)
    actual = tuple(
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    )
    assert (
        actual[: len(expected_params)] == expected_params
    ), f"aov.{name} signature drift: expected {expected_params}, got {actual}"


# ---------------------------------------------------------------------------
# detect_aov_layers
# ---------------------------------------------------------------------------


def test_detect_aov_layers_happy_path(aov_tools):
    """Returns the canonical Karma layer list, ordered, with format + channels."""
    server, _script, tools = aov_tools
    result = tools["detect_aov_layers"]("plate")
    assert result.get("status") != "error"
    layers = result["layers"]
    # Canonical order: rgba first, light layers next, utility after,
    # cryptomatte at the tail. Just check the leading + tail anchors.
    assert layers[0] == "rgba"
    assert "diffuse_direct" in layers
    assert "specular_indirect" in layers
    assert "depth" in layers
    assert layers[-1] == "cryptomatte_object00"
    assert result["format"] == "HD 1920x1080"
    assert "rgba" in result["channels_per_layer"]
    assert result["channels_per_layer"]["depth"] == ["z"]


def test_detect_aov_layers_unknown_node(aov_tools):
    _server, _script, tools = aov_tools
    result = tools["detect_aov_layers"]("does_not_exist")
    assert result.get("status") == "error"
    assert "node not found" in result["error"]


def test_detect_aov_layers_rejects_non_read(aov_tools):
    """A Merge2 isn't a Read and shouldn't be inspectable for AOV layers."""
    server, _script, tools = aov_tools
    server.nodes["not_read"] = {"type": "Merge2", "knobs": {}, "x": 0, "y": 0}
    server.connections["not_read"] = []
    result = tools["detect_aov_layers"]("not_read")
    assert result.get("status") == "error"
    assert "expected Read" in result["error"]


def test_detect_aov_layers_rgba_only_fallback(aov_tools):
    """A Read without the AOV template falls back to the default rgba layer."""
    server, _script, tools = aov_tools
    server.nodes["bare_read"] = {
        "type": "Read",
        "knobs": {"file": "/tmp/bare.exr"},
        "x": 0,
        "y": 0,
    }
    server.connections["bare_read"] = []
    result = tools["detect_aov_layers"]("bare_read")
    assert result.get("status") != "error"
    assert result["layers"] == ["rgba"]


# ---------------------------------------------------------------------------
# setup_karma_aov_pipeline
# ---------------------------------------------------------------------------


def test_setup_karma_aov_pipeline_happy_path(aov_tools):
    """Builds the full sub-graph and returns the wrapper Group ref."""
    server, _script, tools = aov_tools
    result = tools["setup_karma_aov_pipeline"]("/tmp/karma.0001.exr")
    assert result.get("status") != "error"
    # Wrapper Group present.
    assert result["type"] == "Group"
    group_name = result["name"]
    assert group_name.startswith("KarmaAOV_")
    # Layer summary surfaces the canonical Karma layers.
    assert "rgba" in result["layers"]
    assert "diffuse_direct" in result["rebuild_layers"]
    # Light layers all made it into the rebuild list.
    assert set(result["rebuild_layers"]) == {
        "diffuse_direct",
        "diffuse_indirect",
        "specular_direct",
        "specular_indirect",
        "sss",
        "transmission",
        "emission",
        "volume",
    }


def test_setup_karma_aov_pipeline_creates_expected_nodes(aov_tools):
    """One Shuffle per layer, one Merge per light layer, plus QC trio."""
    server, _script, tools = aov_tools
    nodes_before = set(server.nodes.keys())
    tools["setup_karma_aov_pipeline"]("/tmp/karma.0001.exr")
    new_nodes = set(server.nodes.keys()) - nodes_before
    types = [server.nodes[n]["type"] for n in new_nodes]
    # 14 layers in the fixture -> 14 Shuffle nodes.
    assert types.count("Shuffle") == 14
    # 8 light layers -> 8 reconstruction Merge2 nodes, plus 1 QC diff
    # Merge2, total 9.
    assert types.count("Merge2") == 9
    # Cleanup Remove + QC Switch + diff Grade.
    assert types.count("Remove") == 1
    assert types.count("Switch") == 1
    assert types.count("Grade") == 1
    # Wrapper Group.
    assert types.count("Group") == 1


def test_setup_karma_aov_pipeline_idempotent(aov_tools):
    """Re-call with the same name returns the existing Group, no new nodes."""
    server, _script, tools = aov_tools
    first = tools["setup_karma_aov_pipeline"]("/tmp/karma.0001.exr", name="MyKarma")
    nodes_after_first = dict(server.nodes)
    second = tools["setup_karma_aov_pipeline"]("/tmp/karma.0001.exr", name="MyKarma")
    assert first["name"] == second["name"] == "MyKarma"
    # No new nodes introduced on the re-call.
    assert set(server.nodes.keys()) == set(nodes_after_first.keys())
    # Only one Group of that name exists.
    groups = [n for n, d in server.nodes.items() if d["type"] == "Group"]
    assert groups.count("MyKarma") == 1


def test_setup_karma_aov_pipeline_idempotent_class_mismatch(aov_tools):
    """Existing non-Group at the requested name surfaces a clean error."""
    server, _script, tools = aov_tools
    server.nodes["MyKarma"] = {"type": "Merge2", "knobs": {}, "x": 0, "y": 0}
    server.connections["MyKarma"] = []
    result = tools["setup_karma_aov_pipeline"]("/tmp/karma.0001.exr", name="MyKarma")
    assert result.get("status") == "error"
    assert "expected 'Group'" in result["error"]


def test_setup_karma_aov_pipeline_missing_layer_fallback(aov_tools):
    """A Read whose EXR only carries rgba still builds a (minimal) pipeline."""
    server, _script, tools = aov_tools
    # Strip the template so the fresh Read built by the workflow tool
    # has only rgba.
    server.aov_template_layers = {"rgba": ["red", "green", "blue", "alpha"]}
    server.aov_template_format = "HD 1920x1080"
    result = tools["setup_karma_aov_pipeline"]("/tmp/sparse.exr")
    assert result.get("status") != "error"
    assert result["layers"] == ["rgba"]
    # No light layers means no reconstruction merges -- still legal.
    assert result["rebuild_layers"] == []


def test_setup_karma_aov_pipeline_rejects_empty_path(aov_tools):
    _server, _script, tools = aov_tools
    result = tools["setup_karma_aov_pipeline"]("")
    assert result.get("status") == "error"
    assert "non-empty string" in result["error"]


def test_setup_karma_aov_pipeline_records_typed_call(aov_tools):
    """The MCP-side tool ships through a typed handler, not execute_python."""
    server, _script, tools = aov_tools
    tools["setup_karma_aov_pipeline"]("/tmp/karma.0001.exr")
    # No execute_python ever fired.
    assert server.executed_code == []
    cmds = [cmd for cmd, _ in server.typed_calls]
    assert "setup_karma_aov_pipeline" in cmds


# ---------------------------------------------------------------------------
# setup_aov_merge (migrated from channels.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def aov_merge_tools(aov_tools):
    """Adds two extra Read nodes so setup_aov_merge has inputs to wire."""
    server, script, tools = aov_tools
    server.nodes["diffuse"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["diffuse"] = []
    server.nodes["specular"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["specular"] = []
    server.nodes["emission"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["emission"] = []
    return server, script, tools


def test_setup_aov_merge_two_reads(aov_merge_tools):
    server, _script, tools = aov_merge_tools
    result = tools["setup_aov_merge"]("diffuse,specular")
    assert result.get("status") != "error"
    assert len(result["merges"]) == 1
    assert result["inputs"] == ["diffuse", "specular"]
    cmd, params = server.typed_calls[-1]
    assert cmd == "setup_aov_merge"
    assert params["read_nodes"] == ["diffuse", "specular"]


def test_setup_aov_merge_three_reads_chain_length(aov_merge_tools):
    server, _script, tools = aov_merge_tools
    result = tools["setup_aov_merge"]("diffuse,specular,emission")
    assert result["inputs"] == ["diffuse", "specular", "emission"]
    # N-1 merges for N inputs.
    assert len(result["merges"]) == 2


def test_setup_aov_merge_strips_whitespace(aov_merge_tools):
    """Comma split trims whitespace before the typed-handler dispatch."""
    server, _script, tools = aov_merge_tools
    tools["setup_aov_merge"]("diffuse , specular ")
    _cmd, params = server.typed_calls[-1]
    assert params["read_nodes"] == ["diffuse", "specular"]


def test_setup_aov_merge_rejects_too_few(aov_merge_tools):
    _server, _script, tools = aov_merge_tools
    result = tools["setup_aov_merge"]("diffuse")
    assert result.get("status") == "error"
    assert "at least 2" in result["error"]


def test_setup_aov_merge_unknown_input(aov_merge_tools):
    _server, _script, tools = aov_merge_tools
    result = tools["setup_aov_merge"]("diffuse,nope")
    assert result.get("status") == "error"
    assert "node not found" in result["error"]


def test_setup_aov_merge_uses_typed_handler(aov_merge_tools):
    """C3 migration: no execute_python on the wire."""
    server, _script, tools = aov_merge_tools
    tools["setup_aov_merge"]("diffuse,specular")
    assert server.executed_code == []
    cmds = [cmd for cmd, _ in server.typed_calls]
    assert "setup_aov_merge" in cmds

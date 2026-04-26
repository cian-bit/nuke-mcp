"""B7 speed-win tests.

Three claims to verify against the mock layer:

1. ``read_comp`` p50 < 50ms on a 500-node fixture (mock is fast; the real
   point is to lock in a regression net, not benchmark Nuke).
2. The single-pass knob iteration in ``_handle_read_comp`` only visits
   each node entry once. ``MockNukeServer.read_comp_knob_visits`` counts
   visits; the assertion is ``visits == nodes_after_filter``.
3. ``scene_delta(prev_hash)`` short-circuits without enumerating nodes
   when the hash matches.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from nuke_mcp import connection
from nuke_mcp.tools import digest, read


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
def speed_tools(mock_script):
    server, _script = mock_script
    # 500-node fixture -- mix of types so type filtering exercises a code path
    for i in range(500):
        cls = "Grade" if i % 2 == 0 else "Blur"
        name = f"N_{i}"
        server.nodes[name] = {
            "type": cls,
            "knobs": {"mix": 0.5} if i % 3 == 0 else {},
            "x": i * 10,
            "y": i * 10,
        }
        server.connections[name] = []

    ctx = _StubCtx()
    read.register(ctx)
    digest.register(ctx)
    return server, ctx.mcp.registered


def test_read_comp_under_50ms_on_500_nodes(speed_tools):
    """p50 of 5 calls under 50ms. Mock is fast -- this is a regression net."""
    _server, tools = speed_tools

    samples = []
    for _ in range(5):
        t0 = time.perf_counter()
        tools["read_comp"]()
        samples.append((time.perf_counter() - t0) * 1000)

    samples.sort()
    p50 = samples[len(samples) // 2]
    assert p50 < 50.0, f"read_comp p50 {p50:.1f}ms exceeds 50ms budget; samples={samples}"


def test_read_comp_single_pass_visits_each_node_once(speed_tools):
    """B7: the single-pass loop visits each node exactly once."""
    server, tools = speed_tools
    server.read_comp_knob_visits = 0
    result = tools["read_comp"]()
    assert server.read_comp_knob_visits == 500
    assert result["count"] == 500


def test_read_comp_single_pass_with_type_filter(speed_tools):
    """Type-filter narrows the visit count to the matching subset."""
    server, tools = speed_tools
    server.read_comp_knob_visits = 0
    result = tools["read_comp"](type="Grade")
    # Only Grade nodes visited
    assert server.read_comp_knob_visits == 250
    assert result["count"] == 250


def test_scene_delta_short_circuits_without_enumeration(speed_tools):
    """Equal hash -> no node body in the response, short-circuit counter ticks."""
    server, tools = speed_tools
    initial = tools["scene_digest"]()
    h = initial["hash"]

    server.scene_delta_short_circuits = 0
    delta = tools["scene_delta"](prev_hash=h)

    assert delta.get("changed") is False
    assert delta.get("hash") == h
    assert "counts" not in delta
    assert server.scene_delta_short_circuits == 1


def test_warm_connect_round_trip(speed_tools):
    """Sanity: connection still hot, two calls in a row don't reconnect."""
    _server, tools = speed_tools
    a = tools["read_comp"]()
    b = tools["read_comp"]()
    assert a["count"] == b["count"]
    # connection state should be live
    assert connection.is_connected()

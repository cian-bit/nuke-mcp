"""Tests for the B7 scene_digest / scene_delta tools.

Coverage gate: ``tools/digest.py`` >= 85%.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp.tools import digest


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
def digest_tools(mock_script):
    server, _script = mock_script
    ctx = _StubCtx()
    digest.register(ctx)
    return server, ctx.mcp.registered


def test_scene_digest_returns_hash(digest_tools):
    server, tools = digest_tools
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    result = tools["scene_digest"]()
    assert "hash" in result
    assert isinstance(result["hash"], str)
    assert len(result["hash"]) == 8
    assert result["total"] == 1
    assert result["counts"]["Read"] == 1


def test_scene_digest_stable_across_no_op_calls(digest_tools):
    """Hash must be deterministic. Same scene -> same hash."""
    server, tools = digest_tools
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []

    a = tools["scene_digest"]()
    b = tools["scene_digest"]()
    assert a["hash"] == b["hash"]


def test_scene_digest_changes_when_node_added(digest_tools):
    server, tools = digest_tools
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    a = tools["scene_digest"]()

    server.nodes["g"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["g"] = []
    b = tools["scene_digest"]()

    assert a["hash"] != b["hash"]
    assert b["total"] == 2


def test_scene_delta_short_circuits_when_unchanged(digest_tools):
    """Equal hash -> minimal response, no node enumeration leaks into result."""
    server, tools = digest_tools
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    initial = tools["scene_digest"]()
    h = initial["hash"]

    delta = tools["scene_delta"](prev_hash=h)
    assert delta["changed"] is False
    assert delta["hash"] == h
    # short-circuit shape: no counts / total / errors leaked
    assert "counts" not in delta
    assert "total" not in delta
    assert server.scene_delta_short_circuits == 1


def test_scene_delta_returns_full_body_on_change(digest_tools):
    server, tools = digest_tools
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    initial = tools["scene_digest"]()
    old_hash = initial["hash"]

    server.nodes["g"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["g"] = []

    delta = tools["scene_delta"](prev_hash=old_hash)
    assert delta["changed"] is True
    assert delta["hash"] != old_hash
    # full body present on change
    assert delta["total"] == 2
    assert delta["counts"]["Grade"] == 1


def test_scene_delta_with_unknown_hash_returns_full(digest_tools):
    """Caller has no prior hash (cold start) -- pass empty string, get full body."""
    server, tools = digest_tools
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    delta = tools["scene_delta"](prev_hash="")
    assert delta.get("changed") is True
    assert "hash" in delta

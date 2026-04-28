"""Tests for distortion.py (Phase C4).

Covers the four C4 tools:

* ``bake_lens_distortion_envelope`` -- expected NetworkBox + 4 inner
  nodes (LensDistortion + 2 STMaps + Write). STMap cache path resolves
  through ``$SS`` -> ``$NUKE_MCP_SS_ROOT`` -> ``~/.nuke_mcp/stmaps``.
* ``apply_idistort`` -- UV channels wire through to the addon, tuple
  default lands as ``forward.u``/``forward.v``.
* ``apply_smartvector_propagate`` -- Task-wrapped, returns ``task_id``
  immediately and writes the Task record to disk in ``working`` state.
* ``generate_stmap`` -- Task-wrapped; idempotent on ``name`` so the
  same (lens, mode, name) tuple reuses an in-flight task instead of
  spawning a duplicate render.

Mirrors the test_render.py async layout: pin ``NUKE_MCP_TASK_DIR`` to
a per-test ``tmp_path`` so the real ``~/.nuke_mcp/tasks`` is never
touched, drive task_progress lines through the notification queue.
"""

from __future__ import annotations

import inspect
import pathlib
from typing import Any

import pytest

from nuke_mcp import connection
from nuke_mcp import tasks as task_store
from nuke_mcp.tools import distortion

# ---------------------------------------------------------------------------
# Stub MCP infrastructure (mirrors test_tracking / test_render)
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
def distortion_tools(mock_script):
    """Mock-server wired to the distortion-tools registry.

    Seeds a plate, a LensDistortion, and a SmartVector node so each
    primitive has a valid input to point at.
    """
    server, script = mock_script
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    server.nodes["lens_solve"] = {"type": "LensDistortion", "knobs": {}, "x": 0, "y": 0}
    server.connections["lens_solve"] = ["plate"]
    server.nodes["sv1"] = {"type": "SmartVector", "knobs": {}, "x": 0, "y": 0}
    server.connections["sv1"] = ["plate"]
    ctx = _StubCtx()
    distortion.register(ctx)
    return server, script, ctx.mcp.registered


@pytest.fixture
def distortion_tools_with_taskstore(distortion_tools, monkeypatch, tmp_path):
    """``distortion_tools`` with a clean per-test TaskStore.

    Pinning ``NUKE_MCP_TASK_DIR`` to ``tmp_path`` keeps async tests
    isolated from each other and from the real ``~/.nuke_mcp/tasks``.
    """
    monkeypatch.setenv("NUKE_MCP_TASK_DIR", str(tmp_path))
    task_store.reset_default_store()
    yield distortion_tools
    task_store.reset_default_store()


# ---------------------------------------------------------------------------
# Signature pins
# ---------------------------------------------------------------------------


_EXPECTED_SIGNATURES: dict[str, tuple[str, ...]] = {
    "bake_lens_distortion_envelope": ("plate", "lens_solve"),
    "apply_idistort": ("plate", "vector_node"),
    "apply_smartvector_propagate": ("plate", "paint_frame", "range_in", "range_out"),
    "generate_stmap": ("lens_distortion_node",),
}


@pytest.mark.parametrize(("name", "expected"), list(_EXPECTED_SIGNATURES.items()))
def test_registered_tool_signatures(distortion_tools, name: str, expected: tuple[str, ...]):
    """The four tools advertise the pinned leading parameters."""
    _server, _script, tools = distortion_tools
    fn = tools.get(name)
    assert fn is not None, f"distortion tool {name!r} not registered"
    sig = inspect.signature(fn)
    actual = tuple(
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
    )
    assert (
        actual[: len(expected)] == expected
    ), f"distortion.{name} signature drift: expected {expected}, got {actual}"


# ---------------------------------------------------------------------------
# bake_lens_distortion_envelope
# ---------------------------------------------------------------------------


def test_bake_lens_distortion_envelope_builds_expected_graph(distortion_tools):
    server, _script, tools = distortion_tools
    result = tools["bake_lens_distortion_envelope"]("plate", "lens_solve")
    assert result.get("status") != "error", result
    assert result["box"] == "LinearComp_undistorted_plate"
    assert result["head"] == [
        "LinearComp_undistorted_plate_head_lensdistortion",
        "LinearComp_undistorted_plate_head_stmap",
    ]
    assert result["tail"] == [
        "LinearComp_undistorted_plate_tail_stmap",
        "LinearComp_undistorted_plate_write",
    ]
    # All four body nodes + the box exist on the mock server.
    expected = {
        "LinearComp_undistorted_plate",
        "LinearComp_undistorted_plate_head_lensdistortion",
        "LinearComp_undistorted_plate_head_stmap",
        "LinearComp_undistorted_plate_tail_stmap",
        "LinearComp_undistorted_plate_write",
    }
    assert expected.issubset(set(server.nodes.keys()))
    assert server.nodes["LinearComp_undistorted_plate"]["type"] == "BackdropNode"


def test_bake_lens_distortion_envelope_idempotent_on_name(distortion_tools):
    server, _script, tools = distortion_tools
    first = tools["bake_lens_distortion_envelope"]("plate", "lens_solve", name="myEnvelope")
    second = tools["bake_lens_distortion_envelope"]("plate", "lens_solve", name="myEnvelope")
    assert first["box"] == second["box"] == "myEnvelope"
    # Idempotent re-call did NOT duplicate the four body nodes.
    backdrops = [n for n, d in server.nodes.items() if d["type"] == "BackdropNode"]
    assert len(backdrops) == 1


def test_bake_lens_distortion_envelope_unknown_plate(distortion_tools):
    _server, _script, tools = distortion_tools
    result = tools["bake_lens_distortion_envelope"]("does_not_exist", "lens_solve")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# STMap cache path resolution
# ---------------------------------------------------------------------------


def test_stmap_cache_root_uses_ss_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("SS", str(tmp_path))
    monkeypatch.delenv("NUKE_MCP_SS_ROOT", raising=False)
    root = distortion._resolve_stmap_cache_root()
    assert root == pathlib.Path(tmp_path) / "comp" / "stmaps"


def test_stmap_cache_root_falls_back_to_nuke_mcp_ss_root(monkeypatch, tmp_path):
    monkeypatch.delenv("SS", raising=False)
    monkeypatch.setenv("NUKE_MCP_SS_ROOT", str(tmp_path))
    root = distortion._resolve_stmap_cache_root()
    assert root == pathlib.Path(tmp_path) / "comp" / "stmaps"


def test_stmap_cache_root_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("SS", raising=False)
    monkeypatch.delenv("NUKE_MCP_SS_ROOT", raising=False)
    root = distortion._resolve_stmap_cache_root()
    assert root == pathlib.Path.home() / ".nuke_mcp" / "stmaps"


def test_bake_lens_distortion_envelope_forwards_stmap_paths(
    distortion_tools, monkeypatch, tmp_path
):
    """The MCP-side tool resolves the cache root and forwards both
    paths through to the addon as a ``stmap_paths`` dict.
    """
    monkeypatch.setenv("SS", str(tmp_path))
    monkeypatch.delenv("NUKE_MCP_SS_ROOT", raising=False)
    server, _script, tools = distortion_tools
    tools["bake_lens_distortion_envelope"]("plate", "lens_solve")
    cmd, params = server.typed_calls[-1]
    assert cmd == "bake_lens_distortion_envelope"
    paths = params["stmap_paths"]
    assert paths["undistort"] == str(tmp_path / "comp" / "stmaps" / "plate_undistort.exr")
    assert paths["redistort"] == str(tmp_path / "comp" / "stmaps" / "plate_redistort.exr")


# ---------------------------------------------------------------------------
# apply_idistort
# ---------------------------------------------------------------------------


def test_apply_idistort_wires_uv_channels_default(distortion_tools):
    server, _script, tools = distortion_tools
    result = tools["apply_idistort"]("plate", "sv1")
    assert result.get("status") != "error", result
    assert result["type"] == "IDistort"
    assert result["inputs"][:2] == ["plate", "sv1"]
    assert result["u_channel"] == "forward.u"
    assert result["v_channel"] == "forward.v"
    cmd, params = server.typed_calls[-1]
    assert cmd == "apply_idistort"
    assert params["u_channel"] == "forward.u"
    assert params["v_channel"] == "forward.v"


def test_apply_idistort_custom_uv_channels(distortion_tools):
    server, _script, tools = distortion_tools
    result = tools["apply_idistort"]("plate", "sv1", uv_channels=("backward.u", "backward.v"))
    assert result["u_channel"] == "backward.u"
    assert result["v_channel"] == "backward.v"
    _cmd, params = server.typed_calls[-1]
    assert params["u_channel"] == "backward.u"
    assert params["v_channel"] == "backward.v"


def test_apply_idistort_idempotent_on_name(distortion_tools):
    server, _script, tools = distortion_tools
    first = tools["apply_idistort"]("plate", "sv1", name="myIDistort")
    second = tools["apply_idistort"]("plate", "sv1", name="myIDistort")
    assert first["name"] == second["name"] == "myIDistort"
    idistorts = [n for n, d in server.nodes.items() if d["type"] == "IDistort"]
    assert len(idistorts) == 1


# ---------------------------------------------------------------------------
# apply_smartvector_propagate (Task-wrapped)
# ---------------------------------------------------------------------------


def test_apply_smartvector_propagate_returns_task_id(distortion_tools_with_taskstore):
    server, _script, tools = distortion_tools_with_taskstore
    result = tools["apply_smartvector_propagate"](
        plate="plate", paint_frame=1010, range_in=1001, range_out=1020
    )
    assert "task_id" in result
    assert result["state"] == "working"
    assert len(server.async_smartvectors) == 1
    payload = server.async_smartvectors[0]
    assert payload["task_id"] == result["task_id"]
    assert payload["plate"] == "plate"
    assert payload["paint_frame"] == 1010
    assert payload["range_in"] == 1001
    assert payload["range_out"] == 1020
    # On-disk Task record exists in working state.
    store = task_store.default_store()
    task = store.get(result["task_id"])
    assert task is not None
    assert task.state == "working"
    assert task.tool == "apply_smartvector_propagate"


def test_apply_smartvector_propagate_progress_listener_drives_store(
    distortion_tools_with_taskstore,
):
    """Inject a working-then-completion sequence via the notification
    queue and assert the TaskStore record reflects the terminal state.
    """
    _server, _script, tools = distortion_tools_with_taskstore
    out = tools["apply_smartvector_propagate"](
        plate="plate", paint_frame=1, range_in=1, range_out=5
    )
    task_id = out["task_id"]
    queue = connection.notification_queue()
    queue.put({"type": "task_progress", "id": task_id, "state": "working", "frame": 2, "total": 5})
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "completed",
            "frame": 5,
            "total": 5,
            "result": {"smartvector": "SmartVector_plate", "frames": [1, 5]},
        }
    )
    final = task_store.default_store().get(task_id)
    assert final is not None
    assert final.state == "completed"
    assert final.result == {"smartvector": "SmartVector_plate", "frames": [1, 5]}


# ---------------------------------------------------------------------------
# generate_stmap (Task-wrapped + idempotent on name)
# ---------------------------------------------------------------------------


def test_generate_stmap_returns_task_id(distortion_tools_with_taskstore):
    server, _script, tools = distortion_tools_with_taskstore
    result = tools["generate_stmap"]("lens_solve", mode="undistort")
    assert "task_id" in result
    assert result["state"] == "working"
    assert len(server.async_stmaps) == 1
    payload = server.async_stmaps[0]
    assert payload["lens_distortion_node"] == "lens_solve"
    assert payload["mode"] == "undistort"
    store = task_store.default_store()
    task = store.get(result["task_id"])
    assert task is not None
    assert task.tool == "generate_stmap"


def test_generate_stmap_idempotent_on_name(distortion_tools_with_taskstore):
    """Re-calling with the same ``name`` while the original task is
    still working surfaces the same task_id without firing a second
    addon dispatch.
    """
    server, _script, tools = distortion_tools_with_taskstore
    first = tools["generate_stmap"]("lens_solve", mode="undistort", name="ldn1_und")
    second = tools["generate_stmap"]("lens_solve", mode="undistort", name="ldn1_und")
    assert first["task_id"] == second["task_id"]
    # Only one addon dispatch -- the second call short-circuited.
    assert len(server.async_stmaps) == 1
    # The reused-second-call response carries the reuse marker.
    assert second["ack"] == {"reused": True}


def test_generate_stmap_redistort_mode(distortion_tools_with_taskstore):
    server, _script, tools = distortion_tools_with_taskstore
    tools["generate_stmap"]("lens_solve", mode="redistort")
    payload = server.async_stmaps[-1]
    assert payload["mode"] == "redistort"


def test_distortion_tasks_cancel_dispatches_to_addon(distortion_tools_with_taskstore):
    server, _script, tools = distortion_tools_with_taskstore
    out = tools["apply_smartvector_propagate"](
        plate="plate", paint_frame=1, range_in=1, range_out=2
    )

    from nuke_mcp.tools import tasks as tasks_tools

    class _Mcp:
        def __init__(self) -> None:
            self.registered = {}

        def tool(self, **_kwargs):
            def _decorator(fn):
                self.registered[fn.__name__] = fn
                return fn

            return _decorator

    class _Ctx:
        def __init__(self) -> None:
            self.mcp = _Mcp()

    ctx = _Ctx()
    tasks_tools.register(ctx)
    cancelled = ctx.mcp.registered["tasks_cancel"](out["task_id"])

    assert cancelled["state"] == "cancelled"
    assert out["task_id"] in server.cancelled_renders

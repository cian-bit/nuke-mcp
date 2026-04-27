"""Tests for render.py setup_write / render_frames / setup_precomp / list_precomps.

A3 migrated ``setup_write`` to a typed addon handler with file_type +
path-traversal allowlists. ``setup_precomp`` and ``list_precomps`` are
out of A3 scope and still ship f-string ``execute_python`` payloads.

B2 turns ``render_frames`` into an MCP Task by default. Existing
synchronous-path tests use ``synchronous=True`` for back-compat; new
tests cover the async dispatch + task_progress listener.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp import connection
from nuke_mcp import tasks as task_store
from nuke_mcp.tools import render


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
def render_tools(mock_script):
    server, script = mock_script
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    ctx = _StubCtx()
    render.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# setup_write -- A3 typed dispatch
# ---------------------------------------------------------------------------


def test_setup_write_happy_path(render_tools):
    server, _script, tools = render_tools
    # Relative path: skips the absolute-path allow-list check entirely
    # so the happy-path doesn't depend on $HOME / $SS contents.
    result = tools["setup_write"]("plate", "out/plate.####.exr")
    assert result.get("status") != "error", result
    assert server.executed_code == []
    assert len(server.typed_calls) == 1
    cmd, params = server.typed_calls[0]
    assert cmd == "setup_write"
    assert params == {
        "input_node": "plate",
        "path": "out/plate.####.exr",
        "file_type": "exr",
        "colorspace": "scene_linear",
    }


def test_setup_write_path_traversal_rejected(render_tools):
    """A3 closes the f-string hazard: ``..`` in a path raises ValueError addon-side."""
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", "../../../etc/passwd")
    assert result.get("status") == "error"
    assert "path traversal" in result["error"].lower() or "invalid path" in result["error"].lower()


def test_setup_write_invalid_file_type_rejected(render_tools):
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", "out.bad", file_type="badtype")
    assert result.get("status") == "error"
    assert "invalid" in result["error"].lower()


def test_setup_write_alternate_format(render_tools):
    server, _script, tools = render_tools
    tools["setup_write"]("plate", "out.png", file_type="png", colorspace="sRGB")
    _cmd, params = server.typed_calls[0]
    assert params["file_type"] == "png"
    assert params["colorspace"] == "sRGB"


# ---------------------------------------------------------------------------
# GPT-5.5 finding #7: setup_write absolute / UNC / device-name policy
# ---------------------------------------------------------------------------


def test_setup_write_blocks_windows_system_path(render_tools):
    """Absolute path to C:\\Windows must be rejected (not in allow-list)."""
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", r"C:\Windows\System32\drivers\etc\hosts")
    assert result.get("status") == "error"
    assert "PathPolicyViolation" in result.get("error_class", "")


def test_setup_write_blocks_unc_path(render_tools):
    """UNC paths bypass the local allow-list entirely. Must be blocked."""
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", r"\\evil-server\share\evil.exr")
    assert result.get("status") == "error"
    assert "UNC" in result.get("error", "") or result.get("error_class") == "PathPolicyViolation"


def test_setup_write_blocks_unc_forward_slash(render_tools):
    """Forward-slash UNC form is also blocked."""
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", "//evil-server/share/evil.exr")
    assert result.get("status") == "error"


def test_setup_write_blocks_etc_passwd(render_tools):
    """Linux-style absolute paths outside the allow-list are blocked."""
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", "/etc/passwd")
    assert result.get("status") == "error"
    assert "PathPolicyViolation" in result.get("error_class", "") or "absolute" in result.get(
        "error", ""
    )


def test_setup_write_blocks_windows_reserved_device(render_tools):
    """``CON``, ``PRN``, ``NUL`` etc. as the basename are rejected."""
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", "out/CON.exr")
    assert result.get("status") == "error"
    assert "reserved" in result.get("error", "").lower() or "PathPolicyViolation" in result.get(
        "error_class", ""
    )


def test_setup_write_blocks_nul_device(render_tools):
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", "renders/NUL")
    assert result.get("status") == "error"


def test_setup_write_allows_path_under_home(render_tools, tmp_path, monkeypatch):
    """Absolute path under $HOME passes (default allow-listed root)."""
    import os

    monkeypatch.setenv("HOME", str(tmp_path))  # POSIX
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    monkeypatch.delenv("NUKE_MCP_WRITE_ROOTS", raising=False)
    monkeypatch.delenv("SS", raising=False)
    target = os.path.join(str(tmp_path), "renders", "out.####.exr")
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", target)
    assert result.get("status") != "error", result


def test_setup_write_allows_path_under_ss_when_set(render_tools, tmp_path, monkeypatch):
    """``$SS`` sandbox is allow-listed by default when set."""
    monkeypatch.setenv("SS", str(tmp_path))
    monkeypatch.delenv("NUKE_MCP_WRITE_ROOTS", raising=False)
    target = str(tmp_path / "renders" / "out.exr")
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", target)
    assert result.get("status") != "error", result


def test_setup_write_allows_path_under_explicit_override(render_tools, tmp_path, monkeypatch):
    """``NUKE_MCP_WRITE_ROOTS`` overrides defaults entirely."""
    monkeypatch.setenv("NUKE_MCP_WRITE_ROOTS", str(tmp_path))
    monkeypatch.delenv("SS", raising=False)
    target = str(tmp_path / "out.exr")
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", target)
    assert result.get("status") != "error", result


def test_setup_write_blocks_outside_explicit_roots(render_tools, tmp_path, monkeypatch):
    """A path outside the explicit allow-list is rejected."""
    import os

    monkeypatch.setenv("NUKE_MCP_WRITE_ROOTS", str(tmp_path))
    monkeypatch.delenv("SS", raising=False)
    # Pick an absolute path that is NOT under tmp_path.
    other_root = os.path.dirname(str(tmp_path))
    other = os.path.join(other_root, "outside", "evil.exr")
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", other)
    assert result.get("status") == "error"


def test_setup_write_relative_path_still_allowed(render_tools, monkeypatch):
    """Relative paths skip the absolute-allow-list check unconditionally."""
    monkeypatch.delenv("NUKE_MCP_WRITE_ROOTS", raising=False)
    monkeypatch.delenv("SS", raising=False)
    _server, _script, tools = render_tools
    result = tools["setup_write"]("plate", "renders/out.####.exr")
    assert result.get("status") != "error", result


def test_setup_write_rejects_non_string_path(render_tools):
    _server, _script, tools = render_tools
    # Pass an int -- the type-validate path takes the policy violation route.
    result = tools["setup_write"]("plate", 12345)  # type: ignore[arg-type]
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# render_frames -- destructive, gated on confirm
# ---------------------------------------------------------------------------


def test_render_frames_preview_without_confirm(render_tools):
    server, _script, tools = render_tools
    result = tools["render_frames"](write_node="MainWrite", first_frame=1001, last_frame=1010)
    assert "preview" in result
    assert "MainWrite" in result["preview"]
    assert "1001-1010" in result["preview"]
    # no execute_python / no render call without confirm
    assert server.executed_code == []


def test_render_frames_with_confirm_calls_render(render_tools):
    """Back-compat: ``synchronous=True`` keeps the pre-B2 wire shape."""
    server, _script, tools = render_tools
    result = tools["render_frames"](
        write_node="MainWrite",
        first_frame=1001,
        last_frame=1005,
        confirm=True,
        synchronous=True,
    )
    assert result.get("rendered") == "Write1"
    assert result.get("frames") == [1001, 1100]


def test_render_frames_no_args_preview_message(render_tools):
    """Calling with neither node nor frames produces a generic preview msg."""
    _server, _script, tools = render_tools
    result = tools["render_frames"]()
    assert "preview" in result
    assert "will render" in result["preview"]


# ---------------------------------------------------------------------------
# setup_precomp
# ---------------------------------------------------------------------------


def test_setup_precomp_basic(render_tools):
    server, _script, tools = render_tools
    tools["setup_precomp"]("plate")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when render.py migrates to typed handlers.
    assert "'plate'" in code
    assert "Write" in code
    assert "Read" in code


def test_setup_precomp_explicit_path(render_tools):
    server, _script, tools = render_tools
    tools["setup_precomp"]("plate", path="/tmp/precomp.####.exr")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when render.py migrates to typed handlers.
    assert "/tmp/precomp.####.exr" in code


def test_setup_precomp_with_name(render_tools):
    server, _script, tools = render_tools
    tools["setup_precomp"]("plate", name="bg_precomp")
    code = server.executed_code[0]
    # # A3: rewrite this assertion when render.py migrates to typed handlers.
    assert "bg_precomp" in code


# ---------------------------------------------------------------------------
# list_precomps
# ---------------------------------------------------------------------------


def test_list_precomps_invokes_python(render_tools):
    server, _script, tools = render_tools
    tools["list_precomps"]()
    code = server.executed_code[0]
    # # A3: rewrite this assertion when render.py migrates to typed handlers.
    assert "allNodes" in code
    assert "Write" in code
    assert "Read" in code


def test_list_precomps_no_inputs(render_tools):
    """list_precomps takes no arguments. Confirm shape."""
    _server, _script, tools = render_tools
    result = tools["list_precomps"]()
    # mock _execute_python returns {} -- the wrapper just relays it
    assert isinstance(result, dict)


def test_list_precomps_disconnected(render_tools):
    _server, _script, tools = render_tools
    connection.disconnect()
    result = tools["list_precomps"]()
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# B2: render_frames as a Task (async by default)
# ---------------------------------------------------------------------------


@pytest.fixture
def render_tools_with_taskstore(render_tools, monkeypatch, tmp_path):
    """``render_tools`` plus a clean per-test TaskStore.

    Pinning ``NUKE_MCP_TASK_DIR`` per test keeps the suite isolated
    from any real ``~/.nuke_mcp/tasks`` dir and from sibling tests.
    """
    monkeypatch.setenv("NUKE_MCP_TASK_DIR", str(tmp_path))
    task_store.reset_default_store()
    yield render_tools
    task_store.reset_default_store()


def test_render_frames_async_returns_task_id(render_tools_with_taskstore):
    server, _script, tools = render_tools_with_taskstore
    result = tools["render_frames"](
        write_node="MainWrite",
        first_frame=1001,
        last_frame=1005,
        confirm=True,
    )
    assert "task_id" in result
    assert result["state"] == "working"
    # The mock recorded the render_async call with the task_id forwarded.
    assert len(server.async_renders) == 1
    payload = server.async_renders[0]
    assert payload["task_id"] == result["task_id"]
    assert payload["frame_range"] == [1001, 1005]
    assert payload["write_node"] == "MainWrite"
    # The Task record exists on disk in the working state.
    store = task_store.default_store()
    task = store.get(result["task_id"])
    assert task is not None
    assert task.state == "working"
    assert task.tool == "render_frames"


def test_render_frames_async_progress_listener_updates_store(render_tools_with_taskstore):
    server, _script, tools = render_tools_with_taskstore
    out = tools["render_frames"](confirm=True)
    task_id = out["task_id"]
    queue = connection.notification_queue()

    # Inject a progress-then-completion sequence as if the addon worker emitted them.
    queue.put({"type": "task_progress", "id": task_id, "state": "working", "frame": 1, "total": 5})
    queue.put({"type": "task_progress", "id": task_id, "state": "working", "frame": 3, "total": 5})
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "completed",
            "frame": 5,
            "total": 5,
            "result": {"rendered": "Write1", "frames": [1, 5]},
        }
    )

    store = task_store.default_store()
    final = store.get(task_id)
    assert final is not None
    assert final.state == "completed"
    assert final.result == {"rendered": "Write1", "frames": [1, 5]}


def test_render_frames_async_failure_path(render_tools_with_taskstore):
    server, _script, tools = render_tools_with_taskstore
    out = tools["render_frames"](confirm=True)
    task_id = out["task_id"]
    queue = connection.notification_queue()
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "failed",
            "error": {"error_class": "RuntimeError", "message": "boom"},
        }
    )
    store = task_store.default_store()
    task = store.get(task_id)
    assert task is not None
    assert task.state == "failed"
    assert task.error == {"error_class": "RuntimeError", "message": "boom"}


def test_render_frames_async_late_progress_does_not_revive_terminal(render_tools_with_taskstore):
    """A stragglers ``working`` notification after ``completed`` must
    NOT downgrade the task state -- the listener bails when the disk
    record is already terminal.
    """
    server, _script, tools = render_tools_with_taskstore
    out = tools["render_frames"](confirm=True)
    task_id = out["task_id"]
    queue = connection.notification_queue()
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "completed",
            "result": {"rendered": "Write1", "frames": [1, 5]},
        }
    )
    queue.put(
        {"type": "task_progress", "id": task_id, "state": "working", "frame": 99, "total": 100}
    )
    store = task_store.default_store()
    task = store.get(task_id)
    assert task is not None
    assert task.state == "completed"


def test_tasks_cancel_signals_addon_for_render_task(render_tools_with_taskstore):
    server, _script, tools = render_tools_with_taskstore
    out = tools["render_frames"](confirm=True)
    task_id = out["task_id"]

    # Register the tasks_* tools on a separate stub so we can call them.
    from nuke_mcp.tools import tasks as tasks_tools

    ctx = _StubCtx()
    tasks_tools.register(ctx)
    cancelled = ctx.mcp.registered["tasks_cancel"](task_id)

    assert cancelled["state"] == "cancelled"
    # The addon-side cancel_render must have been dispatched.
    assert task_id in server.cancelled_renders


def test_render_frames_synchronous_wire_compat(render_tools_with_taskstore):
    """Back-compat: ``synchronous=True`` MUST return the pre-B2 shape
    (``rendered`` + ``frames``) so existing client code keeps working.
    """
    _server, _script, tools = render_tools_with_taskstore
    out = tools["render_frames"](
        write_node="MainWrite",
        first_frame=1001,
        last_frame=1005,
        confirm=True,
        synchronous=True,
    )
    # The RenderResult model fold-in adds ``frames_written`` /
    # ``output_path`` but ``extra="allow"`` keeps the original keys.
    assert out.get("rendered") == "Write1" or out.get("output_path") == "Write1"
    assert "task_id" not in out

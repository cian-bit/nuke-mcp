"""Tests for the ``tasks_*`` MCP tools (B2 commit 2).

Stubs the FastMCP registration via the same ``_StubMCP`` pattern the
other tool tests use. Each test pins ``NUKE_MCP_TASK_DIR`` to a
``tmp_path`` so the real ``~/.nuke_mcp/tasks`` is never touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp import tasks as task_store
from nuke_mcp.tools import tasks as tasks_tools


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
def tools(monkeypatch, tmp_path):
    monkeypatch.setenv("NUKE_MCP_TASK_DIR", str(tmp_path))
    task_store.reset_default_store()
    ctx = _StubCtx()
    tasks_tools.register(ctx)
    yield ctx.mcp.registered
    task_store.reset_default_store()


# ---------------------------------------------------------------------------
# tasks_list
# ---------------------------------------------------------------------------


def test_tasks_list_empty(tools):
    out = tools["tasks_list"]()
    assert out == {"tasks": [], "count": 0}


def test_tasks_list_returns_newest_first(tools):
    store = task_store.default_store()
    a = store.create(tool="render_frames", params={"i": 1}, request_id="r1")
    b = store.create(tool="render_frames", params={"i": 2}, request_id="r2")
    out = tools["tasks_list"]()
    assert out["count"] == 2
    ids = [t["id"] for t in out["tasks"]]
    # newest first -- b created after a
    assert ids == [b.id, a.id]


def test_tasks_list_respects_limit(tools):
    store = task_store.default_store()
    for i in range(5):
        store.create(tool="render_frames", params={"i": i}, request_id=f"r{i}")
    out = tools["tasks_list"](limit=2)
    assert out["count"] == 2
    assert len(out["tasks"]) == 2


# ---------------------------------------------------------------------------
# tasks_get
# ---------------------------------------------------------------------------


def test_tasks_get_returns_full_record(tools):
    store = task_store.default_store()
    task = store.create(tool="render_frames", params={"frame": 1001}, request_id="rid")
    out = tools["tasks_get"](task.id)
    assert out["id"] == task.id
    assert out["tool"] == "render_frames"
    assert out["state"] == "working"
    assert out["params"] == {"frame": 1001}
    assert out["request_id"] == "rid"


def test_tasks_get_missing_returns_error(tools):
    out = tools["tasks_get"]("notarealtaskid00")
    assert out["status"] == "error"
    assert out["error_class"] == "TaskNotFound"


# ---------------------------------------------------------------------------
# tasks_cancel
# ---------------------------------------------------------------------------


def test_tasks_cancel_transitions_state(tools):
    store = task_store.default_store()
    task = store.create(tool="render_frames", params={}, request_id="rid")
    out = tools["tasks_cancel"](task.id)
    assert out["id"] == task.id
    assert out["state"] == "cancelled"
    # disk-side: cancellation persisted
    assert store.get(task.id).state == "cancelled"  # type: ignore[union-attr]


def test_tasks_cancel_terminal_is_noop(tools):
    """Cancelling an already-completed task returns its existing state, not an error."""
    store = task_store.default_store()
    task = store.create(tool="render_frames", params={}, request_id="rid")
    store.update(task.id, state="completed", result={"frames": [1, 2]})
    out = tools["tasks_cancel"](task.id)
    assert out["state"] == "completed"


def test_tasks_cancel_missing_returns_error(tools):
    out = tools["tasks_cancel"]("doesnotexist0000")
    assert out["status"] == "error"
    assert out["error_class"] == "TaskNotFound"


# ---------------------------------------------------------------------------
# tasks_resume (stub)
# ---------------------------------------------------------------------------


def test_tasks_resume_stub_response(tools):
    store = task_store.default_store()
    task = store.create(tool="render_frames", params={}, request_id="rid")
    out = tools["tasks_resume"](task.id)
    assert out["status"] == "not_yet_implemented"
    assert out["task_id"] == task.id
    assert out["current_state"] == "working"


def test_tasks_resume_missing_returns_error(tools):
    out = tools["tasks_resume"]("doesnotexist0000")
    assert out["status"] == "error"
    assert out["error_class"] == "TaskNotFound"

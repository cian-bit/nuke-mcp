"""MCP tools that expose the Task store to clients.

Phase B2 commit 2. Four tools that read/manage the disk-persisted task
records introduced in commit 1. ``tasks_list`` / ``tasks_get`` are
read-only inspections, ``tasks_cancel`` flips a working task to the
``cancelled`` state, and ``tasks_resume`` is a stub until commit 4
turns ``render_frames`` into a Task.

All four route through the default ``TaskStore`` singleton -- tests
swap the singleton via ``tasks.reset_default_store()`` + the
``NUKE_MCP_TASK_DIR`` env var rather than monkeypatching here.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from nuke_mcp import connection
from nuke_mcp import tasks as task_store
from nuke_mcp.annotations import DESTRUCTIVE, READ_ONLY

if False:
    from nuke_mcp.server import ServerContext

log = logging.getLogger(__name__)


def _serialize(task: task_store.Task) -> dict[str, Any]:
    """Project a Task to the wire dict the MCP client sees.

    Returns ``mode="json"`` so timestamps render as floats and the
    enum-like state stays a string. ``exclude_none`` drops the
    placeholder ``result`` / ``error`` fields when the task hasn't
    finished yet -- keeps the wire snapshot tidy.
    """
    return task.model_dump(mode="json", exclude_none=True)


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(annotations=READ_ONLY, output_schema=None)
    def tasks_list(limit: int = 50) -> dict:
        """List recent tasks, newest first.

        Args:
            limit: max number of records to return. Defaults to 50.
                The store is bounded only by the purge schedule, so
                without a cap a busy session could leak hundreds of
                completed records into a single response.
        """
        started = time.perf_counter()
        store = task_store.default_store()
        try:
            recent = store.list()[: max(0, int(limit))]
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            log.error("tasks_list: %s", exc)
            return {
                "status": "error",
                "error": str(exc),
                "error_class": type(exc).__name__,
                "duration_ms": duration_ms,
            }
        return {
            "tasks": [_serialize(t) for t in recent],
            "count": len(recent),
        }

    @ctx.mcp.tool(annotations=READ_ONLY, output_schema=None)
    def tasks_get(id: str) -> dict:  # noqa: A002 - public API name
        """Fetch a single task by id.

        Args:
            id: 16-hex-char task id from a prior tool call. Returns a
                structured error if the id is unknown rather than
                raising -- keeps the MCP envelope contract.
        """
        store = task_store.default_store()
        task = store.get(id)
        if task is None:
            return {
                "status": "error",
                "error": f"task not found: {id}",
                "error_class": "TaskNotFound",
            }
        return _serialize(task)

    @ctx.mcp.tool(annotations=DESTRUCTIVE, output_schema=None)
    def tasks_cancel(id: str) -> dict:  # noqa: A002 - public API name
        """Cancel an in-flight task.

        Sets ``state="cancelled"`` on the disk record AND signals the
        addon-side worker (for renders) so it stops between frames.
        Cancelling an already-terminal task is a no-op -- the response
        carries the existing state unchanged.

        Args:
            id: 16-hex-char task id.
        """
        store = task_store.default_store()
        try:
            existing = store.get(id)
            if existing is None:
                return {
                    "status": "error",
                    "error": f"task not found: {id}",
                    "error_class": "TaskNotFound",
                }
            already_terminal = existing.state in task_store.TERMINAL_STATES
            task = store.cancel(id)
        except KeyError:
            return {
                "status": "error",
                "error": f"task not found: {id}",
                "error_class": "TaskNotFound",
            }

        # Also signal the addon worker for in-flight renders. Send is
        # best-effort: if the addon side already finished or the
        # connection is gone, we still want the disk-side cancellation
        # recorded. This stays below ``tasks_cancel``'s own timeout
        # via the default ``read`` class.
        if not already_terminal and task.tool == "render_frames":
            with contextlib.suppress(Exception):
                connection.send("cancel_render", task_id=id)
            # Drop any registered notification listener so a stale
            # progress line can't flip the state back to working.
            with contextlib.suppress(Exception):
                connection.notification_queue().unregister_listener(id)

        return _serialize(task)

    @ctx.mcp.tool(annotations=READ_ONLY, output_schema=None)
    def tasks_resume(id: str) -> dict:  # noqa: A002 - public API name
        """Resume a task that's awaiting input.

        TODO(B2c): wired up once a tool actually parks in
        ``input_required``. Until commit 4 ships an async render and a
        future commit adds elicitation, every Task either succeeds or
        fails synchronously, so resume has nothing to drive. Returns a
        stub status so clients can probe capability without crashing.

        Args:
            id: 16-hex-char task id.
        """
        store = task_store.default_store()
        task = store.get(id)
        if task is None:
            return {
                "status": "error",
                "error": f"task not found: {id}",
                "error_class": "TaskNotFound",
            }
        return {
            "status": "not_yet_implemented",
            "task_id": task.id,
            "current_state": task.state,
        }

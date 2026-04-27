"""MCP 2025-11-25 Tasks primitive: disk-persisted task store.

Phase B2 commit 1. Tasks model long-running operations (render, copycat
training) so they survive reconnects and can be cancelled mid-flight.
The store is intentionally process-local: every MCP server instance
owns its own ``~/.nuke_mcp/tasks/`` directory, and a Task record's
authoritative state is its on-disk JSON file. The in-memory cache is
just a write-through accelerator.

Atomic writes use the temp-file + ``os.replace`` pattern: a
``<id>.json.tmp`` is written full, fsync'd, then renamed over the live
file. Any reader either sees the previous version or the new one --
never a torn write -- because ``os.replace`` is atomic on both POSIX
and Windows for same-directory renames.

State machine, lifted from the MCP spec:
  * ``working`` -- created and progressing.
  * ``input_required`` -- awaiting user input (B2c+ uses this).
  * ``completed`` -- finished, result attached.
  * ``failed`` -- finished with an error envelope attached.
  * ``cancelled`` -- explicit ``tasks_cancel`` call (or session-lost
    sweep on reconnect).
"""

from __future__ import annotations

import builtins
import contextlib
import json
import logging
import os
import pathlib
import tempfile
import threading
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

TaskState = Literal["working", "input_required", "completed", "failed", "cancelled"]

TERMINAL_STATES: frozenset[TaskState] = frozenset({"completed", "failed", "cancelled"})


class Task(BaseModel):
    """Persistent record of an in-flight or finished tool call.

    The wire shape matches the disk shape exactly -- ``model_dump`` is
    fed straight to ``json.dumps``. ``extra="allow"`` lets future
    additions (per-tool progress fields beyond the free-form
    ``progress`` dict) round-trip without a model bump.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    tool: str
    state: TaskState
    created_at: float
    updated_at: float
    progress: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    request_id: str


def _default_task_dir() -> pathlib.Path:
    """Resolve ``~/.nuke_mcp/tasks`` honouring ``NUKE_MCP_TASK_DIR``.

    The override lets tests pin the store to a per-test ``tmp_path`` so
    they never collide and don't leave files in the real ``~``. The env
    var is consulted on every call (not cached) so a test can patch it
    after import-time.
    """
    override = os.environ.get("NUKE_MCP_TASK_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".nuke_mcp" / "tasks"


class TaskStore:
    """Disk-backed task registry. Thread-safe via a single coarse lock.

    The lock is intentionally coarse rather than per-task: tasks rarely
    contend (one render at a time in current tools) and the round-trip
    cost of a finer-grained map is not worth the win.
    """

    def __init__(self, base_dir: pathlib.Path | None = None) -> None:
        # Resolve lazily so a single ``TaskStore()`` shared across the
        # process picks up env-var overrides set in test fixtures.
        self._base_dir_override = base_dir
        self._lock = threading.Lock()

    @property
    def base_dir(self) -> pathlib.Path:
        if self._base_dir_override is not None:
            return self._base_dir_override
        return _default_task_dir()

    def _ensure_dir(self) -> pathlib.Path:
        d = self.base_dir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, task_id: str) -> pathlib.Path:
        return self.base_dir / f"{task_id}.json"

    def _atomic_write(self, path: pathlib.Path, payload: dict[str, Any]) -> None:
        """Write ``payload`` to ``path`` atomically.

        ``tempfile.NamedTemporaryFile`` lands the temp file in the same
        directory as the target so ``os.replace`` is a same-fs rename.
        ``delete=False`` because we hand off the path to ``replace``;
        the cleanup happens via the rename, not the context manager.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        # ``json.dumps`` first so a serialization error doesn't leave
        # the temp file around. Sort keys for stable diffs and easier
        # debugging when comparing two on-disk records.
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))

        # delete=False: we manage the lifecycle via os.replace. Naming
        # the temp file with the same stem lets ``ls`` group it with
        # the live file during a debug session.
        fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                with contextlib.suppress(OSError):
                    os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    # -- public API --

    def create(self, tool: str, params: dict[str, Any], request_id: str) -> Task:
        """Allocate a new Task in ``working`` state and persist it.

        ``id`` is ``uuid4().hex[:16]`` for symmetry with
        ``connection.send``'s request ids. 64 bits is collision-safe at
        any plausible session scale.
        """
        now = time.time()
        task = Task(
            id=uuid.uuid4().hex[:16],
            tool=tool,
            state="working",
            created_at=now,
            updated_at=now,
            progress={},
            result=None,
            error=None,
            params=dict(params),
            request_id=request_id,
        )
        with self._lock:
            self._ensure_dir()
            self._atomic_write(self._path(task.id), task.model_dump(mode="json"))
        log.info("task created: id=%s tool=%s rid=%s", task.id, tool, request_id)
        return task

    def get(self, task_id: str) -> Task | None:
        """Read a task off disk. Returns ``None`` for missing or corrupt files.

        Corrupt files are logged but not deleted -- the operator may
        want to inspect them. A future GC pass can prune them.
        """
        path = self._path(task_id)
        try:
            body = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("task read failed: id=%s err=%s", task_id, exc)
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            log.warning("task corrupt: id=%s err=%s", task_id, exc)
            return None
        try:
            return Task.model_validate(data)
        except Exception as exc:  # pydantic.ValidationError is subclass of ValueError
            log.warning("task invalid: id=%s err=%s", task_id, exc)
            return None

    def update(self, task_id: str, **fields: Any) -> Task:
        """Patch a task's mutable fields and re-persist.

        ``id`` / ``tool`` / ``created_at`` / ``request_id`` are
        intentionally not patchable -- they're identity. ``updated_at``
        is bumped automatically. Raises ``KeyError`` if no such task.
        """
        with self._lock:
            existing = self.get(task_id)
            if existing is None:
                raise KeyError(f"task not found: {task_id}")
            data = existing.model_dump(mode="json")
            for key, value in fields.items():
                if key in {"id", "tool", "created_at", "request_id"}:
                    # Silently drop instead of raising: callers often
                    # splat the original Task back in and we don't want
                    # update() to refuse on identity-field round-trip.
                    continue
                data[key] = value
            data["updated_at"] = time.time()
            updated = Task.model_validate(data)
            self._atomic_write(self._path(task_id), updated.model_dump(mode="json"))
        return updated

    def list(self) -> list[Task]:
        """Return every task on disk, sorted newest-first by ``updated_at``.

        Skips files that fail to parse rather than aborting the whole
        listing -- a single corrupt task shouldn't make the rest
        invisible.
        """
        d = self.base_dir
        if not d.exists():
            return []
        out: list[Task] = []
        for entry in d.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            stem = entry.stem
            task = self.get(stem)
            if task is not None:
                out.append(task)
        out.sort(key=lambda t: t.updated_at, reverse=True)
        return out

    def cancel(self, task_id: str) -> Task:
        """Mark a working / input_required task as cancelled.

        Already-terminal tasks return their existing state unchanged:
        cancelling a completed task is a no-op, not an error. The
        addon-side worker thread is signalled separately via the stop
        event registered against the task id.
        """
        existing = self.get(task_id)
        if existing is None:
            raise KeyError(f"task not found: {task_id}")
        if existing.state in TERMINAL_STATES:
            return existing
        return self.update(task_id, state="cancelled")

    def sweep_stale_working(self, max_age_seconds: float = 600.0) -> builtins.list[Task]:
        """Mark working/input_required tasks older than ``max_age_seconds``
        as failed with a ``SessionLost`` error. Returns the affected tasks.

        Used on reconnect: a task left in ``working`` from before the
        MCP server died can never finish (the addon-side worker is
        gone), so flipping it to ``failed`` keeps ``tasks_list`` from
        showing zombie records. The 10-minute default mirrors the
        crash-marker freshness window in ``connection.py``.
        """
        now = time.time()
        flipped: list[Task] = []
        for task in self.list():
            if task.state not in {"working", "input_required"}:
                continue
            if now - task.updated_at < max_age_seconds:
                continue
            try:
                updated = self.update(
                    task.id,
                    state="failed",
                    error={
                        "error_class": "SessionLost",
                        "message": (
                            f"task in {task.state} state for "
                            f"{int(now - task.updated_at)}s -- session lost before completion"
                        ),
                    },
                )
                flipped.append(updated)
            except KeyError:
                # Race: someone else deleted the file between list and update.
                continue
        return flipped

    def purge_completed_older_than(self, seconds: float = 86400.0) -> int:
        """Delete terminal-state tasks whose ``updated_at`` is older than
        ``seconds`` ago. Returns the count of removed records.

        Default is 24 hours. Working tasks are never purged regardless
        of age -- if one's been ``working`` for a day, the operator
        should investigate, not have the record silently disappear.
        """
        cutoff = time.time() - seconds
        removed = 0
        for task in self.list():
            if task.state not in TERMINAL_STATES:
                continue
            if task.updated_at >= cutoff:
                continue
            with contextlib.suppress(OSError):
                self._path(task.id).unlink()
                removed += 1
        return removed


# Singleton used by the rest of the server. Tests construct fresh
# instances pinned to a tmp directory and don't touch this one.
_default_store: TaskStore | None = None


def default_store() -> TaskStore:
    global _default_store
    if _default_store is None:
        _default_store = TaskStore()
    return _default_store


def reset_default_store() -> None:
    """Drop the cached singleton. Used by tests that need a fresh dir."""
    global _default_store
    _default_store = None

"""Tests for ``nuke_mcp.tasks`` -- the disk-persisted Task store.

Phase B2 commit 1. Every test pins ``NUKE_MCP_TASK_DIR`` to a per-test
``tmp_path`` so the real ``~/.nuke_mcp/tasks`` is never touched.
"""

from __future__ import annotations

import json
import time

import pytest

from nuke_mcp import tasks


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Fresh TaskStore rooted at a temp dir, default-singleton reset."""
    monkeypatch.setenv("NUKE_MCP_TASK_DIR", str(tmp_path))
    tasks.reset_default_store()
    yield tasks.TaskStore()
    tasks.reset_default_store()


def test_create_persists_to_disk(store, tmp_path):
    task = store.create(tool="render_frames", params={"frame_range": [1, 5]}, request_id="abc123")
    assert task.id and len(task.id) == 16
    assert task.tool == "render_frames"
    assert task.state == "working"
    assert task.params == {"frame_range": [1, 5]}
    path = tmp_path / f"{task.id}.json"
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["id"] == task.id
    assert on_disk["state"] == "working"
    assert on_disk["request_id"] == "abc123"


def test_get_round_trips_full_record(store):
    created = store.create(tool="copycat_train", params={"epochs": 100}, request_id="rid000")
    fetched = store.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.tool == "copycat_train"
    assert fetched.params == {"epochs": 100}
    assert fetched.state == "working"


def test_get_returns_none_for_missing_id(store):
    assert store.get("deadbeefdeadbeef") is None


def test_get_returns_none_for_corrupt_file(store, tmp_path):
    # Manually write a malformed task file and confirm we don't crash.
    (tmp_path / "deadbeefdeadbeef.json").write_text("{this is not valid", encoding="utf-8")
    assert store.get("deadbeefdeadbeef") is None


def test_update_transitions_state_and_bumps_timestamp(store):
    created = store.create(tool="render_frames", params={}, request_id="rid")
    time.sleep(0.01)  # ensure updated_at strictly increases
    updated = store.update(created.id, state="completed", result={"frames": [1, 2, 3]})
    assert updated.state == "completed"
    assert updated.result == {"frames": [1, 2, 3]}
    assert updated.updated_at > created.updated_at
    # identity fields are not patchable -- this is silently dropped
    sneaky = store.update(created.id, id="new_id_should_be_ignored")
    assert sneaky.id == created.id


def test_update_raises_on_missing(store):
    with pytest.raises(KeyError, match="task not found"):
        store.update("does_not_exist", state="completed")


def test_atomic_write_does_not_leave_temp_files(store, tmp_path):
    """tempfile + os.replace must not leak ``.tmp`` artefacts."""
    task = store.create(tool="render_frames", params={}, request_id="rid")
    store.update(task.id, state="completed")
    # only the live JSON should remain -- no .tmp siblings
    files = list(tmp_path.iterdir())
    suffixes = {f.suffix for f in files}
    assert ".tmp" not in suffixes
    assert files == [tmp_path / f"{task.id}.json"]


def test_cancel_marks_working_task(store):
    task = store.create(tool="render_frames", params={}, request_id="rid")
    cancelled = store.cancel(task.id)
    assert cancelled.state == "cancelled"
    # round-trips through disk
    assert store.get(task.id).state == "cancelled"  # type: ignore[union-attr]


def test_cancel_is_noop_on_terminal_states(store):
    task = store.create(tool="render_frames", params={}, request_id="rid")
    store.update(task.id, state="completed")
    out = store.cancel(task.id)
    # already-terminal: state stays completed, no error raised
    assert out.state == "completed"


def test_cancel_raises_on_missing(store):
    with pytest.raises(KeyError):
        store.cancel("does_not_exist")


def test_list_sorted_newest_first(store):
    a = store.create(tool="render_frames", params={"i": 1}, request_id="r1")
    time.sleep(0.01)
    b = store.create(tool="render_frames", params={"i": 2}, request_id="r2")
    time.sleep(0.01)
    c = store.create(tool="render_frames", params={"i": 3}, request_id="r3")
    listed = store.list()
    assert [t.id for t in listed] == [c.id, b.id, a.id]


def test_list_skips_corrupt_entries(store, tmp_path):
    good = store.create(tool="render_frames", params={}, request_id="r1")
    (tmp_path / "junkjunkjunkjunk.json").write_text("not json", encoding="utf-8")
    listed = store.list()
    assert [t.id for t in listed] == [good.id]


def test_list_empty_when_dir_missing(monkeypatch, tmp_path):
    """A store rooted at a path that doesn't exist must list cleanly, not crash."""
    nonexistent = tmp_path / "no" / "such" / "dir"
    monkeypatch.setenv("NUKE_MCP_TASK_DIR", str(nonexistent))
    fresh = tasks.TaskStore()
    assert fresh.list() == []


def test_purge_removes_old_terminal_tasks(store):
    old_done = store.create(tool="render_frames", params={}, request_id="r1")
    store.update(old_done.id, state="completed")
    # Backdate the file so the purge picks it up.
    store.update(old_done.id, state="completed")
    # Force the timestamp older than cutoff.
    obj = store.get(old_done.id)
    assert obj is not None
    store.update(old_done.id)  # no-op refresh
    # rewrite directly with an old timestamp to simulate a stale record
    backdated = {**obj.model_dump(mode="json"), "updated_at": time.time() - 99999}
    store._atomic_write(store._path(old_done.id), backdated)

    fresh_done = store.create(tool="render_frames", params={}, request_id="r2")
    store.update(fresh_done.id, state="completed")

    working = store.create(tool="render_frames", params={}, request_id="r3")

    removed = store.purge_completed_older_than(seconds=3600.0)
    assert removed == 1
    assert store.get(old_done.id) is None  # purged
    assert store.get(fresh_done.id) is not None  # young, kept
    assert store.get(working.id) is not None  # working state, never purged


def test_default_store_singleton(monkeypatch, tmp_path):
    monkeypatch.setenv("NUKE_MCP_TASK_DIR", str(tmp_path))
    tasks.reset_default_store()
    a = tasks.default_store()
    b = tasks.default_store()
    assert a is b
    tasks.reset_default_store()
    c = tasks.default_store()
    assert c is not a


# ---------------------------------------------------------------------------
# B2 commit 5: stale-working sweep
# ---------------------------------------------------------------------------


def test_sweep_stale_working_flips_old_tasks_to_failed(store):
    """A task stuck in ``working`` past the cutoff must be marked failed."""
    fresh = store.create(tool="render_frames", params={}, request_id="r1")
    stale = store.create(tool="render_frames", params={}, request_id="r2")
    # Backdate the stale one by 11 minutes (cutoff is 10 by default).
    obj = store.get(stale.id)
    assert obj is not None
    backdated = {**obj.model_dump(mode="json"), "updated_at": time.time() - 660}
    store._atomic_write(store._path(stale.id), backdated)

    flipped = store.sweep_stale_working()
    assert len(flipped) == 1
    assert flipped[0].id == stale.id
    assert flipped[0].state == "failed"
    assert flipped[0].error is not None
    assert flipped[0].error.get("error_class") == "SessionLost"

    # Fresh task untouched.
    assert store.get(fresh.id).state == "working"  # type: ignore[union-attr]


def test_sweep_stale_working_skips_terminal_states(store):
    """Completed / failed / cancelled tasks are not affected by the sweep."""
    completed = store.create(tool="render_frames", params={}, request_id="r1")
    store.update(completed.id, state="completed")
    cancelled = store.create(tool="render_frames", params={}, request_id="r2")
    store.update(cancelled.id, state="cancelled")
    # Backdate both well past the cutoff.
    for tid in (completed.id, cancelled.id):
        obj = store.get(tid)
        assert obj is not None
        backdated = {**obj.model_dump(mode="json"), "updated_at": time.time() - 99999}
        store._atomic_write(store._path(tid), backdated)

    flipped = store.sweep_stale_working()
    assert flipped == []
    # Terminal states preserved.
    assert store.get(completed.id).state == "completed"  # type: ignore[union-attr]
    assert store.get(cancelled.id).state == "cancelled"  # type: ignore[union-attr]


def test_sweep_stale_working_ignores_fresh_records(store):
    """Working tasks newer than the cutoff stay working."""
    fresh = store.create(tool="render_frames", params={}, request_id="r1")
    flipped = store.sweep_stale_working(max_age_seconds=600.0)
    assert flipped == []
    assert store.get(fresh.id).state == "working"  # type: ignore[union-attr]


def test_sweep_stale_working_custom_cutoff(store):
    """A tighter cutoff catches tasks that the default 10-minute one would miss."""
    task = store.create(tool="render_frames", params={}, request_id="r1")
    # Backdate to 30 seconds ago.
    obj = store.get(task.id)
    assert obj is not None
    backdated = {**obj.model_dump(mode="json"), "updated_at": time.time() - 30}
    store._atomic_write(store._path(task.id), backdated)

    # Default cutoff (600s) leaves it alone.
    assert store.sweep_stale_working() == []
    # 10s cutoff catches it.
    flipped = store.sweep_stale_working(max_age_seconds=10.0)
    assert len(flipped) == 1
    assert flipped[0].state == "failed"

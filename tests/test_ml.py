"""Tests for ml.py CopyCat training + Cattery model registry tools.

C7. Three Task-wrapped tools (``train_copycat``,
``setup_dehaze_copycat``, ``install_cattery_model``) and two
synchronous tools (``serve_copycat``, ``list_cattery_models``).

CRITICAL TEST DOCTRINE: real CopyCat training takes hours of wall
time. Every test in this module mocks the worker -- the conftest
addon-side handlers record the wire payload and ack synchronously,
and tests drive ``task_progress`` notifications by ``put``-ing
directly on the connection's notification queue. NO test in this
file may trigger a real training run.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp import connection
from nuke_mcp import tasks as task_store
from nuke_mcp.tools import ml


class _StubMCP:
    """Captures registered tool callables (mirrors test_tracking pattern)."""

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
def ml_tools(mock_script):
    """Register ml tools against the connected mock server.

    Seeds a plate node so ``serve_copycat`` has a valid input target.
    """
    server, script = mock_script
    server.nodes["plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["plate"] = []
    ctx = _StubCtx()
    ml.register(ctx)
    return server, script, ctx.mcp.registered


@pytest.fixture
def ml_tools_with_taskstore(ml_tools, monkeypatch, tmp_path):
    """``ml_tools`` plus a clean per-test TaskStore.

    Pinning ``NUKE_MCP_TASK_DIR`` per test keeps the suite isolated
    from any real ``~/.nuke_mcp/tasks`` and from sibling tests.
    """
    monkeypatch.setenv("NUKE_MCP_TASK_DIR", str(tmp_path))
    task_store.reset_default_store()
    yield ml_tools
    task_store.reset_default_store()


# ---------------------------------------------------------------------------
# train_copycat -- async Task lifecycle
# ---------------------------------------------------------------------------


def test_train_copycat_returns_task_id(ml_tools_with_taskstore):
    """train_copycat returns a task_id immediately. Mock NEVER trains."""
    server, _script, tools = ml_tools_with_taskstore
    result = tools["train_copycat"](
        model_path="/tmp/model.cat",
        dataset_dir="/tmp/dataset",
        epochs=100,
    )
    assert "task_id" in result
    assert result["state"] == "working"
    assert len(server.async_trains) == 1
    payload = server.async_trains[0]
    assert payload["task_id"] == result["task_id"]
    assert payload["model_path"] == "/tmp/model.cat"
    assert payload["dataset_dir"] == "/tmp/dataset"
    assert payload["epochs"] == 100
    assert payload["inverse"] is False
    # Disk record exists in working state.
    store = task_store.default_store()
    task = store.get(result["task_id"])
    assert task is not None
    assert task.state == "working"
    assert task.tool == "train_copycat"


def test_train_copycat_progress_listener_emits_three_events(ml_tools_with_taskstore):
    """Mock worker emits 3 progress events then completion. NEVER trains."""
    server, _script, tools = ml_tools_with_taskstore
    out = tools["train_copycat"](
        model_path="/tmp/model.cat",
        dataset_dir="/tmp/dataset",
        epochs=300,
    )
    task_id = out["task_id"]
    queue = connection.notification_queue()

    # Inject three synthetic per-epoch progress lines + one completion.
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "working",
            "epoch": 50,
            "total_epochs": 300,
            "loss": 0.42,
            "eta_seconds": 1200,
            "sample_thumbnail_path": "/tmp/thumbs/50.png",
        }
    )
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "working",
            "epoch": 150,
            "total_epochs": 300,
            "loss": 0.21,
            "eta_seconds": 600,
            "sample_thumbnail_path": "/tmp/thumbs/150.png",
        }
    )
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "working",
            "epoch": 250,
            "total_epochs": 300,
            "loss": 0.09,
            "eta_seconds": 200,
            "sample_thumbnail_path": "/tmp/thumbs/250.png",
        }
    )
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "completed",
            "result": {"model_path": "/tmp/model.cat", "final_loss": 0.05},
        }
    )

    store = task_store.default_store()
    final = store.get(task_id)
    assert final is not None
    assert final.state == "completed"
    assert final.result == {"model_path": "/tmp/model.cat", "final_loss": 0.05}


def test_train_copycat_progress_dict_carries_known_fields(ml_tools_with_taskstore):
    """A working notification updates ``progress`` with the documented
    train field set: epoch / total_epochs / loss / eta_seconds /
    sample_thumbnail_path. Anything else is dropped.
    """
    server, _script, tools = ml_tools_with_taskstore
    out = tools["train_copycat"](
        model_path="/tmp/model.cat",
        dataset_dir="/tmp/dataset",
    )
    task_id = out["task_id"]
    queue = connection.notification_queue()
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "working",
            "epoch": 1000,
            "total_epochs": 10000,
            "loss": 0.31,
            "eta_seconds": 7200,
            "sample_thumbnail_path": "/tmp/thumb.png",
            # Stray field the addon shouldn't be sending; verify it's dropped.
            "raw_internal_state": {"hidden": True},
        }
    )
    store = task_store.default_store()
    task = store.get(task_id)
    assert task is not None
    assert task.progress == {
        "epoch": 1000,
        "total_epochs": 10000,
        "loss": 0.31,
        "eta_seconds": 7200,
        "sample_thumbnail_path": "/tmp/thumb.png",
    }


def test_train_copycat_failure_path(ml_tools_with_taskstore):
    """A ``failed`` notification flips the task to failed with error."""
    server, _script, tools = ml_tools_with_taskstore
    out = tools["train_copycat"](
        model_path="/tmp/model.cat",
        dataset_dir="/tmp/dataset",
    )
    task_id = out["task_id"]
    queue = connection.notification_queue()
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "failed",
            "error": {"error_class": "RuntimeError", "message": "GPU OOM"},
        }
    )
    store = task_store.default_store()
    task = store.get(task_id)
    assert task is not None
    assert task.state == "failed"
    assert task.error == {"error_class": "RuntimeError", "message": "GPU OOM"}


def test_train_copycat_cancellation(ml_tools_with_taskstore):
    """Cancelling a working train flips state to cancelled and dispatches
    cancel_copycat to the addon. Mock never trains.
    """
    server, _script, tools = ml_tools_with_taskstore
    out = tools["train_copycat"](
        model_path="/tmp/model.cat",
        dataset_dir="/tmp/dataset",
    )
    task_id = out["task_id"]

    # Register the tasks_* tools on a separate stub so we can call them.
    from nuke_mcp.tools import tasks as tasks_tools

    ctx = _StubCtx()
    tasks_tools.register(ctx)
    cancelled = ctx.mcp.registered["tasks_cancel"](task_id)

    assert cancelled["state"] == "cancelled"
    # Addon-side cancel signal dispatched.
    assert task_id in server.cancelled_copycats


# ---------------------------------------------------------------------------
# serve_copycat -- synchronous Inference node creation
# ---------------------------------------------------------------------------


def test_serve_copycat_creates_inference_node(ml_tools):
    server, _script, tools = ml_tools
    result = tools["serve_copycat"](
        model_path="/tmp/model.cat",
        plate="plate",
    )
    assert result.get("status") != "error", result
    # Returned NodeRef.
    assert result["type"] == "Inference"
    assert result["inputs"] == ["plate"]
    # Typed call recorded with model + plate.
    matching = [c for c in server.typed_calls if c[0] == "serve_copycat"]
    assert len(matching) == 1
    _cmd, params = matching[0]
    assert params["model_path"] == "/tmp/model.cat"
    assert params["plate"] == "plate"


def test_serve_copycat_with_explicit_name(ml_tools):
    server, _script, tools = ml_tools
    result = tools["serve_copycat"](
        model_path="/tmp/model.cat",
        plate="plate",
        name="dehaze_inference",
    )
    assert result.get("status") != "error", result
    assert result["name"] == "dehaze_inference"
    _cmd, params = [c for c in server.typed_calls if c[0] == "serve_copycat"][0]
    assert params["name"] == "dehaze_inference"


# ---------------------------------------------------------------------------
# setup_dehaze_copycat -- inverse training (clean -> hazy)
# ---------------------------------------------------------------------------


def test_setup_dehaze_copycat_swaps_in_out_layers(ml_tools_with_taskstore):
    """Inverse training: the addon receives in_layer=clean, out_layer=haze
    and inverse=True. Mock NEVER trains.
    """
    server, _script, tools = ml_tools_with_taskstore
    result = tools["setup_dehaze_copycat"](
        haze_exemplars=["/tmp/haze1.exr", "/tmp/haze2.exr"],
        clean_exemplars=["/tmp/clean1.exr", "/tmp/clean2.exr"],
        epochs=8000,
    )
    assert "task_id" in result
    assert result["state"] == "working"
    assert len(server.async_trains) == 1
    payload = server.async_trains[0]
    # Inverse-training contract: in is "clean", out is "haze" on the
    # trainer side, even though at inference time the wiring runs forward.
    assert payload["in_layer"] == "clean"
    assert payload["out_layer"] == "haze"
    assert payload["inverse"] is True
    assert payload["haze_exemplars"] == ["/tmp/haze1.exr", "/tmp/haze2.exr"]
    assert payload["clean_exemplars"] == ["/tmp/clean1.exr", "/tmp/clean2.exr"]
    assert payload["epochs"] == 8000
    # Disk record exists.
    store = task_store.default_store()
    task = store.get(result["task_id"])
    assert task is not None
    assert task.tool == "setup_dehaze_copycat"


def test_setup_dehaze_copycat_cancellation(ml_tools_with_taskstore):
    """Cancelling a dehaze train uses the same cancel_copycat dispatch."""
    server, _script, tools = ml_tools_with_taskstore
    out = tools["setup_dehaze_copycat"](
        haze_exemplars=["/tmp/h.exr"],
        clean_exemplars=["/tmp/c.exr"],
    )
    task_id = out["task_id"]

    from nuke_mcp.tools import tasks as tasks_tools

    ctx = _StubCtx()
    tasks_tools.register(ctx)
    cancelled = ctx.mcp.registered["tasks_cancel"](task_id)
    assert cancelled["state"] == "cancelled"
    assert task_id in server.cancelled_copycats


# ---------------------------------------------------------------------------
# list_cattery_models -- read-only catalog
# ---------------------------------------------------------------------------


def test_list_cattery_models_returns_cache(ml_tools):
    server, _script, tools = ml_tools
    server.cattery_models = [
        {"id": "denoise_v2", "category": "denoise", "size_mb": 120, "installed": True},
        {"id": "dehaze_v1", "category": "dehaze", "size_mb": 85, "installed": True},
        {"id": "upres_2x", "category": "upres", "size_mb": 250, "installed": False},
    ]
    result = tools["list_cattery_models"]()
    assert result.get("status") != "error", result
    assert result["count"] == 3
    ids = {m["id"] for m in result["models"]}
    assert ids == {"denoise_v2", "dehaze_v1", "upres_2x"}


def test_list_cattery_models_filters_by_category(ml_tools):
    server, _script, tools = ml_tools
    server.cattery_models = [
        {"id": "denoise_v2", "category": "denoise"},
        {"id": "dehaze_v1", "category": "dehaze"},
        {"id": "upres_2x", "category": "upres"},
    ]
    result = tools["list_cattery_models"](category="denoise")
    assert result["count"] == 1
    assert result["models"][0]["id"] == "denoise_v2"


def test_list_cattery_models_empty_cache(ml_tools):
    """No installed models -> empty list, not an error."""
    _server, _script, tools = ml_tools
    result = tools["list_cattery_models"]()
    assert result.get("status") != "error", result
    assert result["count"] == 0
    assert result["models"] == []


# ---------------------------------------------------------------------------
# install_cattery_model -- async download Task
# ---------------------------------------------------------------------------


def test_install_cattery_model_returns_task_id(ml_tools_with_taskstore):
    """Mock NEVER downloads. Records call + acks immediately."""
    server, _script, tools = ml_tools_with_taskstore
    result = tools["install_cattery_model"](model_id="denoise_v2")
    assert "task_id" in result
    assert result["state"] == "working"
    assert len(server.async_installs) == 1
    payload = server.async_installs[0]
    assert payload["task_id"] == result["task_id"]
    assert payload["model_id"] == "denoise_v2"
    store = task_store.default_store()
    task = store.get(result["task_id"])
    assert task is not None
    assert task.tool == "install_cattery_model"


def test_install_cattery_model_progress_then_complete(ml_tools_with_taskstore):
    """Mock injects download-progress notifications via the queue. NEVER downloads."""
    server, _script, tools = ml_tools_with_taskstore
    out = tools["install_cattery_model"](model_id="dehaze_v1")
    task_id = out["task_id"]
    queue = connection.notification_queue()

    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "working",
            "bytes_downloaded": 1_000_000,
            "total_bytes": 5_000_000,
            "step": "download",
        }
    )
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "working",
            "bytes_downloaded": 5_000_000,
            "total_bytes": 5_000_000,
            "step": "verify",
        }
    )
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "completed",
            "result": {
                "model_path": "/home/user/.nuke/cattery/dehaze_v1.cat",
                "model_id": "dehaze_v1",
                "sha256": "abc123",
            },
        }
    )

    store = task_store.default_store()
    task = store.get(task_id)
    assert task is not None
    assert task.state == "completed"
    assert task.result["model_id"] == "dehaze_v1"


def test_install_cattery_model_cancellation(ml_tools_with_taskstore):
    """Cancelling an install dispatches cancel_install to the addon."""
    server, _script, tools = ml_tools_with_taskstore
    out = tools["install_cattery_model"](model_id="upres_2x")
    task_id = out["task_id"]

    from nuke_mcp.tools import tasks as tasks_tools

    ctx = _StubCtx()
    tasks_tools.register(ctx)
    cancelled = ctx.mcp.registered["tasks_cancel"](task_id)
    assert cancelled["state"] == "cancelled"
    assert task_id in server.cancelled_installs
    # The render-cancel dispatch must NOT have fired.
    assert task_id not in server.cancelled_renders
    assert task_id not in server.cancelled_copycats


# ---------------------------------------------------------------------------
# Edge cases / defensive paths
# ---------------------------------------------------------------------------


def test_train_copycat_with_explicit_name(ml_tools_with_taskstore):
    """``name`` flows through to the addon payload."""
    server, _script, tools = ml_tools_with_taskstore
    tools["train_copycat"](
        model_path="/tmp/m.cat",
        dataset_dir="/tmp/d",
        name="dehaze_trainer",
    )
    payload = server.async_trains[0]
    assert payload["name"] == "dehaze_trainer"


def test_setup_dehaze_copycat_with_explicit_name(ml_tools_with_taskstore):
    server, _script, tools = ml_tools_with_taskstore
    tools["setup_dehaze_copycat"](
        haze_exemplars=["/tmp/h.exr"],
        clean_exemplars=["/tmp/c.exr"],
        name="my_dehaze",
    )
    payload = server.async_trains[0]
    assert payload["name"] == "my_dehaze"


def test_install_cattery_model_with_explicit_name(ml_tools_with_taskstore):
    server, _script, tools = ml_tools_with_taskstore
    tools["install_cattery_model"](model_id="denoise_v2", name="my_denoiser")
    payload = server.async_installs[0]
    assert payload["name"] == "my_denoiser"


def test_progress_listener_ignores_late_terminal(ml_tools_with_taskstore):
    """A stragglers ``working`` line after ``completed`` MUST NOT
    downgrade the task -- exact mirror of render's safety check.
    """
    server, _script, tools = ml_tools_with_taskstore
    out = tools["train_copycat"](
        model_path="/tmp/m.cat",
        dataset_dir="/tmp/d",
    )
    task_id = out["task_id"]
    queue = connection.notification_queue()
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "completed",
            "result": {"model_path": "/tmp/m.cat"},
        }
    )
    # Late stragglers working notification arrives after termination.
    queue.put(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "working",
            "epoch": 9999,
            "total_epochs": 10000,
        }
    )
    store = task_store.default_store()
    task = store.get(task_id)
    assert task is not None
    assert task.state == "completed"


def test_progress_listener_handles_cancelled_notification(ml_tools_with_taskstore):
    """A direct cancelled-state notification flips the task without a
    cancel call -- the addon worker can self-cancel on a sigint.
    """
    server, _script, tools = ml_tools_with_taskstore
    out = tools["install_cattery_model"](model_id="x")
    task_id = out["task_id"]
    queue = connection.notification_queue()
    queue.put({"type": "task_progress", "id": task_id, "state": "cancelled"})
    store = task_store.default_store()
    task = store.get(task_id)
    assert task is not None
    assert task.state == "cancelled"


def test_progress_listener_drops_notification_with_empty_id(ml_tools_with_taskstore):
    """A malformed notification with no id is dropped silently."""
    # Should not raise. This exercises the early-return path.
    ml._on_progress({"type": "task_progress", "id": ""}, ml._TRAIN_PROGRESS_FIELDS)
    ml._on_progress({"type": "task_progress"}, ml._TRAIN_PROGRESS_FIELDS)


def test_progress_listener_drops_notification_for_unknown_task(ml_tools_with_taskstore):
    """A notification for a task that doesn't exist on disk is dropped."""
    ml._on_progress(
        {"type": "task_progress", "id": "nonexistent_task_id", "state": "working"},
        ml._TRAIN_PROGRESS_FIELDS,
    )


def test_progress_listener_short_circuits_on_already_terminal(ml_tools_with_taskstore):
    """The listener bails on a still-bound task whose disk record has
    already moved to terminal. Calls _on_progress directly so the
    pre-listener-unregister race window is what's exercised.
    """
    server, _script, tools = ml_tools_with_taskstore
    out = tools["train_copycat"](
        model_path="/tmp/m.cat",
        dataset_dir="/tmp/d",
    )
    task_id = out["task_id"]
    # Flip the disk record to completed without going through the listener.
    store = task_store.default_store()
    store.update(task_id, state="completed", result={"model_path": "/tmp/m.cat"})
    # Now invoke the listener with a working notification: the in-memory
    # disk-state check should short-circuit on TERMINAL_STATES.
    ml._on_progress(
        {
            "type": "task_progress",
            "id": task_id,
            "state": "working",
            "epoch": 50,
            "total_epochs": 100,
        },
        ml._TRAIN_PROGRESS_FIELDS,
    )
    task = store.get(task_id)
    assert task is not None
    assert task.state == "completed"


def test_train_copycat_addon_dispatch_failure_marks_task_failed(
    ml_tools_with_taskstore, monkeypatch
):
    """If the addon hand-off raises, the freshly-created Task is flipped
    to failed before the exception propagates to the caller.
    """
    from nuke_mcp import connection as conn_mod

    real_send = conn_mod.send

    def boom(*args, **kwargs):
        if args and args[0] == "train_copycat_async":
            raise conn_mod.ConnectionError("addon offline")
        return real_send(*args, **kwargs)

    monkeypatch.setattr(conn_mod, "send", boom)

    server, _script, tools = ml_tools_with_taskstore
    result = tools["train_copycat"](
        model_path="/tmp/m.cat",
        dataset_dir="/tmp/d",
    )
    # The nuke_command wrapper catches the ConnectionError and returns a
    # structured error envelope. Verify one Task exists in failed state.
    assert result.get("status") == "error"
    store = task_store.default_store()
    tasks_in_store = store.list()
    assert len(tasks_in_store) == 1
    assert tasks_in_store[0].state == "failed"
    assert tasks_in_store[0].error is not None
    assert "ConnectionError" in tasks_in_store[0].error.get("error_class", "")

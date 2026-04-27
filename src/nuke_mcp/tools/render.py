"""Render and precomp tools.

B5 wraps the addon's ``render`` reply through ``RenderResult`` -- the
wire shape ``{rendered, frames}`` flows through the model, which adds
typed accessors for ``frames_written`` / ``output_path`` while
preserving the original keys via ``extra="allow"``.

B2 turns ``render_frames`` into an MCP Task by default. The
synchronous=True kwarg keeps the pre-B2 wire shape for callers that
want a blocking render -- the back-compat tests run through that
path. Async mode persists a Task record, sends ``render_async`` to
the addon (which returns immediately after spawning a worker thread),
and registers a notification listener that updates the Task as
``task_progress`` lines arrive.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from nuke_mcp import connection
from nuke_mcp import tasks as task_store
from nuke_mcp.annotations import BENIGN_NEW, DESTRUCTIVE, OPEN_WORLD, READ_ONLY
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.models import RenderResult
from nuke_mcp.models._warnings import warn_once
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext

log = logging.getLogger(__name__)


def _model_dump(model: Any) -> dict[str, Any]:
    """``model_dump`` with the canonical B5 flag set."""
    return model.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)


def _on_progress(notif: dict[str, Any]) -> None:
    """Notification listener that mirrors a ``task_progress`` line into
    the TaskStore. Registered per-task at render-launch time and
    unregistered on the terminal state.

    The addon emits a ``state`` field on every notification (working,
    completed, failed, cancelled). Terminal states get the ``result``
    or ``error`` payload merged in. Non-terminal updates only bump the
    ``progress`` dict so the operator can poll for live frame counts.
    """
    task_id = str(notif.get("id", ""))
    if not task_id:
        return
    state = notif.get("state", "working")
    store = task_store.default_store()
    try:
        existing = store.get(task_id)
        if existing is None:
            return
        # Don't downgrade a terminal state -- if we already saw a
        # ``completed`` line and a stragglers ``working`` line arrives,
        # ignore the late one.
        if existing.state in task_store.TERMINAL_STATES:
            return

        update_fields: dict[str, Any] = {}
        if state == "working":
            progress = {
                "frame": notif.get("frame"),
                "total": notif.get("total"),
                "step": notif.get("progress"),
            }
            update_fields["progress"] = {k: v for k, v in progress.items() if v is not None}
        elif state == "completed":
            update_fields["state"] = "completed"
            result = notif.get("result")
            if isinstance(result, dict):
                update_fields["result"] = result
        elif state == "failed":
            update_fields["state"] = "failed"
            error = notif.get("error")
            if isinstance(error, dict):
                update_fields["error"] = error
        elif state == "cancelled":
            update_fields["state"] = "cancelled"
        store.update(task_id, **update_fields)

        if state in task_store.TERMINAL_STATES:
            connection.notification_queue().unregister_listener(task_id)
    except Exception:
        log.exception("on_progress listener for task=%s failed", task_id)


def _start_async_render(params: dict[str, Any]) -> dict[str, Any]:
    """Create a Task, register a progress listener, dispatch to addon.

    Returns immediately with the task_id and the initial ``working``
    state. The addon's ``render_async`` handler returns a synchronous
    ``{started: true}`` confirmation; subsequent frame progress flows
    through the notification queue and ``_on_progress``.
    """
    store = task_store.default_store()
    request_id = ""  # filled in by ``connection.send`` -- see below
    task = store.create(tool="render_frames", params=dict(params), request_id=request_id)
    queue = connection.notification_queue()
    queue.register_listener(task.id, _on_progress)
    try:
        ack = connection.send("render_async", _class="render", task_id=task.id, **params)
    except Exception as exc:
        # Failed to even hand off to the addon -- mark the task failed
        # so the operator's tasks_get reflects reality, then surface
        # the original error.
        queue.unregister_listener(task.id)
        with contextlib.suppress(Exception):
            store.update(
                task.id,
                state="failed",
                error={"error_class": type(exc).__name__, "message": str(exc)},
            )
        raise
    return {
        "task_id": task.id,
        "state": task.state,
        "ack": ack,
    }


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="core", annotations=BENIGN_NEW)
    @nuke_command("setup_write")
    def setup_write(
        input_node: str,
        path: str,
        file_type: str = "exr",
        colorspace: str = "scene_linear",
    ) -> dict:
        """Create a Write node connected to input_node with production defaults.

        A3: typed dispatch -- the addon validates ``file_type`` against
        the allowlist (exr/tiff/png/jpeg/mov/dpx) and rejects any path
        with a ``..`` traversal component.

        Args:
            input_node: node to connect as input.
            path: output file path (use #### for frame padding).
            file_type: exr, tiff, png, jpeg, mov, or dpx.
            colorspace: output colorspace.
        """
        return run_on_main(
            "setup_write",
            {
                "input_node": input_node,
                "path": path,
                "file_type": file_type,
                "colorspace": colorspace,
            },
            "mutate",
        )

    @nuke_tool(
        ctx,
        profile="core",
        annotations=DESTRUCTIVE | OPEN_WORLD,
        output_model=RenderResult,
    )
    @nuke_command("render_frames")
    def render_frames(
        write_node: str | None = None,
        first_frame: int | None = None,
        last_frame: int | None = None,
        confirm: bool = False,
        synchronous: bool = False,
    ) -> dict:
        """Render frames through a Write node.

        By default this returns immediately with a ``task_id`` and
        runs the render in the background; poll ``tasks_get(task_id)``
        for progress, ``tasks_cancel`` to interrupt, or wait for the
        terminal state notification.

        Pass ``synchronous=True`` to keep the pre-B2 blocking shape
        (returns the full ``RenderResult`` once the render finishes).
        That path is wire-compatible with existing callers.

        Args:
            write_node: name of Write node. uses first Write in script if omitted.
            first_frame: start frame. uses script range if omitted.
            last_frame: end frame. uses script range if omitted.
            confirm: must be True to render. call with False to preview.
            synchronous: block until done and return ``RenderResult``
                (B2 back-compat). Defaults to False -- use the async
                Task flow.
        """
        if not confirm:
            msg = "will render"
            if write_node:
                msg += f" through '{write_node}'"
            if first_frame is not None and last_frame is not None:
                msg += f" frames {first_frame}-{last_frame}"
            return {"preview": msg + ". call with confirm=True."}

        params: dict = {}
        if write_node:
            params["write_node"] = write_node
        if first_frame is not None and last_frame is not None:
            params["frame_range"] = [first_frame, last_frame]

        if synchronous:
            # render = non-idempotent (writes frames). 900s class timeout
            # via TIMEOUT_CLASSES["render"] -- removes the prior 300s magic
            # number and routes the call through the same envelope as send().
            result = connection.send("render", _class="render", **params)
            if isinstance(result, dict) and "rendered" in result:
                try:
                    return _model_dump(RenderResult.model_validate(result))
                except Exception as exc:
                    warn_once(
                        log,
                        "render_frames",
                        "render_frames: RenderResult validation failed; "
                        "returning raw payload: %s",
                        exc,
                    )
                    return result
            return result

        return _start_async_render(params)

    # ``setup_precomp`` creates new Read+Write nodes -- not idempotent.
    # ``BENIGN_NEW`` carries the explicit ``destructiveHint=False``.
    @nuke_tool(ctx, profile="core", annotations=BENIGN_NEW)
    @nuke_command("setup_precomp")
    def setup_precomp(
        source_node: str,
        name: str | None = None,
        path: str | None = None,
    ) -> dict:
        """Set up a precomp: creates a Write node for the source, and a Read node
        that reads the rendered output back in. Downstream nodes get rewired to
        the Read.

        The Write path is auto-generated from the script name and precomp name
        if not specified.

        Args:
            source_node: node whose output to precomp.
            name: label for the precomp (used in file path). defaults to source node name.
            path: explicit output path. auto-generated if omitted.
        """
        precomp_name = name or source_node
        code = f"""
import nuke, os, tempfile

src = nuke.toNode({source_node!r})
if not src:
    raise ValueError("node not found: {source_node}")

# auto-generate path from script location
script_path = nuke.root().name()
script_dir = os.path.dirname(script_path) if script_path else tempfile.gettempdir()
script_base = os.path.splitext(os.path.basename(script_path))[0] if script_path else "untitled"
precomp_dir = os.path.join(script_dir, "precomp", {precomp_name!r})
os.makedirs(precomp_dir, exist_ok=True)

out_path = {path!r} if {path!r} else os.path.join(precomp_dir, script_base + "_{precomp_name}.####.exr")

# collect downstream connections before rewiring
dependents = src.dependent()
downstream = []
for dep in dependents:
    for i in range(dep.inputs()):
        if dep.input(i) == src:
            downstream.append((dep, i))

# create Write
first = int(nuke.root()["first_frame"].value())
last = int(nuke.root()["last_frame"].value())

w = nuke.nodes.Write()
w.setName("{precomp_name}_write")
w.setInput(0, src)
w["file"].setValue(out_path)
w["file_type"].setValue("exr")
w["first"].setValue(first)
w["last"].setValue(last)
w.setXYpos(src.xpos(), src.ypos() + 80)

# create Read
r = nuke.nodes.Read()
r.setName("{precomp_name}_read")
r["file"].setValue(out_path)
r["first"].setValue(first)
r["last"].setValue(last)
r.setXYpos(src.xpos() + 150, src.ypos() + 80)

# rewire downstream to read from the Read node
for dep, idx in downstream:
    dep.setInput(idx, r)

__result__ = {{
    "write": w.name(),
    "read": r.name(),
    "path": out_path,
    "frames": [first, last],
    "rewired": len(downstream),
}}
"""
        return connection.send("execute_python", code=code)

    @nuke_tool(ctx, profile="core", annotations=READ_ONLY)
    @nuke_command("list_precomps")
    def list_precomps() -> dict:
        """Find all precomp Write/Read pairs in the script."""
        code = """
import nuke, os
writes = nuke.allNodes("Write")
precomps = []
for w in writes:
    path = w["file"].value()
    if not path:
        continue
    # check if there's a matching Read
    matching_reads = [r for r in nuke.allNodes("Read") if r["file"].value() == path]
    rendered = os.path.exists(path.replace("####", "0001")) if path else False
    precomps.append({
        "write": w.name(),
        "read": matching_reads[0].name() if matching_reads else None,
        "path": path,
        "rendered": rendered,
    })
__result__ = {"precomps": precomps, "count": len(precomps)}
"""
        return connection.send("execute_python", code=code)

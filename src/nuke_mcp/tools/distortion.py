"""Lens-distortion / STMap envelope + SmartVector propagate primitives.

C4 atomic primitives for the distortion phase of a comp:

* ``bake_lens_distortion_envelope`` -- wraps the comp body in a
  NetworkBox labelled ``LinearComp_undistorted_<shot>``. The head of
  the box is ``LensDistortion -> STMap(undistort)``; the tail is
  ``STMap(redistort) -> Write``. The two STMaps cache to
  ``$SS/comp/stmaps/{shot}_{undistort,redistort}.exr``.
* ``apply_idistort`` -- creates an IDistort node fed by ``plate`` on
  slot 0 and ``vector_node`` on slot 1, with the UV channels on the
  vector pinned to ``forward.u`` / ``forward.v`` by default.
* ``apply_smartvector_propagate`` -- spawns a SmartVector bake as an
  MCP Task; returns a ``task_id`` immediately so the model can poll
  ``tasks_get`` for progress and ``tasks_cancel`` to interrupt.
* ``generate_stmap`` -- renders a forward or inverse STMap from a
  LensDistortion node. Same Task-wrapped pattern as the smartvector
  bake; the actual render runs on a worker thread inside Nuke.

The two synchronous tools (``bake_lens_distortion_envelope`` and
``apply_idistort``) follow the C1 typed-handler pattern: a thin tool
function dispatches to a typed addon handler via ``run_on_main`` and
gets back a flat ``NodeRef`` (or a ``{box, head, tail}`` dict for the
envelope). The two async tools follow the B2 ``render_frames`` pattern
in ``render.py`` (commits ``83ad981`` and the B2 Tasks merge): create a
Task, register a notification listener, fire the addon's ``*_async``
command, and return ``{task_id, state, ack}`` while the addon worker
emits ``task_progress`` over the live socket.
"""

from __future__ import annotations

import contextlib
import logging
import os
import pathlib
from typing import Any, Literal

from nuke_mcp import connection
from nuke_mcp import tasks as task_store
from nuke_mcp.annotations import BENIGN_NEW
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# STMap cache resolution
# ---------------------------------------------------------------------------


def _resolve_stmap_cache_root() -> pathlib.Path:
    """Resolve the STMap cache directory, honouring environment overrides.

    Search order (first hit wins):

    1. ``$SS`` -- Salt Spill sandbox root. Production layout puts every
       comp asset under ``$SS/comp/`` so the STMap cache lands beside
       the rest of the show data.
    2. ``$NUKE_MCP_SS_ROOT`` -- explicit override for callers running
       outside the Salt Spill tree (CI, sibling shows, dev sandboxes).
       Same ``comp/stmaps/`` subtree underneath.
    3. ``~/.nuke_mcp/stmaps`` -- last-resort fallback that always exists.

    Both env-driven roots get ``comp/stmaps`` appended so the on-disk
    layout is identical regardless of which knob is set; only the parent
    differs.
    """
    ss = os.environ.get("SS")
    if ss:
        return pathlib.Path(ss) / "comp" / "stmaps"
    override = os.environ.get("NUKE_MCP_SS_ROOT")
    if override:
        return pathlib.Path(override) / "comp" / "stmaps"
    return pathlib.Path.home() / ".nuke_mcp" / "stmaps"


def _stmap_paths_for_shot(shot: str) -> dict[str, str]:
    """Return ``{undistort, redistort}`` paths for a shot's STMap cache."""
    root = _resolve_stmap_cache_root()
    return {
        "undistort": str(root / f"{shot}_undistort.exr"),
        "redistort": str(root / f"{shot}_redistort.exr"),
    }


# ---------------------------------------------------------------------------
# Async Task helpers (mirror render.py:_on_progress / _start_async_render)
# ---------------------------------------------------------------------------


def _on_progress(notif: dict[str, Any]) -> None:
    """Notification listener that mirrors a ``task_progress`` line into
    the TaskStore.

    Identical shape to ``render._on_progress``: the addon emits
    ``state`` on every notification (working / completed / failed /
    cancelled); terminal states get the ``result`` or ``error`` payload
    merged in; non-terminal updates only bump ``progress`` so the
    operator can poll for live frame counts.

    Late-arriving working lines after a terminal state are ignored so a
    stragglers ``working`` notification can't downgrade a completed
    task.
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


def _start_async(tool_name: str, addon_cmd: str, params: dict[str, Any]) -> dict[str, Any]:
    """Create a Task, register a progress listener, fire the addon command.

    Generalises the ``render._start_async_render`` shape so both
    ``apply_smartvector_propagate`` and ``generate_stmap`` can reuse a
    single body. The addon-side handler returns a synchronous
    ``{started: true}`` ack; subsequent progress flows through the
    notification queue and ``_on_progress``.
    """
    store = task_store.default_store()
    task = store.create(tool=tool_name, params=dict(params), request_id="")
    queue = connection.notification_queue()
    queue.register_listener(task.id, _on_progress)
    try:
        ack = connection.send(addon_cmd, _class="render", task_id=task.id, **params)
    except Exception as exc:
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


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="distortion", annotations=BENIGN_NEW)
    @nuke_command("bake_lens_distortion_envelope")
    def bake_lens_distortion_envelope(
        plate: str,
        lens_solve: str,
        write_path: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Wrap the comp in an undistorted-linear envelope.

        Builds a NetworkBox labelled ``LinearComp_undistorted_<shot>``
        with two stages:

        * **Head** -- ``LensDistortion -> STMap`` (undistort) -- takes
          the plate to a linear, undistorted pixel space the rest of
          the comp can work in cleanly.
        * **Tail** -- ``STMap (redistort) -> Write`` -- re-applies the
          original lens warp on the way out and renders to disk.

        STMap caches go under ``$SS/comp/stmaps/{shot}_{undistort,
        redistort}.exr``, falling back to ``$NUKE_MCP_SS_ROOT`` and
        finally ``~/.nuke_mcp/stmaps`` when ``$SS`` is unset.

        Args:
            plate: source Read node name. Also doubles as the shot
                identifier embedded in the box name and STMap
                filenames; pass a clean shot label, not a path.
            lens_solve: existing LensDistortion (or LD_3DE_*) node
                whose model the head/tail STMaps lift from.
            write_path: explicit final Write path. If ``None`` the
                addon synthesises a path next to the script.
            name: idempotent re-call key. Returned existing
                ``{box, head, tail}`` dict if a NetworkBox of the
                same name already exists.
        """
        params: dict = {
            "plate": plate,
            "lens_solve": lens_solve,
            "stmap_paths": _stmap_paths_for_shot(plate),
        }
        if write_path is not None:
            params["write_path"] = write_path
        if name is not None:
            params["name"] = name
        return run_on_main("bake_lens_distortion_envelope", params, "mutate")

    @nuke_tool(ctx, profile="distortion", annotations=BENIGN_NEW)
    @nuke_command("apply_idistort")
    def apply_idistort(
        plate: str,
        vector_node: str,
        uv_channels: tuple[str, str] = ("forward.u", "forward.v"),
        name: str | None = None,
    ) -> dict:
        """Create an IDistort node wired between ``plate`` and ``vector_node``.

        Slot 0 takes the plate; slot 1 takes the motion-vector source
        (typically a SmartVector or an STMap). The U/V knobs default to
        ``forward.u`` / ``forward.v`` -- the standard SmartVector channel
        layout -- but callers can repoint at ``backward.u/v`` when
        propagating in the other temporal direction.

        Args:
            plate: source plate (slot 0).
            vector_node: motion-vector input (slot 1). Usually a
                SmartVector, STMap, or any node that emits the chosen
                ``uv_channels``.
            uv_channels: 2-tuple of channel names. First entry maps to
                ``uv.x`` knob, second to ``uv.y``. Defaults to forward
                SmartVector channels.
            name: idempotent re-call key.
        """
        u_chan, v_chan = uv_channels
        params: dict = {
            "plate": plate,
            "vector_node": vector_node,
            "u_channel": u_chan,
            "v_channel": v_chan,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("apply_idistort", params, "mutate")

    @nuke_tool(ctx, profile="distortion", annotations=BENIGN_NEW)
    @nuke_command("apply_smartvector_propagate")
    def apply_smartvector_propagate(
        plate: str,
        paint_frame: int,
        range_in: int,
        range_out: int,
        name: str | None = None,
    ) -> dict:
        """Bake SmartVectors propagating a paint from ``paint_frame``
        across ``[range_in, range_out]``.

        Returns immediately with a ``{task_id, state, ack}`` dict; the
        actual bake runs as an MCP Task on a worker thread inside Nuke.
        Poll ``tasks_get(task_id)`` for progress, ``tasks_cancel`` to
        interrupt between frames.

        Args:
            plate: source plate (Read or upstream comp node).
            paint_frame: frame the paint reference lives on.
            range_in: first frame to propagate to.
            range_out: last frame to propagate to.
            name: optional explicit name for the SmartVector node.
        """
        params: dict = {
            "plate": plate,
            "paint_frame": int(paint_frame),
            "range_in": int(range_in),
            "range_out": int(range_out),
        }
        if name is not None:
            params["name"] = name
        return _start_async(
            tool_name="apply_smartvector_propagate",
            addon_cmd="apply_smartvector_propagate_async",
            params=params,
        )

    @nuke_tool(ctx, profile="distortion", annotations=BENIGN_NEW)
    @nuke_command("generate_stmap")
    def generate_stmap(
        lens_distortion_node: str,
        mode: Literal["undistort", "redistort"] = "undistort",
        name: str | None = None,
    ) -> dict:
        """Render an STMap from a ``LensDistortion`` node.

        Returns immediately with a ``{task_id, state, ack}`` dict; the
        render runs as an MCP Task. ``mode="undistort"`` renders the
        forward STMap (plate -> linear); ``mode="redistort"`` renders
        the inverse (linear -> plate).

        Args:
            lens_distortion_node: existing LensDistortion (or LD_3DE_*)
                node to lift the model from.
            mode: ``undistort`` (default) or ``redistort``.
            name: optional explicit name. When supplied AND a Task with
                the same params already exists in working/completed
                state, the existing task_id is returned (idempotent
                re-call) instead of spawning a duplicate render.
        """
        params: dict = {
            "lens_distortion_node": lens_distortion_node,
            "mode": mode,
        }
        if name is not None:
            params["name"] = name
            # Idempotent re-call: a non-terminal task with the same
            # (lens_distortion_node, mode, name) tuple should be
            # surfaced as-is rather than spawn a duplicate render.
            store = task_store.default_store()
            for existing in store.list():
                if existing.tool != "generate_stmap":
                    continue
                if existing.state in task_store.TERMINAL_STATES:
                    continue
                ep = existing.params or {}
                if (
                    ep.get("lens_distortion_node") == lens_distortion_node
                    and ep.get("mode") == mode
                    and ep.get("name") == name
                ):
                    return {
                        "task_id": existing.id,
                        "state": existing.state,
                        "ack": {"reused": True},
                    }
        return _start_async(
            tool_name="generate_stmap",
            addon_cmd="generate_stmap_async",
            params=params,
        )

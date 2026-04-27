"""CopyCat training, inference, and Cattery model management tools.

Phase C7. CopyCat is Nuke's ML training feature: a CopyCat node trained
on paired exemplars (input plate -> desired output) produces a small
``.cat`` model. Training is multi-hour wall time, so every train tool
in this module is wrapped as an MCP Task (B2 Tasks primitive). The
synchronous tools are limited to inference (``serve_copycat``: load a
``.cat`` into an Inference node) and read-only catalog queries
(``list_cattery_models``).

Cattery is Foundry's model registry -- a curated collection of
pre-trained ``.cat`` files for common comp tasks (denoise, dehaze,
matte refine, upres). ``list_cattery_models`` enumerates the local
``~/.nuke/cattery/`` cache plus an optional remote registry stub.
``install_cattery_model`` downloads a model into that cache; the
download itself is Task-wrapped because production-grade models can
weigh hundreds of megabytes.

Async dispatch mirrors ``render.py`` exactly:

* ``_start_async_*`` creates a ``Task``, registers a per-task
  notification listener, sends the ``*_async`` command to the addon,
  and returns ``{task_id, state, ack}``.
* ``_on_progress`` mirrors addon-emitted ``task_progress`` lines into
  the ``TaskStore`` (working / completed / failed / cancelled). The
  listener self-unregisters on any terminal state.

The progress payload differs by tool: training emits per-epoch
``{epoch, total_epochs, loss, eta_seconds, sample_thumbnail_path}``
fields; install emits ``{bytes_downloaded, total_bytes}``. The
listener stores whatever non-None fields the notification carries so
the schema is forward-compatible -- adding a new progress field on the
addon side doesn't require a server change here.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from nuke_mcp import connection
from nuke_mcp import tasks as task_store
from nuke_mcp.annotations import BENIGN_NEW, READ_ONLY
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext

log = logging.getLogger(__name__)


# Progress fields that get mirrored verbatim into ``Task.progress``
# during the ``working`` state. Keeping the allow-list explicit (rather
# than blindly forwarding the whole notification dict) prevents the
# addon from leaking incidental wire envelopes -- ``id``, ``state``,
# ``type`` -- into the user-visible progress snapshot.
_TRAIN_PROGRESS_FIELDS: tuple[str, ...] = (
    "epoch",
    "total_epochs",
    "loss",
    "eta_seconds",
    "sample_thumbnail_path",
)

_INSTALL_PROGRESS_FIELDS: tuple[str, ...] = (
    "bytes_downloaded",
    "total_bytes",
    "step",
)


def _on_progress(notif: dict[str, Any], progress_fields: tuple[str, ...]) -> None:
    """Notification listener factory body. See ``render._on_progress``.

    The shape matches ``render.py`` line for line: terminal states
    unregister the listener and merge ``result`` / ``error`` payloads;
    non-terminal lines bump ``progress`` with whatever fields the
    addon emitted from ``progress_fields``. Splitting the closure out
    of the per-tool factory keeps the test surface small -- one mock
    notification feed exercises every train/install code path.
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
        # Don't downgrade a terminal state: a stragglers ``working``
        # line arriving after ``completed`` is a normal race when the
        # addon emits its final progress before the result, and we
        # already saw the result.
        if existing.state in task_store.TERMINAL_STATES:
            return

        update_fields: dict[str, Any] = {}
        if state == "working":
            progress = {k: notif.get(k) for k in progress_fields}
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


def _make_train_listener() -> Any:
    """Build the per-task callback for training progress.

    A fresh callable is bound per task so the closure captures the
    train-specific progress field allow-list. Install gets its own.
    """

    def _listener(notif: dict[str, Any]) -> None:
        _on_progress(notif, _TRAIN_PROGRESS_FIELDS)

    return _listener


def _make_install_listener() -> Any:
    def _listener(notif: dict[str, Any]) -> None:
        _on_progress(notif, _INSTALL_PROGRESS_FIELDS)

    return _listener


def _start_async(
    *,
    tool: str,
    addon_command: str,
    params: dict[str, Any],
    progress_fields: tuple[str, ...],
    timeout_class: str = "copycat",
) -> dict[str, Any]:
    """Create a Task + register progress listener + dispatch to addon.

    Generic helper shared by the three Task-wrapped tools in this
    module. The addon-side handler is expected to:

    1. Spawn a worker thread on receipt.
    2. Return a synchronous ``{started: true, task_id}`` ack.
    3. Stream ``task_progress`` notifications over the same socket
       until a terminal-state line is emitted.

    A failure to even hand off (the addon is down, the wire payload
    didn't validate) flips the freshly-created Task to ``failed`` so
    ``tasks_get`` reflects reality, then re-raises so the
    ``nuke_command`` decorator can wrap the structured error envelope.
    """
    store = task_store.default_store()
    task = store.create(tool=tool, params=dict(params), request_id="")
    queue = connection.notification_queue()
    if progress_fields is _INSTALL_PROGRESS_FIELDS:
        listener = _make_install_listener()
    else:
        listener = _make_train_listener()
    queue.register_listener(task.id, listener)
    try:
        ack = connection.send(addon_command, _class=timeout_class, task_id=task.id, **params)
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


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="copycat", annotations=BENIGN_NEW)
    @nuke_command("train_copycat")
    def train_copycat(
        model_path: str,
        dataset_dir: str,
        epochs: int = 10000,
        in_layer: str = "rgb",
        out_layer: str = "rgb",
        inverse: bool = False,
        name: str | None = None,
    ) -> dict:
        """Train a CopyCat model. Returns a task_id for async polling.

        Training is multi-hour wall time -- this tool returns
        immediately with ``{task_id, state, ack}`` and the addon
        streams per-epoch ``task_progress`` notifications carrying
        ``{epoch, total_epochs, loss, eta_seconds,
        sample_thumbnail_path}``. Poll ``tasks_get(task_id)`` for the
        live progress snapshot, ``tasks_cancel`` to interrupt.

        Args:
            model_path: output ``.cat`` file path. Written on
                completion. Path-policy gates apply addon-side.
            dataset_dir: directory containing paired exemplar plates.
                The addon discovers ``Input/`` and ``Output/``
                subfolders by convention.
            epochs: total training epochs. Default 10000 is the
                Foundry-recommended floor for production-grade models.
            in_layer: input layer name on the dataset reads. ``rgb``
                covers the common case.
            out_layer: target output layer. Same default.
            inverse: when True, swap the dataset's input/output
                directories at train time. Used for "learn the
                forward problem from inverse exemplars" workflows
                (see ``setup_dehaze_copycat`` for the canonical case).
            name: optional CopyCat node name for the trainer node the
                addon creates. Defaults to an auto-generated name.
        """
        params: dict[str, Any] = {
            "model_path": model_path,
            "dataset_dir": dataset_dir,
            "epochs": int(epochs),
            "in_layer": in_layer,
            "out_layer": out_layer,
            "inverse": bool(inverse),
        }
        if name is not None:
            params["name"] = name
        return _start_async(
            tool="train_copycat",
            addon_command="train_copycat_async",
            params=params,
            progress_fields=_TRAIN_PROGRESS_FIELDS,
        )

    @nuke_tool(ctx, profile="copycat", annotations=BENIGN_NEW)
    @nuke_command("serve_copycat")
    def serve_copycat(
        model_path: str,
        plate: str,
        name: str | None = None,
    ) -> dict:
        """Create an Inference node loaded with a trained ``.cat`` model.

        Synchronous: inference-node creation is a graph mutation that
        finishes in milliseconds, so it returns the standard
        ``NodeRef`` dict immediately. The actual per-frame inference
        cost only materializes when downstream renders the Inference.

        Args:
            model_path: path to a ``.cat`` produced by
                ``train_copycat`` or installed from Cattery.
            plate: input node feeding the Inference's first input.
            name: explicit Inference node name. When the same name
                already exists with the same model + input, the
                existing node is returned (idempotent).
        """
        params: dict[str, Any] = {
            "model_path": model_path,
            "plate": plate,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("serve_copycat", params, "mutate")

    @nuke_tool(ctx, profile="copycat", annotations=BENIGN_NEW)
    @nuke_command("setup_dehaze_copycat")
    def setup_dehaze_copycat(
        haze_exemplars: list[str],
        clean_exemplars: list[str],
        epochs: int = 8000,
        name: str | None = None,
    ) -> dict:
        """Train a dehaze CopyCat using inverse training. Returns task_id.

        The dehaze workflow asks: given a hazy plate, produce a clean
        plate. But hazy/clean exemplar pairs are hard to source for
        real footage. Instead we run *inverse* training: feed the
        network ``clean -> hazy`` pairs (synthetic or DOP-derived) so
        it learns the forward haze model, then at inference time the
        Inference node runs the network *forward* on a hazy input and
        produces the clean estimate.

        Mechanically this means the addon swaps ``in_layer`` /
        ``out_layer`` -- ``in_layer="clean"``, ``out_layer="haze"``
        on the trainer side -- and the resulting ``.cat`` is wired
        normally at inference time. The behavior is implemented by
        calling the same async trainer with ``inverse=True``.

        Args:
            haze_exemplars: list of hazy plate paths.
            clean_exemplars: list of clean plate paths. Must align
                pairwise with ``haze_exemplars``.
            epochs: training epochs. Defaults to 8000 -- dehaze
                models converge faster than full transfer learning.
            name: optional CopyCat node name.
        """
        params: dict[str, Any] = {
            "haze_exemplars": list(haze_exemplars),
            "clean_exemplars": list(clean_exemplars),
            "epochs": int(epochs),
            "in_layer": "clean",
            "out_layer": "haze",
            "inverse": True,
        }
        if name is not None:
            params["name"] = name
        return _start_async(
            tool="setup_dehaze_copycat",
            addon_command="setup_dehaze_copycat_async",
            params=params,
            progress_fields=_TRAIN_PROGRESS_FIELDS,
        )

    @nuke_tool(ctx, profile="copycat", annotations=READ_ONLY)
    @nuke_command("list_cattery_models")
    def list_cattery_models(category: str | None = None) -> dict:
        """List models from the local Cattery cache + remote stub.

        Read-only directory enumeration on the addon side -- the
        Cattery cache lives at ``~/.nuke/cattery/`` by default,
        overridable via ``NUKE_CATTERY_DIR``. When a remote registry
        is configured, the addon merges in pending entries (not yet
        downloaded) flagged with ``installed: false``.

        Args:
            category: optional filter on the model's ``category``
                field (e.g. ``"denoise"``, ``"dehaze"``,
                ``"upres"``). Case-sensitive substring match.
        """
        params: dict[str, Any] = {}
        if category is not None:
            params["category"] = category
        return connection.send("list_cattery_models", _class="read", **params)

    @nuke_tool(ctx, profile="copycat", annotations=BENIGN_NEW)
    @nuke_command("install_cattery_model")
    def install_cattery_model(
        model_id: str,
        name: str | None = None,
    ) -> dict:
        """Download + cache a Cattery model. Returns a task_id.

        Production Cattery models can weigh hundreds of megabytes, so
        the download is Task-wrapped. The addon emits
        ``{bytes_downloaded, total_bytes, step}`` progress lines
        until the file lands in ``~/.nuke/cattery/`` and verifies its
        hash, then a terminal ``completed`` line carrying
        ``{model_path, model_id, sha256}``.

        Args:
            model_id: registry id of the model to install. Match
                ``CatteryModel.id`` returned by
                ``list_cattery_models``.
            name: optional friendly alias for the cached file.
        """
        params: dict[str, Any] = {"model_id": model_id}
        if name is not None:
            params["name"] = name
        return _start_async(
            tool="install_cattery_model",
            addon_command="install_cattery_model_async",
            params=params,
            progress_fields=_INSTALL_PROGRESS_FIELDS,
        )

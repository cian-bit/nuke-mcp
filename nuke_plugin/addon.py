"""Nuke-side socket server. Runs inside Nuke's Python interpreter.

This file gets copied to ~/.nuke/ and loaded via init.py.
It opens a TCP socket and waits for the nuke-mcp server process to connect.
Commands are received as JSON, executed on Nuke's main thread, and results
sent back as JSON.

The key detail: all nuke API calls must go through
nuke.executeInMainThreadWithResult() when called from a non-main thread.
Using executeInMainThread (without Result) is fire-and-forget and will
silently return None. This is the bug in kleer001's implementation.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import pathlib
import socket
import sys
import threading
import traceback
from typing import Any

# A5: crash watchdog. addon.py is loaded two ways:
#   * Inside Nuke as ``nuke_mcp_addon.addon`` (a real package).
#   * Inside tests via ``spec_from_file_location`` with no parent
#     package, so ``from . import _watchdog`` fails.
# Try the package import first; fall back to a sibling-file load so the
# same module instance is shared across both load paths.
try:
    from . import _watchdog  # type: ignore[no-redef]
except ImportError:
    _wd_path = pathlib.Path(__file__).with_name("_watchdog.py")
    _wd_name = "nuke_mcp_addon._watchdog"
    if _wd_name in sys.modules:
        _watchdog = sys.modules[_wd_name]
    else:
        _wd_spec = importlib.util.spec_from_file_location(_wd_name, _wd_path)
        assert _wd_spec is not None and _wd_spec.loader is not None
        _watchdog = importlib.util.module_from_spec(_wd_spec)
        sys.modules[_wd_name] = _watchdog
        _wd_spec.loader.exec_module(_watchdog)

log = logging.getLogger("nuke_mcp.addon")

PORT = int(os.environ.get("NUKE_MCP_PORT", "9876"))
_server_thread: threading.Thread | None = None
_running = False
_server_socket: socket.socket | None = None


def start(port: int = PORT) -> None:
    global _server_thread, _running
    if _running:
        log.warning("server already running")
        return
    _running = True
    _server_thread = threading.Thread(target=_server_loop, args=(port,), daemon=True)
    _server_thread.start()
    log.info("nuke-mcp addon started on port %d", port)


def stop() -> None:
    global _running, _server_socket
    _running = False
    if _server_socket:
        with contextlib.suppress(OSError):
            _server_socket.close()
        _server_socket = None
    log.info("nuke-mcp addon stopped")


def is_running() -> bool:
    return _running


def _enable_keepalive(sock: socket.socket) -> None:
    """Enable TCP keepalive with aggressive per-OS tuning.

    Layered cross-platform: every socket gets SO_KEEPALIVE; per-OS
    tunings are wrapped in try/except so a missing constant on one
    platform doesn't break the others. Surfaces a torn TCP stream
    within a few seconds of a Nuke crash instead of waiting for the
    next handler call to time out.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except (OSError, AttributeError):
        return

    # Linux
    with contextlib.suppress(OSError, AttributeError):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 1)  # type: ignore[attr-defined]
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 1)  # type: ignore[attr-defined]
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)  # type: ignore[attr-defined]

    # Windows
    with contextlib.suppress(OSError, AttributeError):
        sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 1000, 1000))  # type: ignore[attr-defined]

    # macOS
    with contextlib.suppress(OSError, AttributeError):
        tcp_keepalive = getattr(socket, "TCP_KEEPALIVE", 0x10)
        sock.setsockopt(socket.IPPROTO_TCP, tcp_keepalive, 1)


def _server_loop(port: int) -> None:
    global _server_socket
    try:
        _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _enable_keepalive(_server_socket)
        _server_socket.bind(("127.0.0.1", port))
        _server_socket.listen(1)
        _server_socket.settimeout(1.0)

        while _running:
            try:
                client, addr = _server_socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break

            _enable_keepalive(client)
            log.info("client connected from %s", addr)
            _handle_client(client)
            log.info("client disconnected")

    except Exception:
        log.error("server loop error:\n%s", traceback.format_exc())
    finally:
        stop()


def _build_handshake() -> dict:
    import nuke

    variant = "Nuke"
    if nuke.env.get("studio"):
        variant = "NukeStudio"
    elif nuke.env.get("nukex"):
        variant = "NukeX"

    return {
        "nuke_version": nuke.NUKE_VERSION_STRING,
        "variant": variant,
        "pid": os.getpid(),
    }


def _handle_client(client: socket.socket) -> None:
    import nuke

    # handshake must run on main thread
    try:
        handshake = nuke.executeInMainThreadWithResult(_build_handshake)
        _send(client, handshake)
    except Exception:
        log.error("handshake failed:\n%s", traceback.format_exc())
        client.close()
        return

    buf = b""
    while _running:
        try:
            chunk = client.recv(4096)
            if not chunk:
                break
            buf += chunk

            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    _send(client, {"status": "error", "error": "invalid json"})
                    continue

                resp = _dispatch(msg, client=client)
                _send(client, resp)

        except TimeoutError:
            continue
        except OSError:
            break
    # tear down per-socket helper state so a long-lived process
    # doesn't accumulate dict entries across reconnects.
    _drop_send_lock(client)


# B7: per-request node-name -> nuke.Node cache. Stashed on threading.local
# so handlers can call _resolve_node(name) without each one paying its own
# nuke.toNode() lookup. _dispatch resets the cache for every request, so
# a stale cache can't leak across calls.
_request_local = threading.local()


def _resolve_node(name: str) -> Any:
    """Look up ``name`` via the per-request cache, falling back to ``nuke.toNode``.

    Cache is set up by ``_dispatch`` for the duration of a single request.
    If no cache is active (e.g. handler called directly from a test), this
    falls through to ``nuke.toNode`` with no caching.
    """
    import nuke

    cache: dict[str, Any] | None = getattr(_request_local, "node_cache", None)
    if cache is None:
        return nuke.toNode(name)
    if name in cache:
        return cache[name]
    node = nuke.toNode(name)
    cache[name] = node
    return node


def _dispatch(msg: dict[str, Any], client: socket.socket | None = None) -> dict[str, Any]:
    """Route a command to the right handler, executed on Nuke's main thread.

    A2: echoes ``_request_id`` from the top-level payload back in the
    response so the MCP-side ``send()`` can assert round-trip identity.
    The id lives at the payload root, not inside ``params``.

    B7: installs a fresh per-request ``node_cache`` on ``_request_local``
    so handlers that touch the same node twice (or that participate in
    batch operations) avoid redundant ``nuke.toNode`` calls.

    B2: ``render_async`` is special-cased -- it must NOT block on
    ``executeInMainThreadWithResult`` because the whole point is to
    return a task_id immediately and let a background worker emit
    ``task_progress`` lines on the same socket.
    """
    cmd = msg.get("type", "")
    params = msg.get("params", {})
    rid = msg.get("_request_id")

    if cmd == "ping":
        resp = {"status": "ok", "result": {"pong": True}}
        if rid is not None:
            resp["_request_id"] = rid
        return resp

    # B2: async commands return immediately after spawning a worker.
    # Each entry maps an ``*_async`` command to a starter that records
    # the task in ``_active_renders`` and spawns a background thread
    # which emits ``task_progress`` notifications on the live socket.
    # C4 widens this from a single ``render_async`` special-case to a
    # registry so distortion / smartvector tasks can plug in.
    async_starter = ASYNC_HANDLERS.get(cmd)
    if async_starter is not None:
        if client is None:
            resp = {
                "status": "error",
                "error": f"{cmd} requires the client socket",
                "error_class": "ValueError",
            }
            if rid is not None:
                resp["_request_id"] = rid
            return resp
        try:
            result = async_starter(params, client)
            resp = {"status": "ok", "result": result}
        except Exception as e:
            resp = {
                "status": "error",
                "error": str(e),
                "error_class": type(e).__name__,
                "traceback": traceback.format_exc(),
            }
            _watchdog.record_failure(cmd, rid, e)
        if rid is not None:
            resp["_request_id"] = rid
        return resp

    if cmd == "cancel_render":
        task_id = params.get("task_id")
        cancelled = bool(task_id) and _cancel_active_render(str(task_id))
        resp = {"status": "ok", "result": {"cancelled": cancelled, "task_id": task_id}}
        if rid is not None:
            resp["_request_id"] = rid
        return resp

    if cmd == "cancel_copycat":
        task_id = params.get("task_id")
        cancelled = bool(task_id) and _cancel_copycat_task(str(task_id))
        resp = {"status": "ok", "result": {"cancelled": cancelled, "task_id": task_id}}
        if rid is not None:
            resp["_request_id"] = rid
        return resp

    if cmd == "cancel_install":
        task_id = params.get("task_id")
        cancelled = bool(task_id) and _cancel_install_task(str(task_id))
        resp = {"status": "ok", "result": {"cancelled": cancelled, "task_id": task_id}}
        if rid is not None:
            resp["_request_id"] = rid
        return resp

    # build the code string to execute on the main thread
    handler = HANDLERS.get(cmd)
    if handler is None:
        resp = {"status": "error", "error": f"unknown command: {cmd}"}
        if rid is not None:
            resp["_request_id"] = rid
        return resp

    _request_local.node_cache = {}
    try:
        import nuke

        result = nuke.executeInMainThreadWithResult(handler, args=(params,))
        resp = {"status": "ok", "result": result}
        if rid is not None:
            resp["_request_id"] = rid
        _watchdog.record_success()
        return resp
    except Exception as e:
        resp = {
            "status": "error",
            "error": str(e),
            "error_class": type(e).__name__,
            "traceback": traceback.format_exc(),
        }
        if rid is not None:
            resp["_request_id"] = rid
        _watchdog.record_failure(cmd, rid, e)
        return resp
    finally:
        _request_local.node_cache = None


def _json_safe(obj: Any) -> Any:
    """Make any Python value JSON-serializable."""
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    return str(obj)


# Per-socket write lock. The B2 async-render worker thread calls
# ``_send`` from a background thread to emit ``task_progress`` lines
# while the main loop may also be sending a response. ``sendall`` is
# atomic for one buffer but two interleaved calls could fragment a
# frame -- the lock keeps each call's bytes contiguous.
_send_locks: dict[int, threading.Lock] = {}
_send_locks_guard = threading.Lock()


def _get_send_lock(sock: socket.socket) -> threading.Lock:
    """Lazy per-socket write lock keyed on ``id(sock)``."""
    with _send_locks_guard:
        lock = _send_locks.get(id(sock))
        if lock is None:
            lock = threading.Lock()
            _send_locks[id(sock)] = lock
        return lock


def _drop_send_lock(sock: socket.socket) -> None:
    with _send_locks_guard:
        _send_locks.pop(id(sock), None)


def _send(sock: socket.socket, data: dict) -> None:
    payload = json.dumps(_json_safe(data), separators=(",", ":")).encode("utf-8")
    with _get_send_lock(sock):
        sock.sendall(payload + b"\n")


# B2: active async-render registry. Keyed by task_id, value is a stop
# event the worker checks between frames. ``cancel_render`` sets the
# event; the worker exits cleanly and writes a final ``cancelled``
# notification before tearing down.
_active_renders: dict[str, threading.Event] = {}
_active_renders_guard = threading.Lock()


def _register_render(task_id: str) -> threading.Event:
    stop = threading.Event()
    with _active_renders_guard:
        _active_renders[task_id] = stop
    return stop


def _unregister_render(task_id: str) -> None:
    with _active_renders_guard:
        _active_renders.pop(task_id, None)


def _cancel_active_render(task_id: str) -> bool:
    """Signal the worker for ``task_id`` to stop. Returns True if found.

    Looks in both the render registry (``_active_renders``) and the
    C4 distortion registry (``_active_distortion_tasks``) so a single
    ``cancel_render`` covers SmartVector / STMap workers alongside the
    original Write render workers without duplicating the entry point.
    """
    with _active_renders_guard:
        stop = _active_renders.get(task_id)
    if stop is not None:
        stop.set()
        return True
    # The dict won't be defined yet during initial module import (the
    # distortion handlers are declared later in the file). Look it up
    # via globals so the import-order dependency is explicit.
    distortion_dict = globals().get("_active_distortion_tasks")
    distortion_lock = globals().get("_active_distortion_tasks_guard")
    if distortion_dict is not None and distortion_lock is not None:
        with distortion_lock:
            stop = distortion_dict.get(task_id)
        if stop is not None:
            stop.set()
            return True
    return False


_active_copycat_tasks: dict[str, threading.Event] = {}
_active_copycat_tasks_guard = threading.Lock()
_active_install_tasks: dict[str, threading.Event] = {}
_active_install_tasks_guard = threading.Lock()


def _register_copycat_task(task_id: str) -> threading.Event:
    stop = threading.Event()
    with _active_copycat_tasks_guard:
        _active_copycat_tasks[task_id] = stop
    return stop


def _unregister_copycat_task(task_id: str) -> None:
    with _active_copycat_tasks_guard:
        _active_copycat_tasks.pop(task_id, None)


def _register_install_task(task_id: str) -> threading.Event:
    stop = threading.Event()
    with _active_install_tasks_guard:
        _active_install_tasks[task_id] = stop
    return stop


def _unregister_install_task(task_id: str) -> None:
    with _active_install_tasks_guard:
        _active_install_tasks.pop(task_id, None)


def _cancel_copycat_task(task_id: str) -> bool:
    with _active_copycat_tasks_guard:
        stop = _active_copycat_tasks.get(task_id)
    if stop is None:
        return False
    stop.set()
    return True


def _cancel_install_task(task_id: str) -> bool:
    with _active_install_tasks_guard:
        stop = _active_install_tasks.get(task_id)
    if stop is None:
        return False
    stop.set()
    return True


# knobs to skip in output -- ui-only, never useful for comp analysis
_SKIP_KNOBS = frozenset(
    {
        "selected",
        "xpos",
        "ypos",
        "postage_stamp",
        "postage_stamp_frame",
        "hide_input",
        "tile_color",
        "gl_color",
        "cached",
        "dope_sheet",
        "note_font",
        "note_font_size",
        "note_font_color",
        "bookmark",
        "indicators",
        "icon",
        "process_mask",
        "panel",
        "lifetimeStart",
        "lifetimeEnd",
        "useLifetime",
    }
)

CLASS_ALIASES: dict[str, str] = {
    "Checkerboard": "CheckerBoard2",
    "checkerboard": "CheckerBoard2",
    "CheckerBoard": "CheckerBoard2",
}

# -- command handlers --
# each takes a params dict and returns a result dict
# these run on Nuke's main thread via executeInMainThreadWithResult


def _handle_get_script_info(params: dict) -> dict:
    import nuke

    root = nuke.root()
    return {
        "script": nuke.root().name(),
        "first_frame": int(root["first_frame"].value()),
        "last_frame": int(root["last_frame"].value()),
        "fps": root["fps"].value(),
        "format": root.format().name(),
        "colorspace": root["colorManagement"].value() if root.knob("colorManagement") else "",
        "node_count": len(nuke.allNodes()),
    }


def _handle_get_node_info(params: dict) -> dict:
    import nuke

    name = params["name"]
    node = nuke.toNode(name)
    if node is None:
        raise ValueError(f"node not found: {name}")

    inputs = []
    for i in range(node.inputs()):
        inp = node.input(i)
        inputs.append(inp.name() if inp else None)

    knobs = {}
    for k in node.knobs():
        if k in _SKIP_KNOBS:
            continue
        knob = node.knob(k)
        if (
            knob.isAnimated()
            or knob.hasExpression()
            or (hasattr(knob, "isDefault") and not knob.isDefault())
        ):
            try:
                knobs[k] = knob.value()
            except Exception:
                knobs[k] = "<unreadable>"

    return {
        "name": node.name(),
        "type": node.Class(),
        "inputs": inputs,
        "knobs": knobs,
        "error": node.hasError(),
        "warning": bool(node.warnings()) if hasattr(node, "warnings") else False,
        "x": node.xpos(),
        "y": node.ypos(),
    }


def _handle_create_node(params: dict) -> dict:
    import nuke

    node_type = CLASS_ALIASES.get(params["type"], params["type"])
    name = params.get("name")
    position = params.get("position")
    connect_to = params.get("connect_to")
    knobs_to_set = params.get("knobs", {})

    node = getattr(nuke.nodes, node_type)()
    if name:
        node.setName(name)
    if position:
        node.setXYpos(int(position[0]), int(position[1]))
    if connect_to:
        src = nuke.toNode(connect_to)
        if src:
            node.setInput(0, src)

    for k, v in knobs_to_set.items():
        knob = node.knob(k)
        if knob:
            knob.setValue(v)

    return {
        "name": node.name(),
        "type": node.Class(),
        "x": node.xpos(),
        "y": node.ypos(),
    }


def _handle_delete_node(params: dict) -> dict:
    import nuke

    name = params["name"]
    node = nuke.toNode(name)
    if node is None:
        raise ValueError(f"node not found: {name}")
    nuke.delete(node)
    return {"deleted": name}


def _handle_modify_node(params: dict) -> dict:
    import nuke

    name = params["name"]
    node = nuke.toNode(name)
    if node is None:
        raise ValueError(f"node not found: {name}")

    knobs = params.get("knobs", {})
    position = params.get("position")
    new_name = params.get("new_name")
    update_expressions = params.get("update_expressions", False)

    for k, v in knobs.items():
        knob = node.knob(k)
        if knob:
            knob.setValue(v)

    if position:
        node.setXYpos(int(position[0]), int(position[1]))

    broken_expressions: list[str] = []
    if new_name:
        # scan for expression references before renaming
        old_name = name
        for n in nuke.allNodes():
            for k in n.knobs():
                knob = n.knob(k)
                if knob.hasExpression():
                    expr = knob.expression()
                    if old_name in expr:
                        broken_expressions.append(f"{n.name()}.{k}")
                        if update_expressions:
                            knob.setExpression(expr.replace(old_name, new_name))

        node.setName(new_name)

    result: dict[str, Any] = {"name": node.name(), "type": node.Class()}
    if broken_expressions:
        if update_expressions:
            result["updated_expressions"] = broken_expressions
        else:
            result["broken_expressions"] = broken_expressions
    return result


def _handle_connect_nodes(params: dict) -> dict:
    import nuke

    src_name = params["from"]
    dst_name = params["to"]
    input_idx = params.get("input", 0)

    src = nuke.toNode(src_name)
    dst = nuke.toNode(dst_name)
    if src is None:
        raise ValueError(f"source node not found: {src_name}")
    if dst is None:
        raise ValueError(f"target node not found: {dst_name}")

    # merge nodes: default to B pipe (input 1) unless specified
    if dst.Class().startswith("Merge") and input_idx == 0 and "input" not in params:
        input_idx = 1

    dst.setInput(input_idx, src)
    return {"connected": f"{src_name} -> {dst_name}[{input_idx}]"}


def _handle_find_nodes(params: dict) -> dict:
    import nuke

    node_type = params.get("type")
    pattern = params.get("pattern")
    errors_only = params.get("errors_only", False)

    nodes = nuke.allNodes()
    results = []

    for n in nodes:
        if node_type and n.Class() != node_type:
            continue
        if pattern and pattern.lower() not in n.name().lower():
            continue
        if errors_only and not n.hasError():
            continue
        results.append(
            {
                "name": n.name(),
                "type": n.Class(),
                "error": n.hasError(),
            }
        )

    return {"nodes": results, "count": len(results)}


def _handle_list_nodes(params: dict) -> dict:
    import nuke

    root = params.get("root")
    if root:
        parent = nuke.toNode(root)
        if parent is None:
            raise ValueError(f"root node not found: {root}")
        nodes = parent.nodes() if hasattr(parent, "nodes") else []
    else:
        nodes = nuke.allNodes()

    return {
        "nodes": [{"name": n.name(), "type": n.Class()} for n in nodes],
        "count": len(nodes),
    }


def _handle_get_knob(params: dict) -> dict:
    import nuke

    node = nuke.toNode(params["node"])
    if node is None:
        raise ValueError(f"node not found: {params['node']}")

    knob = node.knob(params["knob"])
    if knob is None:
        raise ValueError(f"knob not found: {params['knob']} on {params['node']}")

    result = {
        "value": knob.value(),
        "type": type(knob).__name__,
        "animated": knob.isAnimated(),
        "default": knob.isDefault() if hasattr(knob, "isDefault") else False,
    }
    if knob.hasExpression():
        result["expression"] = knob.expression()

    return result


def _handle_set_knob(params: dict) -> dict:
    import nuke

    node = nuke.toNode(params["node"])
    if node is None:
        raise ValueError(f"node not found: {params['node']}")

    knob = node.knob(params["knob"])
    if knob is None:
        raise ValueError(f"knob not found: {params['knob']} on {params['node']}")

    value = params["value"]

    # expand scalar to multi-value if knob is multi-dimensional
    if isinstance(value, int | float) and hasattr(knob, "dimensions") and knob.dimensions() > 1:
        value = [float(value)] * knob.dimensions()

    # handle list/array values for multi-value knobs
    if isinstance(value, list):
        for i, v in enumerate(value):
            knob.setValue(float(v), i)
    else:
        knob.setValue(value)

    return {"node": node.name(), "knob": params["knob"], "value": knob.value()}


def _handle_auto_layout(params: dict) -> dict:
    import nuke

    selected = params.get("selected_only", False)
    nodes = nuke.selectedNodes() if selected else nuke.allNodes()

    if not nodes:
        return {"laid_out": 0}

    nuke.autoplace_all() if not selected else nuke.autoplace_snap_selected()
    return {"laid_out": len(nodes)}


def _handle_read_comp(params: dict) -> dict:
    """Serialize the node graph for Claude to read."""
    import nuke

    root_name = params.get("root")
    depth = params.get("depth", 999)
    summary = params.get("summary", False)
    node_type = params.get("type")
    offset = params.get("offset", 0)
    limit = params.get("limit", 0)  # 0 = no limit

    if root_name:
        root_node = nuke.toNode(root_name)
        if root_node is None:
            raise ValueError(f"root node not found: {root_name}")
        nodes = root_node.nodes() if hasattr(root_node, "nodes") else [root_node]
    else:
        nodes = nuke.allNodes()

    # filter by type if requested
    if node_type:
        nodes = [n for n in nodes if n.Class() == node_type]

    total = len(nodes)

    # paginate
    if offset:
        nodes = nodes[offset:]
    if limit:
        nodes = nodes[:limit]

    # B7: single-pass knob iteration. The previous implementation walked
    # ``n.knobs()`` once for changed values and a second time for
    # expressions. Now we collect both in one pass per node.
    result = []
    for n in nodes:
        try:
            entry: dict[str, Any] = {
                "name": n.name(),
                "type": n.Class(),
            }

            # inputs
            inputs = []
            for i in range(n.inputs()):
                inp = n.input(i)
                inputs.append(inp.name() if inp else None)
            if any(inputs):
                entry["inputs"] = inputs

            if n.hasError():
                entry["error"] = True

            # summary mode: skip knobs and expressions to save tokens
            if not summary:
                changed: dict[str, Any] = {}
                exprs: dict[str, str] = {}
                for k in n.knobs():
                    if k in _SKIP_KNOBS:
                        continue
                    knob = n.knob(k)
                    # Single-pass: capture expressions and non-default values
                    # in the same iteration.
                    try:
                        has_expr = knob.hasExpression()
                    except Exception:
                        has_expr = False
                    if has_expr:
                        with contextlib.suppress(Exception):
                            exprs[k] = knob.expression()
                    try:
                        is_relevant = (
                            knob.isAnimated()
                            or has_expr
                            or (hasattr(knob, "isDefault") and not knob.isDefault())
                        )
                    except Exception:
                        continue
                    if is_relevant:
                        try:
                            val = knob.value()
                            if isinstance(val, str) and len(val) > 500:
                                changed[k] = f"<{len(val)} chars>"
                            elif isinstance(val, int | float | str | bool | list | tuple):
                                changed[k] = val
                        except Exception:
                            changed[k] = "<unreadable>"
                if changed:
                    entry["knobs"] = changed
                if exprs:
                    entry["expressions"] = exprs

                # group internals (one level only to save tokens)
                if hasattr(n, "nodes") and depth > 0:
                    try:
                        children = n.nodes()
                        if children:
                            entry["children"] = [
                                {"name": c.name(), "type": c.Class()} for c in children
                            ]
                    except Exception:
                        pass

            result.append(entry)
        except Exception:
            # absolute fallback: if even name()/Class() fails, skip the node
            result.append({"name": "<error>", "type": "<error>", "error": True})

    resp: dict[str, Any] = {"nodes": result, "count": len(result), "total": total}
    if offset or limit:
        resp["offset"] = offset
        resp["limit"] = limit
    return resp


def _handle_read_selected(params: dict) -> dict:
    import nuke

    nodes = nuke.selectedNodes()
    if not nodes:
        return {"nodes": [], "count": 0}

    result = []
    for n in nodes:
        try:
            entry = {"name": n.name(), "type": n.Class()}

            inputs = []
            for i in range(n.inputs()):
                inp = n.input(i)
                inputs.append(inp.name() if inp else None)
            if any(inputs):
                entry["inputs"] = inputs

            changed: dict[str, Any] = {}
            for k in n.knobs():
                if k in _SKIP_KNOBS:
                    continue
                knob = n.knob(k)
                try:
                    is_relevant = (
                        knob.isAnimated()
                        or knob.hasExpression()
                        or (hasattr(knob, "isDefault") and not knob.isDefault())
                    )
                except Exception:
                    continue
                if is_relevant:
                    try:
                        val = knob.value()
                        if isinstance(val, str) and len(val) > 500:
                            changed[k] = f"<{len(val)} chars>"
                        elif isinstance(val, int | float | str | bool | list | tuple):
                            changed[k] = val
                    except Exception:
                        changed[k] = "<unreadable>"
            if changed:
                entry["knobs"] = changed

            if n.hasError():
                entry["error"] = True

            result.append(entry)
        except Exception:
            result.append({"name": "<error>", "type": "<error>", "error": True})

    return {"nodes": result, "count": len(result)}


def _handle_execute_python(params: dict) -> dict:
    import nuke

    code = params["code"]

    # block dangerous operations
    dangerous = [
        "os.remove",
        "os.rmdir",
        "shutil.rmtree",
        "subprocess",
        "sys.exit",
        "nuke.scriptClose",
        "nuke.scriptClear",
    ]
    for pattern in dangerous:
        if pattern in code:
            raise ValueError(f"blocked: code contains '{pattern}'")

    result: dict[str, Any] = {}
    exec_globals: dict[str, Any] = {"nuke": nuke, "__result__": result}
    exec(code, exec_globals)
    out = exec_globals.get("__result__", {})
    # always return a dict -- wrap primitives
    if not isinstance(out, dict):
        return {"result": out}
    return out


def _handle_render(params: dict) -> dict:
    import nuke

    write_name = params.get("write_node")
    frame_range = params.get("frame_range")

    if write_name:
        node = nuke.toNode(write_name)
        if node is None:
            raise ValueError(f"write node not found: {write_name}")
    else:
        writes = [n for n in nuke.allNodes("Write")]
        if not writes:
            raise ValueError("no Write nodes in script")
        node = writes[0]

    if frame_range:
        first, last = frame_range
    else:
        first = int(nuke.root()["first_frame"].value())
        last = int(nuke.root()["last_frame"].value())

    nuke.execute(node, first, last)
    return {"rendered": node.name(), "frames": [first, last]}


# B2: async render. The worker thread iterates frames, calls
# ``nuke.executeInMainThreadWithResult`` per frame so each frame stays
# on the main thread, and emits ``task_progress`` notifications on
# the live client socket between frames. A stop event registered at
# spawn time lets ``cancel_render`` short-circuit the loop without
# killing the thread (Python lacks safe thread-kill -- cooperative
# cancellation is the only sane path).
def _start_render_async(params: dict, client: socket.socket) -> dict:
    """Validate args, register the task, spawn the worker. Returns
    immediately with the task_id so the caller can poll task status.
    """
    task_id = params.get("task_id")
    if not task_id:
        raise ValueError("render_async requires task_id")
    write_name = params.get("write_node")
    frame_range = params.get("frame_range")

    # Resolve frame range now (still on main thread via dispatch
    # caller's executeInMainThreadWithResult... wait, no: render_async
    # bypasses the main-thread dispatch path. We do the resolve inside
    # the worker so any ``nuke.root()`` access stays on the main
    # thread via executeInMainThreadWithResult.

    stop_event = _register_render(str(task_id))
    thread = threading.Thread(
        target=_render_worker,
        args=(str(task_id), write_name, frame_range, client, stop_event),
        name=f"nuke-mcp-render-{task_id}",
        daemon=True,
    )
    thread.start()
    return {"task_id": str(task_id), "started": True}


def _render_worker(
    task_id: str,
    write_name: str | None,
    frame_range: list[int] | None,
    client: socket.socket,
    stop_event: threading.Event,
) -> None:
    """Background render loop.

    Runs each frame's execute() through ``executeInMainThreadWithResult``
    so Nuke's own main-thread invariant holds. After each frame emits a
    ``task_progress`` notification; emits a final notification with
    ``state`` set to ``completed`` / ``failed`` / ``cancelled`` so the
    MCP-side listener can flip the TaskStore record without polling.
    """
    import nuke

    def _resolve() -> tuple[Any, int, int]:
        if write_name:
            node = nuke.toNode(write_name)
            if node is None:
                raise ValueError(f"write node not found: {write_name}")
        else:
            writes = list(nuke.allNodes("Write"))
            if not writes:
                raise ValueError("no Write nodes in script")
            node = writes[0]
        if frame_range:
            first, last = int(frame_range[0]), int(frame_range[1])
        else:
            first = int(nuke.root()["first_frame"].value())
            last = int(nuke.root()["last_frame"].value())
        return node, first, last

    def _emit(payload: dict) -> None:
        # Best-effort: a write failure means the client died; the
        # worker exits next iteration when the addon notices the
        # closed socket. No retry -- progress lines are lossy by
        # design.
        try:
            _send(client, payload)
        except OSError as exc:
            log.warning("render_worker emit failed (task=%s): %s", task_id, exc)

    try:
        node, first, last = nuke.executeInMainThreadWithResult(_resolve)
        node_name = nuke.executeInMainThreadWithResult(node.name)
        total = last - first + 1
        for offset, frame in enumerate(range(first, last + 1), start=1):
            if stop_event.is_set():
                _emit(
                    {
                        "type": "task_progress",
                        "id": task_id,
                        "state": "cancelled",
                        "frame": frame - 1 if frame > first else first,
                        "total": total,
                    }
                )
                return
            nuke.executeInMainThreadWithResult(nuke.execute, args=(node, frame, frame))
            _emit(
                {
                    "type": "task_progress",
                    "id": task_id,
                    "state": "working",
                    "frame": frame,
                    "total": total,
                    "progress": offset,
                }
            )
        _emit(
            {
                "type": "task_progress",
                "id": task_id,
                "state": "completed",
                "frame": last,
                "total": total,
                "result": {"rendered": node_name, "frames": [first, last]},
            }
        )
    except Exception as e:
        log.exception("render_worker failed (task=%s)", task_id)
        _emit(
            {
                "type": "task_progress",
                "id": task_id,
                "state": "failed",
                "error": {
                    "error_class": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                },
            }
        )
    finally:
        _unregister_render(task_id)


def _handle_save_script(params: dict) -> dict:
    import nuke

    path = params.get("path")
    if path:
        nuke.scriptSaveAs(path)
    else:
        nuke.scriptSave()
    return {"saved": nuke.root().name()}


def _handle_load_script(params: dict) -> dict:
    import nuke

    path = params["path"]
    nuke.scriptOpen(path)
    return {"loaded": path}


def _handle_set_frame_range(params: dict) -> dict:
    import nuke

    root = nuke.root()
    if "first" in params:
        root["first_frame"].setValue(params["first"])
    if "last" in params:
        root["last_frame"].setValue(params["last"])
    if "current" in params:
        nuke.frame(params["current"])
    return {
        "first": int(root["first_frame"].value()),
        "last": int(root["last_frame"].value()),
    }


def _handle_view_node(params: dict) -> dict:
    import nuke

    name = params["node"]
    node = nuke.toNode(name)
    if node is None:
        raise ValueError(f"node not found: {name}")

    viewer = nuke.activeViewer()
    if viewer:
        viewer.node().setInput(0, node)
    else:
        nuke.show(node)

    return {"viewing": name}


def _handle_set_expression(params: dict) -> dict:
    import nuke

    node = nuke.toNode(params["node"])
    if node is None:
        raise ValueError(f"node not found: {params['node']}")
    knob = node.knob(params["knob"])
    if knob is None:
        raise ValueError(f"knob not found: {params['knob']}")
    knob.setExpression(params["expression"])
    return {"node": node.name(), "knob": params["knob"], "expression": params["expression"]}


def _handle_clear_expression(params: dict) -> dict:
    import nuke

    node = nuke.toNode(params["node"])
    if node is None:
        raise ValueError(f"node not found: {params['node']}")
    knob = node.knob(params["knob"])
    if knob is None:
        raise ValueError(f"knob not found: {params['knob']}")
    knob.clearAnimated()
    return {"node": node.name(), "cleared": params["knob"]}


def _handle_set_keyframe(params: dict) -> dict:
    import nuke

    node = nuke.toNode(params["node"])
    if node is None:
        raise ValueError(f"node not found: {params['node']}")
    knob = node.knob(params["knob"])
    if knob is None:
        raise ValueError(f"knob not found: {params['knob']}")
    if not knob.isAnimated():
        knob.setAnimated()
    knob.setValueAt(params["value"], params["frame"])
    return {
        "node": node.name(),
        "knob": params["knob"],
        "frame": params["frame"],
        "value": params["value"],
    }


def _handle_list_keyframes(params: dict) -> dict:
    import nuke

    node = nuke.toNode(params["node"])
    if node is None:
        raise ValueError(f"node not found: {params['node']}")
    knob = node.knob(params["knob"])
    if knob is None:
        raise ValueError(f"knob not found: {params['knob']}")

    keyframes = []
    if knob.isAnimated():
        for key in knob.animations()[0].keys():  # noqa: SIM118 -- nuke API, not dict
            keyframes.append({"frame": key.x, "value": key.y})
    return {"node": node.name(), "knob": params["knob"], "keyframes": keyframes}


# server-side snapshot storage for diff_comp
_snapshots: dict[str, dict] = {}
_snapshot_counter = 0


def _handle_snapshot_comp(params: dict) -> dict:
    """Take a snapshot of current comp state. Returns a small ID."""
    global _snapshot_counter
    _snapshot_counter += 1
    snap_id = str(_snapshot_counter)
    _snapshots[snap_id] = _handle_read_comp({})
    # keep max 5 snapshots to avoid memory bloat
    if len(_snapshots) > 5:
        oldest = min(_snapshots.keys(), key=int)
        del _snapshots[oldest]
    return {"snapshot_id": snap_id, "node_count": _snapshots[snap_id]["count"]}


def _handle_diff_comp(params: dict) -> dict:
    """Compare current comp to a stored snapshot."""
    snap_id = params.get("snapshot_id")
    if not snap_id or snap_id not in _snapshots:
        available = list(_snapshots.keys())
        return {"status": "error", "error": f"snapshot not found. available: {available}"}

    before = _snapshots[snap_id]
    current = _handle_read_comp({})

    before_nodes = {n["name"]: n for n in before.get("nodes", [])}
    current_nodes = {n["name"]: n for n in current.get("nodes", [])}

    added = [
        {"name": n["name"], "type": n["type"]}
        for name, n in current_nodes.items()
        if name not in before_nodes
    ]
    removed = [
        {"name": n["name"], "type": n["type"]}
        for name, n in before_nodes.items()
        if name not in current_nodes
    ]

    changed = []
    for name in set(before_nodes) & set(current_nodes):
        b, c = before_nodes[name], current_nodes[name]
        diffs = {}
        if b.get("inputs") != c.get("inputs"):
            diffs["inputs"] = {"before": b.get("inputs"), "after": c.get("inputs")}
        bk, ck = b.get("knobs", {}), c.get("knobs", {})
        if bk != ck:
            knob_changes = {}
            for k in set(bk) | set(ck):
                if bk.get(k) != ck.get(k):
                    knob_changes[k] = {"before": bk.get(k), "after": ck.get(k)}
            if knob_changes:
                diffs["knobs"] = knob_changes
        if diffs:
            diffs["name"] = name
            changed.append(diffs)

    return {"added": added, "removed": removed, "changed": changed}


def _handle_list_channels(params: dict) -> dict:
    import nuke

    name = params["node"]
    node = nuke.toNode(name)
    if node is None:
        raise ValueError(f"node not found: {name}")

    channels = node.channels()
    # group by layer
    layers: dict[str, list[str]] = {}
    for ch in channels:
        parts = ch.split(".")
        layer = parts[0] if len(parts) > 1 else "main"
        channel = parts[-1]
        layers.setdefault(layer, []).append(channel)

    return {"layers": layers}


def _handle_create_nodes(params: dict) -> dict:
    """Batch create multiple nodes in one call."""
    import nuke

    specs = params.get("nodes", [])
    created = []
    for spec in specs:
        ntype = CLASS_ALIASES.get(spec["type"], spec["type"])
        node = getattr(nuke.nodes, ntype)()
        if spec.get("name"):
            node.setName(spec["name"])
        if spec.get("connect_to"):
            src = nuke.toNode(spec["connect_to"])
            if src:
                node.setInput(0, src)
        for k, v in spec.get("knobs", {}).items():
            knob = node.knob(k)
            if knob:
                knob.setValue(v)
        created.append({"name": node.name(), "type": node.Class()})
    return {"nodes": created, "count": len(created)}


def _handle_set_knobs(params: dict) -> dict:
    """Batch set multiple knobs across one or more nodes."""
    import nuke

    ops = params.get("operations", [])
    results = []
    for op in ops:
        node = nuke.toNode(op["node"])
        if node is None:
            results.append({"node": op["node"], "error": "not found"})
            continue
        knob = node.knob(op["knob"])
        if knob is None:
            results.append({"node": op["node"], "knob": op["knob"], "error": "knob not found"})
            continue
        value = op["value"]
        if isinstance(value, int | float) and hasattr(knob, "dimensions") and knob.dimensions() > 1:
            value = [float(value)] * knob.dimensions()
        if isinstance(value, list):
            for i, v in enumerate(value):
                knob.setValue(float(v), i)
        else:
            knob.setValue(value)
        results.append({"node": node.name(), "knob": op["knob"], "value": knob.value()})
    return {"results": results, "count": len(results)}


def _handle_disconnect_input(params: dict) -> dict:
    """Disconnect a specific input on a node."""
    import nuke

    name = params["node"]
    node = nuke.toNode(name)
    if node is None:
        raise ValueError(f"node not found: {name}")
    input_idx = params.get("input", 0)
    old_input = node.input(input_idx)
    old_name = old_input.name() if old_input else None
    node.setInput(input_idx, None)
    return {"node": name, "input": input_idx, "disconnected": old_name}


def _handle_set_node_position(params: dict) -> dict:
    """Set x/y position of one or more nodes in the DAG."""
    import nuke

    positions = params.get("positions", [])
    results = []
    for pos in positions:
        node = nuke.toNode(pos["node"])
        if node is None:
            results.append({"node": pos["node"], "error": "not found"})
            continue
        node.setXYpos(int(pos["x"]), int(pos["y"]))
        results.append({"node": node.name(), "x": node.xpos(), "y": node.ypos()})
    return {"results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# A3: typed comp/render handlers
#
# These replace the f-string ``execute_python`` blobs that ``comp.py`` and
# ``render.py:setup_write`` shipped to the addon. Each handler validates
# its inputs (operation allowlists, path traversal) and raises ValueError
# on bad input -- ``_dispatch`` formats that into the structured error
# envelope.
# ---------------------------------------------------------------------------

# Allowlists -- operations that map to a Nuke node class. Anything
# outside the set raises ``invalid operation`` so a caller can't drive
# arbitrary ``getattr(nuke.nodes, X)`` lookups.
_COLOR_OPERATIONS = frozenset({"Grade", "ColorCorrect", "HueCorrect", "OCIOColorSpace"})
_MERGE_OPERATIONS = frozenset(
    {
        "over",
        "plus",
        "multiply",
        "screen",
        "stencil",
        "mask",
        "minus",
        "difference",
        "divide",
        "from",
        "copy",
    }
)
_TRANSFORM_OPERATIONS = frozenset({"Transform", "CornerPin2D", "Reformat", "Tracker4"})
_KEYER_TYPES = frozenset({"Keylight", "Primatte", "IBKGizmo", "Cryptomatte"})
_WRITE_FILE_TYPES = frozenset({"exr", "tiff", "tif", "png", "jpeg", "jpg", "mov", "dpx"})


def _handle_setup_keying(params: dict) -> dict:
    """Build the standard keying chain: keyer + FilterErode + EdgeBlur + Premult.

    A3 typed: replaces ``comp.py``'s f-string ``execute_python`` payload.
    Looks up ``input_node`` via the per-request node cache.
    """
    import nuke

    input_node = params["input_node"]
    keyer_type = params.get("keyer_type", "Keylight")

    if keyer_type not in _KEYER_TYPES:
        raise ValueError(f"invalid keyer_type: {keyer_type}")

    src = _resolve_node(input_node)
    if src is None:
        raise ValueError(f"node not found: {input_node}")

    # TODO(A3-followup): idempotency -- detect existing keyer chain
    # downstream of ``input_node`` of the same ``keyer_type`` and return
    # that instead of creating a duplicate. Skipped here to avoid an
    # over-engineered first pass; the IDEMPOTENT annotation is honest
    # only when the caller hasn't run the tool yet.

    x, y = src.xpos(), src.ypos()

    keyer = getattr(nuke.nodes, keyer_type)()
    keyer.setInput(0, src)
    keyer.setXYpos(x, y + 60)

    erode = nuke.nodes.FilterErode()
    erode.setInput(0, keyer)
    if erode.knob("channels"):
        erode["channels"].setValue("alpha")
    if erode.knob("size"):
        erode["size"].setValue(-0.5)
    erode.setXYpos(x, y + 120)

    edge = nuke.nodes.EdgeBlur()
    edge.setInput(0, erode)
    if edge.knob("size"):
        edge["size"].setValue(3)
    edge.setXYpos(x, y + 180)

    premult = nuke.nodes.Premult()
    premult.setInput(0, edge)
    premult.setXYpos(x, y + 240)

    return {
        "keyer": keyer.name(),
        "erode": erode.name(),
        "edge_blur": edge.name(),
        "premult": premult.name(),
        "tip": "adjust the keyer node settings and erode size to refine the matte",
    }


def _handle_setup_color_correction(params: dict) -> dict:
    """Create a color-correction node downstream of ``input_node``."""
    import nuke

    input_node = params["input_node"]
    operation = params.get("operation", "Grade")

    if operation not in _COLOR_OPERATIONS:
        raise ValueError(f"invalid operation: {operation}")

    src = _resolve_node(input_node)
    if src is None:
        raise ValueError(f"node not found: {input_node}")

    cc = getattr(nuke.nodes, operation)()
    cc.setInput(0, src)
    cc.setXYpos(src.xpos(), src.ypos() + 60)

    return {"name": cc.name(), "type": cc.Class()}


def _handle_setup_merge(params: dict) -> dict:
    """Create a Merge2 with fg on B pipe (input 1) and bg on A pipe (input 0)."""
    import nuke

    fg_name = params["fg"]
    bg_name = params["bg"]
    operation = params.get("operation", "over")

    if operation not in _MERGE_OPERATIONS:
        raise ValueError(f"invalid operation: {operation}")

    fg_node = _resolve_node(fg_name)
    bg_node = _resolve_node(bg_name)
    if fg_node is None:
        raise ValueError(f"fg node not found: {fg_name}")
    if bg_node is None:
        raise ValueError(f"bg node not found: {bg_name}")

    merge = nuke.nodes.Merge2()
    merge["operation"].setValue(operation)
    merge.setInput(0, bg_node)  # A pipe = bg
    merge.setInput(1, fg_node)  # B pipe = fg
    merge.setXYpos(
        (fg_node.xpos() + bg_node.xpos()) // 2,
        max(fg_node.ypos(), bg_node.ypos()) + 80,
    )

    return {"name": merge.name(), "operation": operation}


def _handle_setup_transform(params: dict) -> dict:
    """Create a transform node downstream of ``input_node``."""
    import nuke

    input_node = params["input_node"]
    operation = params.get("operation", "Transform")

    if operation not in _TRANSFORM_OPERATIONS:
        raise ValueError(f"invalid operation: {operation}")

    src = _resolve_node(input_node)
    if src is None:
        raise ValueError(f"node not found: {input_node}")

    t = getattr(nuke.nodes, operation)()
    t.setInput(0, src)
    t.setXYpos(src.xpos(), src.ypos() + 60)

    return {"name": t.name(), "type": t.Class()}


def _handle_setup_denoise(params: dict) -> dict:
    """Create a Denoise2 node downstream of ``input_node``."""
    import nuke

    input_node = params["input_node"]

    src = _resolve_node(input_node)
    if src is None:
        raise ValueError(f"node not found: {input_node}")

    dn = nuke.nodes.Denoise2()
    dn.setInput(0, src)
    dn.setXYpos(src.xpos(), src.ypos() + 60)

    return {"name": dn.name(), "type": dn.Class()}


# Windows-reserved device basenames -- a path whose final segment matches
# one of these (case-insensitive, with or without an extension) refers to
# a device, not a file. Writing to ``CON``, ``PRN`` etc. has historically
# been a hang/crash source on Windows.
_WIN_RESERVED_DEVICES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


class PathPolicyViolation(ValueError):
    """Raised when a write path fails the policy check.

    Carries an ``error_class`` attribute so the wire envelope surfaces
    ``PathPolicyViolation`` rather than the generic ``ValueError`` from
    the addon's exception path.
    """

    error_class = "PathPolicyViolation"


def _allowed_write_roots() -> list[str]:
    """Return the list of absolute roots a setup_write path may live under.

    Defaults to the user's home directory plus the ``$SS`` (Salt Spill
    sandbox) env var if set. ``NUKE_MCP_WRITE_ROOTS`` (semicolon- or
    os.pathsep-separated) overrides the defaults entirely.

    Empty / missing env values are skipped so a missing ``$SS`` doesn't
    silently widen the allow-list.
    """
    roots: list[str] = []
    override = os.environ.get("NUKE_MCP_WRITE_ROOTS")
    if override:
        roots = [r.strip() for r in override.replace(";", os.pathsep).split(os.pathsep)]
        return [os.path.normcase(os.path.normpath(r)) for r in roots if r]

    home = os.path.expanduser("~")
    if home and home != "~":
        roots.append(home)
    ss = os.environ.get("SS")
    if ss:
        roots.append(ss)
    return [os.path.normcase(os.path.normpath(r)) for r in roots]


def _validate_write_path(path: object) -> str:
    """Apply the setup_write path policy. Returns the (resolved) path.

    Rejects:
      * non-string inputs
      * traversal (``..`` segment)
      * UNC paths (``\\\\server\\share\\...``)
      * Windows reserved device basenames (CON, PRN, NUL, COM1, ...)
      * absolute paths that don't live under any allow-listed root

    Raises PathPolicyViolation on any violation. Relative paths are
    accepted unconditionally -- they resolve under the script's current
    working directory inside Nuke, which is the user's choice.
    """
    if not isinstance(path, str) or not path:
        raise PathPolicyViolation("invalid path: must be a non-empty string")

    # Expand ~ first so the absolute-path check sees the resolved form.
    expanded = os.path.expanduser(path)

    # Traversal: any ``..`` component (cross-platform). Rejecting on the
    # raw split catches both forward- and back-slash forms.
    parts = expanded.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        raise PathPolicyViolation("invalid path: path traversal not permitted")

    # UNC: ``\\server\share\...`` or ``//server/share/...``.
    if expanded.startswith("\\\\") or expanded.startswith("//"):
        raise PathPolicyViolation(
            "invalid path: UNC paths not permitted (network share writes blocked)"
        )

    # Windows reserved devices: check the final basename without extension.
    base = os.path.basename(expanded)
    if base:
        stem = base.split(".", 1)[0].upper()
        if stem in _WIN_RESERVED_DEVICES:
            raise PathPolicyViolation(
                f"invalid path: Windows reserved device name '{stem}' not permitted"
            )

    # Absolute-path allow-list. Relative paths skip this check.
    if os.path.isabs(expanded):
        normalized = os.path.normcase(os.path.normpath(expanded))
        roots = _allowed_write_roots()
        if not roots:
            raise PathPolicyViolation(
                "invalid path: absolute writes blocked (no allow-listed roots; "
                "set NUKE_MCP_WRITE_ROOTS or $SS to enable)"
            )
        if not any(normalized == root or normalized.startswith(root + os.sep) for root in roots):
            raise PathPolicyViolation(
                "invalid path: absolute path is outside the allow-listed write roots "
                "(set NUKE_MCP_WRITE_ROOTS to widen)"
            )

    return expanded


def _handle_setup_write(params: dict) -> dict:
    """Create a Write node downstream of ``input_node`` with validated path/file_type.

    Path policy (see ``_validate_write_path``):
      * traversal (``..``) -> rejected.
      * UNC (``\\\\server\\share\\...``) -> rejected.
      * Windows reserved device basenames -> rejected.
      * Absolute paths -> rejected unless they live under a root from
        ``NUKE_MCP_WRITE_ROOTS`` (semicolon-separated) or, by default,
        ``$HOME`` and ``$SS`` (Salt Spill sandbox).
      * Relative paths -> accepted (resolved relative to the script's
        cwd inside Nuke).
    """
    import nuke

    input_node = params["input_node"]
    path = _validate_write_path(params["path"])
    file_type = params.get("file_type", "exr")
    colorspace = params.get("colorspace", "scene_linear")

    if file_type not in _WRITE_FILE_TYPES:
        raise ValueError(f"invalid file_type: {file_type}")

    src = _resolve_node(input_node)
    if src is None:
        raise ValueError(f"node not found: {input_node}")

    w = nuke.nodes.Write()
    w.setInput(0, src)
    w["file"].setValue(path)
    w["file_type"].setValue(file_type)
    if w.knob("colorspace"):
        w["colorspace"].setValue(colorspace)

    return {"name": w.name(), "path": path, "file_type": file_type}


# ---------------------------------------------------------------------------
# B7: scene_digest / scene_delta
#
# Compact fingerprint of the node graph for delta-aware turn loops. Ports
# the pattern from houdini-mcp-beta/houdini_mcp/tools/digest.py:28-200.
# ---------------------------------------------------------------------------


def _compute_digest_hash(data: dict[str, Any]) -> str:
    """Return md5 hex[:8] over the JSON-serialized digest body."""
    import hashlib

    # Drop ``hash`` and ``status`` from the body before hashing so the
    # hash is stable across calls.
    body = {k: v for k, v in data.items() if k not in ("hash", "status", "changed")}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def _build_scene_digest() -> dict[str, Any]:
    """Build the scene digest body. Used by both digest and delta handlers."""
    import nuke

    nodes = nuke.allNodes()
    counts: dict[str, int] = {}
    errors: list[str] = []
    warnings: list[str] = []

    for n in nodes:
        try:
            cls = n.Class()
        except Exception:
            continue
        counts[cls] = counts.get(cls, 0) + 1
        try:
            if n.hasError():
                errors.append(n.name())
        except Exception:
            pass
        try:
            if hasattr(n, "warnings") and n.warnings():
                warnings.append(n.name())
        except Exception:
            pass

    selected = [n.name() for n in nuke.selectedNodes()]

    viewer_active = ""
    display_node = ""
    try:
        viewer = nuke.activeViewer()
        if viewer is not None:
            v_node = viewer.node()
            if v_node is not None:
                viewer_active = v_node.name()
                inp = v_node.input(0)
                if inp is not None:
                    display_node = inp.name()
    except Exception:
        pass

    body: dict[str, Any] = {
        "counts": counts,
        "total": len(nodes),
        "errors": errors,
        "warnings": warnings,
        "selected": selected,
        "viewer_active": viewer_active,
        "display_node": display_node,
    }
    return body


def _handle_scene_digest(params: dict) -> dict:
    """Return the full scene digest body plus an md5[:8] hash."""
    body = _build_scene_digest()
    body["hash"] = _compute_digest_hash(body)
    return body


def _handle_scene_delta(params: dict) -> dict:
    """Return early ``{"changed": False, "hash": prev_hash}`` if the digest is unchanged.

    On change, returns the full body with ``changed=True`` and the new
    ``hash``. The delta handler always builds the full body (cheap on a
    hot Nuke session) so the early-exit is a wire-level optimization
    rather than a Nuke-side one. The win is the MCP client gets to skip
    re-rendering large response payloads on no-op turns.
    """
    prev_hash = params.get("prev_hash") or ""
    body = _build_scene_digest()
    current_hash = _compute_digest_hash(body)
    if current_hash == prev_hash:
        return {"changed": False, "hash": current_hash}
    body["hash"] = current_hash
    body["changed"] = True
    return body


# ---------------------------------------------------------------------------
# C1: tracking + deep typed handlers
#
# Atomic primitives for camera-tracker, planar tracker, Tracker4 and the
# bake operations; plus the deep-comp primitives (DeepRecolor / DeepMerge
# / DeepHoldout / DeepTransform / DeepToImage). All handlers return a
# flat ``NodeRef`` -- ``{name, type, x, y, inputs}`` -- so callers
# never have to follow up with a separate ``get_node_info`` round-trip.
#
# Idempotency: when ``name`` is supplied AND a node of the same class
# with matching inputs already exists at that name, return the existing
# NodeRef instead of creating a duplicate. When ``name`` is None, the
# tool is BENIGN_NEW (Nuke auto-uniquifies, so a second call yields a
# fresh ``Foo2``).
# ---------------------------------------------------------------------------

_CAMERA_SOLVE_METHODS = frozenset({"Match-Move", "Tripod", "Free Camera", "Planar", "Object"})
_DEEP_MERGE_OPS = frozenset({"over", "holdout"})


def _node_inputs(node: Any) -> list[str | None]:
    inputs: list[str | None] = []
    for i in range(node.inputs()):
        inp = node.input(i)
        inputs.append(inp.name() if inp else None)
    return inputs


def _node_ref(node: Any) -> dict[str, Any]:
    """Return the standard NodeRef shape for one Nuke node.

    Wire keys match the existing addon convention used by
    ``_handle_read_node_detail`` and ``_handle_list_nodes`` (``type``/``x``/``y``)
    rather than Python attribute names (``Class``/``xpos``/``ypos``).
    """
    return {
        "name": node.name(),
        "type": node.Class(),
        "x": int(node.xpos()),
        "y": int(node.ypos()),
        "inputs": _node_inputs(node),
    }


def _maybe_existing(
    name: str | None,
    node_class: str,
    expected_inputs: list[str | None],
) -> dict[str, Any] | None:
    """Return a NodeRef if an existing node matches name+class+inputs.

    Used by every C1 handler to short-circuit duplicate creation when
    the caller supplies an explicit ``name``. If the existing node has
    the wrong class or different inputs, raises ValueError so the
    caller hits a fresh error rather than silently mutating an
    unrelated node.
    """
    import nuke

    if not name:
        return None
    existing = nuke.toNode(name)
    if existing is None:
        return None
    if existing.Class() != node_class:
        raise ValueError(
            f"node '{name}' exists but is class '{existing.Class()}', " f"expected '{node_class}'"
        )
    actual_inputs = _node_inputs(existing)
    # Only compare leading slots that were specified -- ``expected_inputs``
    # is the list the handler intends to wire, and Nuke can have stray
    # ``None`` tail slots.
    leading = actual_inputs[: len(expected_inputs)]
    if leading != expected_inputs:
        raise ValueError(
            f"node '{name}' exists but has inputs {actual_inputs}, " f"expected {expected_inputs}"
        )
    return _node_ref(existing)


def _set_name(node: Any, name: str | None) -> None:
    if name:
        node.setName(name)


def _handle_setup_camera_tracker(params: dict) -> dict:
    """Create a CameraTracker downstream of ``input_node``."""
    import nuke

    input_node = params["input_node"]
    features = int(params.get("features", 300))
    solve_method = params.get("solve_method", "Match-Move")
    mask = params.get("mask")
    name = params.get("name")

    if solve_method not in _CAMERA_SOLVE_METHODS:
        raise ValueError(f"invalid solve_method: {solve_method}")

    src = _resolve_node(input_node)
    if src is None:
        raise ValueError(f"node not found: {input_node}")

    mask_node = None
    if mask is not None:
        mask_node = _resolve_node(mask)
        if mask_node is None:
            raise ValueError(f"mask node not found: {mask}")

    expected_inputs: list[str | None] = [src.name()]
    if mask_node is not None:
        expected_inputs.append(mask_node.name())
    cached = _maybe_existing(name, "CameraTracker", expected_inputs)
    if cached is not None:
        return cached

    tracker = nuke.nodes.CameraTracker()
    tracker.setInput(0, src)
    if mask_node is not None:
        tracker.setInput(1, mask_node)
    if tracker.knob("numberFeatures"):
        tracker["numberFeatures"].setValue(features)
    if tracker.knob("solveMethod"):
        with contextlib.suppress(Exception):
            tracker["solveMethod"].setValue(solve_method)
    tracker.setXYpos(src.xpos(), src.ypos() + 60)
    _set_name(tracker, name)
    return _node_ref(tracker)


def _handle_setup_planar_tracker(params: dict) -> dict:
    """Create a PlanarTracker fed by ``input_node`` + ``plane_roto``."""
    import nuke

    input_node = params["input_node"]
    plane_roto = params["plane_roto"]
    ref_frame = int(params.get("ref_frame", 1))
    name = params.get("name")

    src = _resolve_node(input_node)
    if src is None:
        raise ValueError(f"node not found: {input_node}")
    roto = _resolve_node(plane_roto)
    if roto is None:
        raise ValueError(f"plane_roto node not found: {plane_roto}")
    if roto.Class() not in ("Roto", "RotoPaint"):
        raise ValueError(
            f"plane_roto '{plane_roto}' is class '{roto.Class()}', expected Roto or RotoPaint"
        )

    expected_inputs = [src.name(), roto.name()]
    # Try PlanarTracker (Nuke 15.x verified) first, then legacy
    # PlanarTrackerNode. ``nuke.nodes.X`` always returns a callable
    # factory, so the *creation* call is what reveals the real class.
    cached = _maybe_existing(name, "PlanarTracker", expected_inputs)
    if cached is not None:
        return cached

    tracker = None
    create_errors = []
    for class_name in ("PlanarTracker", "PlanarTrackerNode"):
        factory = getattr(nuke.nodes, class_name, None)
        if factory is None:
            create_errors.append(f"{class_name}: factory missing")
            continue
        try:
            tracker = factory()
            break
        except (RuntimeError, Exception) as exc:
            create_errors.append(f"{class_name}: {exc}")
    if tracker is None:
        raise ValueError(
            "PlanarTracker not available in this Nuke build: " + "; ".join(create_errors)
        )
    tracker.setInput(0, src)
    tracker.setInput(1, roto)
    if tracker.knob("referenceFrame"):
        tracker["referenceFrame"].setValue(ref_frame)
    tracker.setXYpos(src.xpos(), src.ypos() + 60)
    _set_name(tracker, name)
    return _node_ref(tracker)


def _handle_setup_tracker4(params: dict) -> dict:
    """Create a Tracker4 with ``num_tracks`` track slots."""
    import nuke

    input_node = params["input_node"]
    num_tracks = int(params.get("num_tracks", 4))
    name = params.get("name")

    if num_tracks < 1:
        raise ValueError(f"num_tracks must be >= 1, got {num_tracks}")

    src = _resolve_node(input_node)
    if src is None:
        raise ValueError(f"node not found: {input_node}")

    cached = _maybe_existing(name, "Tracker4", [src.name()])
    if cached is not None:
        return cached

    tracker = nuke.nodes.Tracker4()
    tracker.setInput(0, src)
    tracker.setXYpos(src.xpos(), src.ypos() + 60)
    # Tracker4 ships with knobs for tracks 1..N already; we don't
    # synthesize per-track knobs here -- callers tweak via set_knob.
    _set_name(tracker, name)
    return _node_ref(tracker)


def _handle_bake_tracker_to_corner_pin(params: dict) -> dict:
    """Bake a Tracker4 / PlanarTracker into a CornerPin2D."""
    import nuke

    tracker_name = params["tracker_node"]
    ref_frame = int(params.get("ref_frame", 1))
    name = params.get("name")

    src = _resolve_node(tracker_name)
    if src is None:
        raise ValueError(f"tracker node not found: {tracker_name}")
    if src.Class() not in ("Tracker4", "PlanarTrackerNode", "PlanarTracker"):
        raise ValueError(
            f"tracker_node '{tracker_name}' is class '{src.Class()}', "
            "expected Tracker4 or PlanarTracker"
        )

    cached = _maybe_existing(name, "CornerPin2D", [src.name()])
    if cached is not None:
        return cached

    pin = nuke.nodes.CornerPin2D()
    pin.setInput(0, src)
    if pin.knob("reference_frame"):
        pin["reference_frame"].setValue(ref_frame)
    pin.setXYpos(src.xpos() + 80, src.ypos() + 60)
    _set_name(pin, name)
    return _node_ref(pin)


def _handle_solve_3d_camera(params: dict) -> dict:
    """Solve a CameraTracker and return its NodeRef.

    The actual solve runs through Nuke's CameraTracker::solveCamera()
    when available; fall back to a direct knob trigger.
    """

    tracker_name = params["camera_tracker_node"]
    name = params.get("name")

    src = _resolve_node(tracker_name)
    if src is None:
        raise ValueError(f"camera_tracker_node not found: {tracker_name}")
    if src.Class() != "CameraTracker":
        raise ValueError(
            f"node '{tracker_name}' is class '{src.Class()}', " f"expected CameraTracker"
        )

    # Idempotent: if name supplied AND it already points at this same
    # CameraTracker, return its NodeRef without re-solving.
    if name and name == src.name():
        return _node_ref(src)

    # Trigger the solve. In Nuke 15.x ``solveCamera`` is a button knob
    # on the CameraTracker (not a method on the PyNode). Older builds
    # exposed a method by the same name, and some had a ``solve`` knob.
    # Try button-knob path first (the verified Nuke 15.1 shape), then
    # method, then legacy ``solve`` knob.
    solve_knob = src.knob("solveCamera")
    if solve_knob is not None and hasattr(solve_knob, "execute"):
        solve_knob.execute()
    else:
        solver = getattr(src, "solveCamera", None)
        if callable(solver):
            solver()
        else:
            legacy = src.knob("solve")
            if legacy is not None and hasattr(legacy, "execute"):
                legacy.execute()
            else:
                raise ValueError("CameraTracker has no solveCamera knob/method or solve knob")

    if name and name != src.name():
        src.setName(name)
    return _node_ref(src)


def _handle_bake_camera_to_card(params: dict) -> dict:
    """Bake a solved Camera / CameraTracker to a Card3D at ``frame``."""
    import nuke

    cam_name = params["camera_node"]
    frame = int(params.get("frame", 1))
    name = params.get("name")

    src = _resolve_node(cam_name)
    if src is None:
        raise ValueError(f"camera_node not found: {cam_name}")

    cached = _maybe_existing(name, "Card3D", [src.name()])
    if cached is not None:
        return cached

    card_factory = getattr(nuke.nodes, "Card3D", None) or getattr(nuke.nodes, "Card", None)
    if card_factory is None:
        raise ValueError("Card3D / Card not available in this Nuke build")
    card = card_factory()
    card.setInput(0, src)
    if card.knob("frame"):
        with contextlib.suppress(Exception):
            card["frame"].setValue(frame)
    card.setXYpos(src.xpos() + 80, src.ypos() + 60)
    _set_name(card, name)
    return _node_ref(card)


# ---------------------------------------------------------------------------
# C5 workflow macros
#
# These compose the C1 atomic primitives above into a multi-node graph
# wrapped in a Group. The macro never reimplements tracker creation --
# it dispatches into the corresponding ``_handle_setup_*`` /
# ``_handle_bake_*`` / ``_handle_solve_*`` handlers and wires the
# returned NodeRefs into a Group.
# ---------------------------------------------------------------------------


_SURFACE_TYPES = frozenset({"planar", "3d"})


def _derive_shot_tag() -> str:
    """Derive a Group-name suffix from ``$SS_SHOT`` or the script path.

    Order:
      1. ``$SS_SHOT`` env var (the Salt Spill / FMP convention).
      2. Stem of ``nuke.root().name()`` -- e.g. ``ss_shot0170_v003.nk``
         -> ``ss_shot0170_v003``.
      3. ``unsaved`` if neither is available (root has no name yet).

    The result is sanitised so the Group name is always a valid Nuke
    identifier: only ``[A-Za-z0-9_]`` survives, anything else collapses
    to ``_``. An empty result falls through to ``unsaved``.
    """
    import nuke

    raw = os.environ.get("SS_SHOT") or ""
    if not raw:
        try:
            script_path = nuke.root().name() or ""
        except Exception:
            script_path = ""
        if script_path:
            raw = pathlib.Path(script_path).stem
    if not raw:
        raw = "unsaved"
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in raw)
    return cleaned or "unsaved"


def _group_name_for_patch(explicit: str | None) -> str:
    """Pick the Group name for a spaceship patch macro call."""
    if explicit:
        return explicit
    return f"SpaceshipPatch_{_derive_shot_tag()}"


def _handle_setup_spaceship_track_patch(params: dict) -> dict:
    """Build a tracked-patch graph wrapped in a SpaceshipPatch_<shot> Group.

    Composes the C1 primitives:

    * ``planar`` branch: Roto plane -> setup_planar_tracker ->
      bake_tracker_to_corner_pin -> RotoPaint (or supplied
      ``patch_source``) -> CornerPin2D restore.
    * ``3d`` branch: setup_camera_tracker -> solve_3d_camera ->
      bake_camera_to_card -> Project3D -> ScanlineRender -> Merge2 over.

    Idempotent on ``name=`` -- a re-call where a Group of that name
    already exists short-circuits to the existing Group's NodeRef.
    """
    import nuke

    plate_name = params["plate"]
    ref_frame = int(params["ref_frame"])
    surface_type = params.get("surface_type", "planar")
    patch_source = params.get("patch_source")
    explicit_name = params.get("name")

    if surface_type not in _SURFACE_TYPES:
        raise ValueError(f"invalid surface_type: {surface_type!r} (expected 'planar' or '3d')")

    plate = _resolve_node(plate_name)
    if plate is None:
        raise ValueError(f"plate node not found: {plate_name}")

    if patch_source is not None and _resolve_node(patch_source) is None:
        raise ValueError(f"patch_source node not found: {patch_source}")

    group_name = _group_name_for_patch(explicit_name)

    # Idempotent re-call: a Group of this name already exists, return it.
    existing = nuke.toNode(group_name)
    if existing is not None:
        if existing.Class() != "Group":
            raise ValueError(
                f"node '{group_name}' exists but is class '{existing.Class()}', " "expected 'Group'"
            )
        return _node_ref(existing)

    # Build sub-graph at module scope (we move nodes into the Group at
    # the end via Group ``.script`` round-trip is not necessary --
    # ``nuke.nodes.Group()`` plus inner-node creation while the group
    # context is open is the clean path).
    group = nuke.nodes.Group()
    group.setName(group_name)

    members: list[str] = []
    try:
        group.begin()

        # Every Group's input plate appears as an Input node inside the
        # group context. The outer ``setInput(0, plate)`` below wires
        # the Group's external input pipe to the plate.
        group_input = nuke.nodes.Input()
        group_input.setName("plate_in")

        if surface_type == "planar":
            # Roto plane (driven by the user post-creation; here we
            # seed an empty RotoPaint stand-in, since PlanarTracker
            # accepts Roto / RotoPaint.
            roto_plane = nuke.nodes.Roto()
            roto_plane.setInput(0, group_input)
            roto_plane.setName(f"{group_name}_plane")
            members.append(roto_plane.name())

            # Compose: setup_planar_tracker
            planar = _handle_setup_planar_tracker(
                {
                    "input_node": group_input.name(),
                    "plane_roto": roto_plane.name(),
                    "ref_frame": ref_frame,
                    "name": f"{group_name}_planar",
                }
            )
            members.append(planar["name"])

            # Compose: bake_tracker_to_corner_pin (forward pin)
            forward_pin = _handle_bake_tracker_to_corner_pin(
                {
                    "tracker_node": planar["name"],
                    "ref_frame": ref_frame,
                    "name": f"{group_name}_pinFwd",
                }
            )
            members.append(forward_pin["name"])

            # Patch source: user-supplied node OR a default RotoPaint
            # clone fed by the forward pin.
            if patch_source is not None:
                patch_node = nuke.toNode(patch_source)
                if patch_node is None:
                    raise ValueError(f"patch_source vanished mid-build: {patch_source}")
                patch_root_name = patch_node.name()
            else:
                rp = nuke.nodes.RotoPaint()
                rp.setInput(0, nuke.toNode(forward_pin["name"]))
                rp.setName(f"{group_name}_paint")
                patch_root_name = rp.name()
            members.append(patch_root_name)

            # Restore-perspective CornerPin: a second CornerPin2D fed by
            # the patch source, reversing the forward pin so the painted
            # patch sits back in plate space.
            pin_node = nuke.toNode(forward_pin["name"])
            patch_node = nuke.toNode(patch_root_name)
            restore_pin = nuke.nodes.CornerPin2D()
            restore_pin.setInput(0, patch_node)
            if restore_pin.knob("reference_frame"):
                with contextlib.suppress(Exception):
                    restore_pin["reference_frame"].setValue(ref_frame)
            if restore_pin.knob("invert"):
                with contextlib.suppress(Exception):
                    restore_pin["invert"].setValue(True)
            restore_pin.setName(f"{group_name}_pinRestore")
            members.append(restore_pin.name())

            output_input: Any = restore_pin
            _ = pin_node  # forward pin lives in the chain; not the output

        else:  # surface_type == "3d"
            # Compose: setup_camera_tracker
            camtrack = _handle_setup_camera_tracker(
                {
                    "input_node": group_input.name(),
                    "name": f"{group_name}_camTrack",
                }
            )
            members.append(camtrack["name"])

            # Compose: solve_3d_camera
            solved = _handle_solve_3d_camera(
                {
                    "camera_tracker_node": camtrack["name"],
                    "name": f"{group_name}_camTrack",
                }
            )
            members.append(solved["name"])

            # Compose: bake_camera_to_card
            card = _handle_bake_camera_to_card(
                {
                    "camera_node": solved["name"],
                    "frame": ref_frame,
                    "name": f"{group_name}_card",
                }
            )
            members.append(card["name"])

            # Patch source: user-supplied OR a default RotoPaint upstream
            # of the Project3D so the artist has somewhere to paint.
            if patch_source is not None:
                patch_node = nuke.toNode(patch_source)
                if patch_node is None:
                    raise ValueError(f"patch_source vanished mid-build: {patch_source}")
                patch_root_name = patch_node.name()
            else:
                rp = nuke.nodes.RotoPaint()
                rp.setInput(0, group_input)
                rp.setName(f"{group_name}_paint")
                patch_root_name = rp.name()
            members.append(patch_root_name)

            # Project3D fed by patch source; second input is the
            # solved camera (CameraTracker exposes a camera output).
            project = nuke.nodes.Project3D()
            project.setInput(0, nuke.toNode(patch_root_name))
            project.setInput(1, nuke.toNode(solved["name"]))
            project.setName(f"{group_name}_project3D")
            members.append(project.name())

            # ScanlineRender takes (obj=card3d_with_projection, cam=solved)
            # then we Merge over plate.
            #
            # Wire: card receives the Project3D as its 'img' projection
            # via setInput(1, project3D). Card3D inputs in Nuke 15 are
            # input 0 = unused (in some builds) / image, input 1 = camera
            # for projection. Wiring is best-effort via setInput; the
            # macro stays robust by guarding with contextlib.suppress so
            # a build-mismatch doesn't break the Group construction.
            card_node = nuke.toNode(card["name"])
            with contextlib.suppress(Exception):
                card_node.setInput(0, project)

            scanline = nuke.nodes.ScanlineRender()
            scanline.setInput(1, card_node)
            scanline.setInput(2, nuke.toNode(solved["name"]))
            scanline.setName(f"{group_name}_scanline")
            members.append(scanline.name())

            merge = nuke.nodes.Merge2()
            if merge.knob("operation"):
                with contextlib.suppress(Exception):
                    merge["operation"].setValue("over")
            merge.setInput(0, group_input)  # A pipe = bg = plate
            merge.setInput(1, scanline)  # B pipe = fg = rendered patch
            merge.setName(f"{group_name}_merge")
            members.append(merge.name())

            output_input = merge

        # Group output node closes off the internal graph.
        group_output = nuke.nodes.Output()
        group_output.setInput(0, output_input)
    finally:
        with contextlib.suppress(Exception):
            group.end()

    # Wire the Group's external input to the plate.
    group.setInput(0, plate)
    group.setXYpos(plate.xpos(), plate.ypos() + 80)

    ref = _node_ref(group)
    ref["surface_type"] = surface_type
    ref["members"] = members
    return ref


def _handle_create_deep_recolor(params: dict) -> dict:
    """Create a DeepRecolor fed by deep input + colour input."""
    import nuke

    deep_name = params["deep_node"]
    color_name = params["color_node"]
    target_input_alpha = bool(params.get("target_input_alpha", True))
    name = params.get("name")

    deep = _resolve_node(deep_name)
    if deep is None:
        raise ValueError(f"deep node not found: {deep_name}")
    color = _resolve_node(color_name)
    if color is None:
        raise ValueError(f"color node not found: {color_name}")

    expected_inputs = [deep.name(), color.name()]
    cached = _maybe_existing(name, "DeepRecolor", expected_inputs)
    if cached is not None:
        return cached

    rec = nuke.nodes.DeepRecolor()
    rec.setInput(0, deep)
    rec.setInput(1, color)
    if rec.knob("target_input_alpha"):
        rec["target_input_alpha"].setValue(target_input_alpha)
    rec.setXYpos(deep.xpos(), deep.ypos() + 60)
    _set_name(rec, name)
    return _node_ref(rec)


def _handle_create_deep_merge(params: dict) -> dict:
    """Create a DeepMerge between two deep streams (default operation 'over')."""
    import nuke

    a_name = params["a_node"]
    b_name = params["b_node"]
    op = params.get("op", "over")
    name = params.get("name")

    if op not in _DEEP_MERGE_OPS:
        raise ValueError(f"invalid op: {op}")

    a = _resolve_node(a_name)
    if a is None:
        raise ValueError(f"a_node not found: {a_name}")
    b = _resolve_node(b_name)
    if b is None:
        raise ValueError(f"b_node not found: {b_name}")

    expected_inputs = [a.name(), b.name()]
    cached = _maybe_existing(name, "DeepMerge", expected_inputs)
    if cached is not None:
        return cached

    merge = nuke.nodes.DeepMerge()
    merge.setInput(0, a)
    merge.setInput(1, b)
    if merge.knob("operation"):
        with contextlib.suppress(Exception):
            merge["operation"].setValue(op)
    merge.setXYpos(
        (a.xpos() + b.xpos()) // 2,
        max(a.ypos(), b.ypos()) + 80,
    )
    _set_name(merge, name)
    return _node_ref(merge)


def _handle_create_deep_holdout(params: dict) -> dict:
    """Create a DeepHoldout: subject minus holdout."""
    import nuke

    subject_name = params["subject_node"]
    holdout_name = params["holdout_node"]
    name = params.get("name")

    subject = _resolve_node(subject_name)
    if subject is None:
        raise ValueError(f"subject_node not found: {subject_name}")
    holdout = _resolve_node(holdout_name)
    if holdout is None:
        raise ValueError(f"holdout_node not found: {holdout_name}")

    expected_inputs = [subject.name(), holdout.name()]
    # DeepHoldout2 is the modern 2-input version (Nuke 15+); legacy
    # DeepHoldout takes a single input. Try the 2-input shape first
    # then fall back. Match cache against the actual class created.
    cached = _maybe_existing(name, "DeepHoldout2", expected_inputs) or _maybe_existing(
        name, "DeepHoldout", expected_inputs
    )
    if cached is not None:
        return cached

    hd = None
    create_errors = []
    for class_name in ("DeepHoldout2", "DeepHoldout"):
        factory = getattr(nuke.nodes, class_name, None)
        if factory is None:
            create_errors.append(f"{class_name}: factory missing")
            continue
        try:
            candidate = factory()
            # Verify it accepts the second input -- legacy DeepHoldout
            # is single-input and ``setInput(1, ...)`` returns False.
            if candidate.setInput(1, holdout):
                candidate.setInput(0, subject)
                hd = candidate
                break
            create_errors.append(f"{class_name}: refused holdout input")
        except (RuntimeError, Exception) as exc:
            create_errors.append(f"{class_name}: {exc}")
    if hd is None:
        raise ValueError("Could not create DeepHoldout: " + "; ".join(create_errors))
    hd.setXYpos(
        (subject.xpos() + holdout.xpos()) // 2,
        max(subject.ypos(), holdout.ypos()) + 80,
    )
    _set_name(hd, name)
    return _node_ref(hd)


def _handle_create_deep_transform(params: dict) -> dict:
    """Create a DeepTransform with an optional translate vector."""
    import nuke

    input_name = params["input_node"]
    translate = params.get("translate", (0.0, 0.0, 0.0))
    name = params.get("name")

    if not isinstance(translate, list | tuple) or len(translate) != 3:
        raise ValueError(f"translate must be a 3-tuple, got {translate!r}")

    src = _resolve_node(input_name)
    if src is None:
        raise ValueError(f"input_node not found: {input_name}")

    cached = _maybe_existing(name, "DeepTransform", [src.name()])
    if cached is not None:
        return cached

    dt = nuke.nodes.DeepTransform()
    dt.setInput(0, src)
    if dt.knob("translate"):
        for i, v in enumerate(translate):
            with contextlib.suppress(Exception):
                dt["translate"].setValue(float(v), i)
    dt.setXYpos(src.xpos(), src.ypos() + 60)
    _set_name(dt, name)
    return _node_ref(dt)


def _handle_deep_to_image(params: dict) -> dict:
    """Create a DeepToImage flattening a deep stream to 2D."""
    import nuke

    input_name = params["input_node"]
    name = params.get("name")

    src = _resolve_node(input_name)
    if src is None:
        raise ValueError(f"input_node not found: {input_name}")

    cached = _maybe_existing(name, "DeepToImage", [src.name()])
    if cached is not None:
        return cached

    n = nuke.nodes.DeepToImage()
    n.setInput(0, src)
    n.setXYpos(src.xpos(), src.ypos() + 60)
    _set_name(n, name)
    return _node_ref(n)


# ---------------------------------------------------------------------------
# C2: OCIO / ACEScct color management
# ---------------------------------------------------------------------------

# Image-file extensions that imply a non-linear (sRGB/display) source.
# Used by ``audit_acescct_consistency`` to flag Reads with
# ``colorspace=default`` whose paths look like sRGB textures rather than
# scene-linear EXR/DPX deliveries.
_NONLINEAR_EXTS = (".png", ".jpg", ".jpeg")

# Heuristic substring -- if a Read filename contains this, the colorspace
# should almost certainly be sRGB-textured rather than the working space.
_SRGB_HINT = "_srgb"


def _root_knob_value(knob_name: str) -> str:
    """Return the string value of a root knob, or an empty string if absent.

    Several colour-management knobs were renamed across Nuke versions
    (``OCIOConfigPath`` -> ``OCIO_config`` -> ``ocio_config_path``),
    and a few don't exist on the Nuke variant at all (``monitorLut``
    is NukeStudio/Hiero only on some builds). Returning ``""`` for
    missing knobs keeps the wire shape stable across versions instead
    of forcing the caller to special-case every knob.
    """
    import nuke

    root = nuke.root()
    knob = root.knob(knob_name)
    if knob is None:
        return ""
    try:
        return str(knob.value())
    except Exception:
        return ""


def _handle_get_color_management(params: dict) -> dict:
    """Return the script's color-management state.

    Reads ``nuke.root()`` knobs and assembles a flat dict. Keys are
    stable across Nuke versions; missing knobs come back as ``""``.
    """
    return {
        "color_management": _root_knob_value("colorManagement"),
        "ocio_config": _root_knob_value("OCIO_config"),
        "working_space": _root_knob_value("workingSpaceLUT"),
        "default_view": _root_knob_value("defaultViewerLUT"),
        "monitor_lut": _root_knob_value("monitorLut"),
    }


def _handle_set_working_space(params: dict) -> dict:
    """Set ``nuke.root()['workingSpaceLUT']`` after validating the value.

    Validates ``space`` against the knob's enumeration when the knob
    exposes one (``Enumeration_Knob.values()``). Free-form OCIO
    configs may have hundreds of values; if the enumeration is empty
    (rare, but possible for custom configs that surface the knob as
    a String_Knob) we accept the value and let Nuke surface its own
    error.
    """
    import nuke

    space = params["space"]
    root = nuke.root()
    knob = root.knob("workingSpaceLUT")
    if knob is None:
        raise ValueError("nuke.root() has no 'workingSpaceLUT' knob")

    values_method = getattr(knob, "values", None)
    if callable(values_method):
        allowed = list(values_method())
        if allowed and space not in allowed:
            raise ValueError(
                f"invalid working space {space!r}; "
                f"allowed: {sorted(allowed)[:10]}{' ...' if len(allowed) > 10 else ''}"
            )

    knob.setValue(space)
    return {
        "working_space": str(knob.value()),
    }


def _read_node_colorspace(node: Any) -> str:
    """Pull the ``colorspace`` knob value off a Read/Write node, or ''.

    Nuke's Read/Write expose ``colorspace`` as an enumeration. The
    ``default`` value indicates "follow the OCIO config's input rules"
    -- the audit treats that as the unset state and flags suspicious
    paths.
    """
    knob = node.knob("colorspace")
    if knob is None:
        return ""
    try:
        return str(knob.value())
    except Exception:
        return ""


def _path_looks_nonlinear(path: str) -> bool:
    """True if ``path`` matches the sRGB/PNG/JPG heuristic."""
    if not path:
        return False
    lowered = path.lower()
    if _SRGB_HINT in lowered:
        return True
    return lowered.endswith(_NONLINEAR_EXTS)


def _is_acescg_pipe(working_space: str) -> bool:
    """True if the script's working space implies an ACEScg context.

    Matches ``ACES - ACEScg`` (OCIO 1.x) and ``ACEScg`` (legacy short
    form). Defensive: variations like ``ACES2065-1`` are NOT ACEScg
    even though they are ACES-family, so we don't blanket-match
    ``startswith("ACES")``.
    """
    return "ACEScg" in (working_space or "")


def _has_upstream_acescct_conversion(node: Any, max_depth: int = 12) -> bool:
    """Walk upstream looking for an OCIOColorSpace converting INTO ACEScct.

    ``max_depth`` caps the walk so we don't pay an O(N) cost on every
    audited node in deep graphs. The audit only needs to see the
    nearest upstream conversion -- if a node 12 hops away converts to
    ACEScct, the heuristic is still happy.
    """
    seen: set[str] = set()
    frontier: list[Any] = [node]
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: list[Any] = []
        for n in frontier:
            for i in range(n.inputs()):
                inp = n.input(i)
                if inp is None:
                    continue
                key = inp.name()
                if key in seen:
                    continue
                seen.add(key)
                if inp.Class() == "OCIOColorSpace":
                    out_knob = inp.knob("out_colorspace")
                    if out_knob is not None:
                        try:
                            if "ACEScct" in str(out_knob.value()):
                                return True
                        except Exception:
                            pass
                next_frontier.append(inp)
        frontier = next_frontier
        depth += 1
    return False


def _handle_audit_acescct_consistency(params: dict) -> dict:
    """Walk every node and flag colour-management mistakes.

    Pure read -- no node creation, no knob mutation. Returns a
    ``{findings: [...]}`` dict matching the public schema. ``strict``
    demotes the Grade-without-ACEScct heuristic from ``warning`` to
    ``info`` when False; Read/Write checks always fire at full
    severity.
    """
    import nuke

    strict = bool(params.get("strict", True))
    working = _root_knob_value("workingSpaceLUT")
    is_acescg = _is_acescg_pipe(working)

    findings: list[dict[str, str]] = []

    for node in nuke.allNodes():
        cls = node.Class()
        nname = node.name()

        if cls == "Read":
            cspace = _read_node_colorspace(node)
            file_knob = node.knob("file")
            path = ""
            if file_knob is not None:
                try:
                    path = str(file_knob.value())
                except Exception:
                    path = ""
            if cspace.startswith("default") and _path_looks_nonlinear(path):
                findings.append(
                    {
                        "severity": "warning",
                        "node": nname,
                        "message": (
                            f"Read '{nname}' colorspace=default but path "
                            f"'{path}' looks non-linear (sRGB/PNG/JPG)."
                        ),
                        "fix_suggestion": (
                            "Set the Read 'colorspace' knob to "
                            "'sRGB - Texture' (or your config's "
                            "equivalent) instead of leaving it at default."
                        ),
                    }
                )

        elif cls == "Grade" and is_acescg:
            severity = "warning" if strict else "info"
            if not _has_upstream_acescct_conversion(node):
                findings.append(
                    {
                        "severity": severity,
                        "node": nname,
                        "message": (
                            f"Grade '{nname}' is downstream of an ACEScg "
                            f"working space but no upstream OCIOColorSpace "
                            f"converts to ACEScct."
                        ),
                        "fix_suggestion": (
                            "Insert an OCIOColorSpace ACEScg -> ACEScct "
                            "before the Grade and a matching ACEScct -> "
                            "ACEScg after it."
                        ),
                    }
                )

        elif cls == "Write":
            cspace = _read_node_colorspace(node)
            file_knob = node.knob("file")
            path = ""
            if file_knob is not None:
                try:
                    path = str(file_knob.value())
                except Exception:
                    path = ""
            # Linear EXR/DPX delivery should not be tagged sRGB.
            lowered_path = path.lower()
            is_linear_delivery = lowered_path.endswith((".exr", ".dpx", ".tif", ".tiff"))
            looks_srgb = "srgb" in cspace.lower() or cspace.lower() == "rec.709"
            if is_linear_delivery and looks_srgb:
                findings.append(
                    {
                        "severity": "error",
                        "node": nname,
                        "message": (
                            f"Write '{nname}' delivers '{path}' as "
                            f"colorspace='{cspace}' -- "
                            f"scene-linear formats expect a linear / "
                            f"working-space tag."
                        ),
                        "fix_suggestion": (
                            "Set the Write 'colorspace' knob to the "
                            "scene-linear delivery target (typically "
                            "'ACES - ACEScg' or 'scene_linear')."
                        ),
                    }
                )

    return {"findings": findings}


# C9: read-only audit + QC handlers
#
# Each finding has the canonical shape:
#   {severity, node, message, fix_suggestion, ...extras}
# severity is one of "error" | "warning" | "info". Auditors NEVER
# auto-fix -- they surface signal and leave the artist in control.
# ---------------------------------------------------------------------------


def _expand_audit_root(token: str) -> tuple[str | None, str | None]:
    """Expand a single allow-list token into (expanded_path, missing_var).

    Returns ``(path, None)`` on a successful expansion; ``(None, var)``
    when ``token`` named an env var that wasn't set so the caller can
    surface a finding rather than silently widening the allow-list.
    """
    if token.startswith("$"):
        var = token[1:]
        val = os.environ.get(var)
        if not val:
            return None, var
        return val, None
    return token, None


def _normalize_audit_root(path: str) -> str:
    """Normalize an allow-list root for comparison."""
    return os.path.normcase(os.path.normpath(path))


def _handle_audit_write_paths(params: dict) -> dict:
    """Flag Write nodes whose path lies outside the allow-listed roots.

    ``$VAR`` tokens in ``allow_roots`` expand from the addon-side env.
    Each unset variable produces one ``info`` finding (so the audit
    tells you the allow-list is incomplete) plus continues with the
    remaining roots.
    """
    import nuke

    raw_roots = params.get("allow_roots") or ["$SS"]
    roots: list[str] = []
    findings: list[dict[str, Any]] = []

    for token in raw_roots:
        expanded, missing = _expand_audit_root(token)
        if missing is not None:
            findings.append(
                {
                    "severity": "info",
                    "node": "",
                    "message": f"allow-list root ${missing} is unset; skipped",
                    "fix_suggestion": f"set ${missing} or remove it from allow_roots",
                }
            )
            continue
        if expanded:
            roots.append(_normalize_audit_root(expanded))

    for n in nuke.allNodes():
        if n.Class() != "Write":
            continue
        knob = n.knob("file")
        if knob is None:
            continue
        path = knob.value() or ""
        if not path:
            findings.append(
                {
                    "severity": "warning",
                    "node": n.name(),
                    "path": "",
                    "message": "Write node has no file path set",
                    "fix_suggestion": "set the file knob to a path under an allow-listed root",
                }
            )
            continue

        normalized = _normalize_audit_root(path)
        ok = any(normalized == root or normalized.startswith(root + os.sep) for root in roots)
        if not ok:
            findings.append(
                {
                    "severity": "error",
                    "node": n.name(),
                    "path": path,
                    "message": (f"Write path is outside the allow-listed roots: {path}"),
                    "fix_suggestion": (f"move the output under one of: {', '.join(raw_roots)}"),
                }
            )

    return {"findings": findings}


def _set_ocio_colorspace_knobs(node: Any, in_cs: str, out_cs: str) -> None:
    """Set the ``in_colorspace`` / ``out_colorspace`` knobs on an OCIOColorSpace.

    The knob names are stable across Nuke 13/14/15. Catches
    enumeration errors (passing a bogus colourspace) and re-raises
    with a clearer message so the caller knows which knob and value
    failed.
    """
    in_knob = node.knob("in_colorspace")
    out_knob = node.knob("out_colorspace")
    if in_knob is not None:
        try:
            in_knob.setValue(in_cs)
        except (RuntimeError, ValueError) as exc:
            raise ValueError(f"OCIOColorSpace in_colorspace={in_cs!r} rejected: {exc}") from exc
    if out_knob is not None:
        try:
            out_knob.setValue(out_cs)
        except (RuntimeError, ValueError) as exc:
            raise ValueError(f"OCIOColorSpace out_colorspace={out_cs!r} rejected: {exc}") from exc


def _handle_convert_node_colorspace(params: dict) -> dict:
    """Wrap ``node`` in an OCIOColorSpace pair (in->out upstream, out->in downstream).

    The trailing converter rebuilds every downstream consumer that was
    feeding from ``node``: each one is re-wired to the trailing
    converter so the surrounding graph still sees ``in_cs``. Nodes
    that already feed from the leading converter (i.e. the wrap was
    applied earlier) are left alone.
    """
    import nuke

    target_name = params["node"]
    in_cs = params["in_cs"]
    out_cs = params["out_cs"]

    target = _resolve_node(target_name)
    if target is None:
        raise ValueError(f"node not found: {target_name}")

    # Snapshot downstream consumers BEFORE creating the trailing
    # converter -- otherwise the trailing converter itself would show
    # up in the consumer list and we'd self-rewire.
    consumers: list[tuple[Any, int]] = []
    for n in nuke.allNodes():
        if n is target:
            continue
        for i in range(n.inputs()):
            if n.input(i) is target:
                consumers.append((n, i))

    # Leading converter -- inserts before target by detaching its
    # input-0 and re-feeding through the new node.
    upstream = target.input(0) if target.inputs() > 0 else None

    leading = nuke.nodes.OCIOColorSpace()
    if upstream is not None:
        leading.setInput(0, upstream)
    _set_ocio_colorspace_knobs(leading, in_cs, out_cs)
    leading.setXYpos(target.xpos() - 80, target.ypos() - 60)
    target.setInput(0, leading)

    # Trailing converter -- fed by target, replaces target in every
    # snapshotted consumer.
    trailing = nuke.nodes.OCIOColorSpace()
    trailing.setInput(0, target)
    _set_ocio_colorspace_knobs(trailing, out_cs, in_cs)
    trailing.setXYpos(target.xpos() + 80, target.ypos() + 60)
    for consumer, slot in consumers:
        consumer.setInput(slot, trailing)

    return {
        "leading": _node_ref(leading),
        "trailing": _node_ref(trailing),
        "wrapped": target.name(),
    }


def _handle_create_ocio_colorspace(params: dict) -> dict:
    """Create a single OCIOColorSpace fed by ``input_node``.

    Idempotent on ``name=``: when the named node already exists with
    the same class + leading input, returns the existing NodeRef
    without creating a duplicate.
    """
    import nuke

    input_name = params["input_node"]
    in_cs = params["in_cs"]
    out_cs = params["out_cs"]
    name = params.get("name")

    src = _resolve_node(input_name)
    if src is None:
        raise ValueError(f"input_node not found: {input_name}")

    expected_inputs = [src.name()]
    cached = _maybe_existing(name, "OCIOColorSpace", expected_inputs)
    if cached is not None:
        return cached

    cs = nuke.nodes.OCIOColorSpace()
    cs.setInput(0, src)
    _set_ocio_colorspace_knobs(cs, in_cs, out_cs)
    cs.setXYpos(src.xpos(), src.ypos() + 60)
    _set_name(cs, name)
    return _node_ref(cs)


# C3 AOV / channel rebuild handlers
#
# Karma multi-channel EXRs ship every render path packed into one file:
# beauty plus diffuse_direct / diffuse_indirect / specular_direct /
# specular_indirect / sss / transmission / emission / volume plus utility
# layers (P, N, depth, motion) and one or more cryptomatte slots.
#
# ``detect_aov_layers`` reads ``Read.metadata()`` ``exr/*`` keys and
# ``Read.channels()`` to surface which layers actually live in the EXR.
# ``setup_karma_aov_pipeline`` then builds the full split-and-rebuild
# graph plus a QC viewer pair so the comp can verify the recombine
# matches the original beauty.
# ---------------------------------------------------------------------------

# Canonical Karma AOV layers in priority/build order. Layers absent from
# the source EXR are silently skipped; layers in the EXR but absent
# from this list surface under ``unknown_layers`` in the result so the
# caller can detect AOV-name drift.
_KARMA_AOV_LAYERS: tuple[str, ...] = (
    "rgba",  # beauty -- the merge base
    "diffuse_direct",
    "diffuse_indirect",
    "specular_direct",
    "specular_indirect",
    "sss",
    "transmission",
    "emission",
    "volume",
    "P",
    "N",
    "depth",
    "motion",
    # cryptomattes are detected dynamically (cryptomatte_*)
)

# Layers that sum into the beauty reconstruction (the ``Merge plus``
# chain). Utility layers (P, N, depth, motion, cryptomattes) get their
# own Shuffles but are NOT additively merged -- they're metadata
# passes, not light components.
_KARMA_LIGHT_LAYERS: frozenset[str] = frozenset(
    {
        "diffuse_direct",
        "diffuse_indirect",
        "specular_direct",
        "specular_indirect",
        "sss",
        "transmission",
        "emission",
        "volume",
    }
)


def _parse_layers_from_channels(channels: list[str]) -> dict[str, list[str]]:
    """Bucket flat channel names into ``{layer: [channel, ...]}``.

    ``Read.channels()`` returns flat strings like ``"rgba.red"`` or
    ``"depth.z"``; we split on the rightmost dot so a layer that
    happens to contain a dot (rare but legal in EXR) survives intact.
    Channels with no dot land under ``"main"``.
    """
    out: dict[str, list[str]] = {}
    for ch in channels:
        if "." in ch:
            layer, _, channel = ch.rpartition(".")
        else:
            layer, channel = "main", ch
        out.setdefault(layer, []).append(channel)
    return out


def _detect_karma_layers(read_node: Any) -> dict[str, Any]:
    """Pull layer / format / per-layer-channels off a Read node.

    Shared between ``_handle_detect_aov_layers`` (the read-only tool)
    and ``_handle_setup_karma_aov_pipeline`` (so the workflow tool
    doesn't have to round-trip through the addon a second time).
    """
    channels = list(read_node.channels())
    channels_per_layer = _parse_layers_from_channels(channels)

    # Layer order: canonical Karma list first (so the rebuild graph
    # stays predictable), then any layer in the EXR but not in the
    # canonical list, sorted for determinism.
    found = set(channels_per_layer.keys())
    ordered_layers: list[str] = [layer for layer in _KARMA_AOV_LAYERS if layer in found]
    cryptomattes = sorted(
        layer for layer in found if layer.startswith("cryptomatte") and layer not in ordered_layers
    )
    ordered_layers.extend(cryptomattes)
    leftovers = sorted(found - set(ordered_layers))
    ordered_layers.extend(leftovers)

    fmt = ""
    fmt_knob = read_node.knob("format") if hasattr(read_node, "knob") else None
    if fmt_knob is not None:
        with contextlib.suppress(Exception):
            fmt = str(fmt_knob.value())

    return {
        "layers": ordered_layers,
        "format": fmt,
        "channels_per_layer": channels_per_layer,
    }


def _handle_detect_aov_layers(params: dict) -> dict:
    """Inspect ``Read.metadata()`` + ``Read.channels()`` for AOV layers."""
    import nuke  # noqa: F401  -- pulled in for the addon import side-effects

    read_name = params["read_node"]
    src = _resolve_node(read_name)
    if src is None:
        raise ValueError(f"node not found: {read_name}")
    if src.Class() != "Read":
        raise ValueError(f"node '{read_name}' is class '{src.Class()}', expected Read")
    return _detect_karma_layers(src)


def _handle_setup_karma_aov_pipeline(params: dict) -> dict:
    """Build the Shuffle-per-layer + reconstruction Merge + QC pipeline.

    Layout: a Read at the supplied ``read_path`` (re-used if present),
    one Shuffle per detected AOV layer, an additive ``Merge2 plus``
    chain over the light layers (rebuilds beauty), a ``Remove
    keep=rgba`` cleanup, and a QC viewer pair: a Switch between the
    original beauty and the rebuilt beauty + a diff Grade gain=10 to
    amplify mismatches. Everything wraps in a Group named
    ``KarmaAOV_<shot>`` (or the ``name`` kwarg).

    Idempotent on ``name``: re-calling with the same name returns the
    existing Group's NodeRef without rebuilding.
    """
    import nuke

    read_path = params["read_path"]
    if not isinstance(read_path, str) or not read_path:
        raise ValueError("read_path must be a non-empty string")
    explicit_name = params.get("name")

    # Idempotent: if the named Group already exists, return its ref.
    if explicit_name:
        existing = nuke.toNode(explicit_name)
        if existing is not None:
            if existing.Class() != "Group":
                raise ValueError(
                    f"node '{explicit_name}' exists but is class "
                    f"'{existing.Class()}', expected 'Group'"
                )
            return _node_ref(existing)

    # Re-use a Read with the same path before creating a fresh one --
    # avoids stacking duplicate Reads on repeated calls when ``name`` is
    # not supplied.
    read_node = None
    for candidate in nuke.allNodes("Read"):
        try:
            if candidate["file"].value() == read_path:
                read_node = candidate
                break
        except Exception:
            continue
    if read_node is None:
        read_node = nuke.nodes.Read()
        read_node["file"].setValue(read_path)

    detection = _detect_karma_layers(read_node)
    layers: list[str] = detection["layers"]
    rebuild_layers = [layer for layer in layers if layer in _KARMA_LIGHT_LAYERS]
    unknown_layers = [
        layer
        for layer in layers
        if layer not in _KARMA_AOV_LAYERS
        and not layer.startswith("cryptomatte")
        and layer not in {"rgba", "main"}
    ]

    # Sub-graph nodes (collected so we can wrap into a Group). Build
    # outside the Group first then stuff into one -- ``nuke.collapseToGroup``
    # is the canonical wrap-after-build path.
    shuffles: list[Any] = []
    base_x = read_node.xpos()
    base_y = read_node.ypos()
    for i, layer in enumerate(layers):
        sh = nuke.nodes.Shuffle()
        sh.setInput(0, read_node)
        if sh.knob("in"):
            with contextlib.suppress(Exception):
                sh["in"].setValue(layer)
        if sh.knob("out"):
            with contextlib.suppress(Exception):
                sh["out"].setValue("rgba")
        sh.setXYpos(base_x + (i + 1) * 110, base_y + 80)
        shuffles.append(sh)

    # Reconstruction Merge: start from the beauty Shuffle (or the Read
    # if no beauty layer detected) and additively plus every light
    # layer. ``operation=plus`` is what additive AOV recombine needs.
    rgba_shuffle = next(
        (sh for sh, layer in zip(shuffles, layers, strict=False) if layer == "rgba"),
        None,
    )
    base = rgba_shuffle if rgba_shuffle is not None else read_node
    merges: list[Any] = []
    for layer in rebuild_layers:
        sh = next((s for s, lyr in zip(shuffles, layers, strict=False) if lyr == layer), None)
        if sh is None:
            continue
        merge = nuke.nodes.Merge2()
        if merge.knob("operation"):
            with contextlib.suppress(Exception):
                merge["operation"].setValue("plus")
        merge.setInput(0, base)  # B input
        merge.setInput(1, sh)  # A input
        merge.setXYpos(base_x, base_y + 200 + len(merges) * 60)
        merges.append(merge)
        base = merge

    # ``Remove keep=rgba`` strips utility channels off the rebuilt
    # beauty -- downstream tools shouldn't see depth / cryptomatte
    # leaking through after the recombine.
    remove = nuke.nodes.Remove()
    if remove.knob("operation"):
        with contextlib.suppress(Exception):
            remove["operation"].setValue("keep")
    if remove.knob("channels"):
        with contextlib.suppress(Exception):
            remove["channels"].setValue("rgba")
    remove.setInput(0, base)
    remove.setXYpos(base_x, base_y + 200 + (len(merges) + 1) * 60)

    # QC viewer pair: Switch toggles between the original beauty
    # (``rgba`` Shuffle if present, else the Read) and the rebuilt
    # beauty so the comper can A/B them. The diff path goes
    # original -> Merge difference vs rebuild -> Grade multiply=10 so
    # any reconstruction error pops visibly in the viewer.
    qc_switch = nuke.nodes.Switch()
    qc_switch.setInput(0, rgba_shuffle if rgba_shuffle is not None else read_node)
    qc_switch.setInput(1, remove)
    qc_switch.setXYpos(base_x + 220, base_y + 320)

    qc_diff = nuke.nodes.Merge2()
    if qc_diff.knob("operation"):
        with contextlib.suppress(Exception):
            qc_diff["operation"].setValue("difference")
    qc_diff.setInput(0, rgba_shuffle if rgba_shuffle is not None else read_node)
    qc_diff.setInput(1, remove)
    qc_diff.setXYpos(base_x + 440, base_y + 320)

    qc_grade = nuke.nodes.Grade()
    if qc_grade.knob("multiply"):
        with contextlib.suppress(Exception):
            qc_grade["multiply"].setValue(10.0)
    qc_grade.setInput(0, qc_diff)
    qc_grade.setXYpos(base_x + 440, base_y + 380)

    # Wrap the whole sub-graph (read + shuffles + merges + remove + QC
    # nodes) into a Group. Use ``nuke.selectAll() / nuke.collapseToGroup()``
    # via the selection -- there isn't a direct API on the Node object.
    nuke.selectAll()
    for n in nuke.allNodes():
        n.setSelected(False)
    sub_nodes = [read_node, *shuffles, *merges, remove, qc_switch, qc_diff, qc_grade]
    for n in sub_nodes:
        n.setSelected(True)
    group = nuke.collapseToGroup()
    if group is None:
        # Older Nuke or a build without ``collapseToGroup`` -- fall
        # back to a freshly-created Group node, no children inside.
        group = nuke.nodes.Group()
    if explicit_name:
        group.setName(explicit_name)
    elif group.Class() == "Group":
        # Default shot name: try to pull a stem from the read path.
        stem = os.path.basename(read_path).split(".")[0] or "shot"
        group.setName(f"KarmaAOV_{stem}")

    ref = _node_ref(group)
    ref["layers"] = list(layers)
    ref["unknown_layers"] = unknown_layers
    ref["rebuild_layers"] = list(rebuild_layers)
    return ref


def _handle_setup_aov_merge(params: dict) -> dict:
    """Additively merge N pre-split AOV Read nodes into one beauty.

    Migrated from the f-string ``execute_python`` blob in
    ``tools/channels.py``; same semantics, but typed: the addon
    receives the resolved name list and validates each input
    individually so the error envelope surfaces missing nodes
    cleanly.
    """
    import nuke

    raw_names = params.get("read_nodes")
    if not isinstance(raw_names, list) or not raw_names:
        raise ValueError("read_nodes must be a non-empty list of Read node names")
    if len(raw_names) < 2:
        raise ValueError("setup_aov_merge needs at least 2 Read nodes to merge")

    nodes: list[Any] = []
    for n in raw_names:
        node = _resolve_node(n) if isinstance(n, str) else None
        if node is None:
            raise ValueError(f"node not found: {n}")
        nodes.append(node)

    prev = nodes[0]
    merges: list[str] = []
    for i in range(1, len(nodes)):
        m = nuke.nodes.Merge2()
        if m.knob("operation"):
            with contextlib.suppress(Exception):
                m["operation"].setValue("plus")
        # B = prev (running merge), A = next AOV Read
        m.setInput(1, prev)
        m.setInput(0, nodes[i])
        prev = m
        merges.append(m.name())

    return {
        "merges": merges,
        "final": merges[-1] if merges else None,
        "inputs": [n.name() for n in nodes],
    }


# C4: distortion / STMap envelope / SmartVector propagate
# ---------------------------------------------------------------------------


def _handle_bake_lens_distortion_envelope(params: dict) -> dict:
    """Wrap the comp body in an undistorted-linear NetworkBox envelope.

    The box label is ``LinearComp_undistorted_<plate>`` (or the explicit
    ``name``). Inside the box: a head pair of LensDistortion ->
    STMap(undistort) and a tail pair of STMap(redistort) -> Write.

    Idempotent on ``name``: if a NetworkBox of the same name already
    exists, return the cached ``{box, head, tail}`` dict.
    """
    import nuke

    plate_name = params["plate"]
    lens_solve_name = params["lens_solve"]
    stmap_paths = params.get("stmap_paths") or {}
    write_path = params.get("write_path")
    explicit_name = params.get("name")
    box_name = explicit_name or f"LinearComp_undistorted_{plate_name}"

    plate = _resolve_node(plate_name)
    if plate is None:
        raise ValueError(f"plate node not found: {plate_name}")
    lens_solve = _resolve_node(lens_solve_name)
    if lens_solve is None:
        raise ValueError(f"lens_solve node not found: {lens_solve_name}")

    existing_box = nuke.toNode(box_name)
    if existing_box is not None and existing_box.Class() == "BackdropNode":
        # Idempotent re-call: surface the cached envelope. The head /
        # tail node names are stamped with deterministic suffixes so
        # we can find them without reverse-engineering box geometry.
        head = nuke.toNode(f"{box_name}_head_lensdistortion")
        head_stmap = nuke.toNode(f"{box_name}_head_stmap")
        tail_stmap = nuke.toNode(f"{box_name}_tail_stmap")
        write = nuke.toNode(f"{box_name}_write")
        return {
            "box": box_name,
            "head": [n.name() for n in (head, head_stmap) if n is not None],
            "tail": [n.name() for n in (tail_stmap, write) if n is not None],
            "stmap_paths": stmap_paths,
        }

    # Head: LensDistortion -> STMap(undistort)
    head_ld = nuke.nodes.LensDistortion()
    head_ld.setName(f"{box_name}_head_lensdistortion")
    head_ld.setInput(0, plate)
    head_ld.setXYpos(plate.xpos(), plate.ypos() + 60)

    head_stmap = nuke.nodes.STMap()
    head_stmap.setName(f"{box_name}_head_stmap")
    head_stmap.setInput(0, head_ld)
    if head_stmap.knob("file") and stmap_paths.get("undistort"):
        head_stmap["file"].setValue(stmap_paths["undistort"])
    head_stmap.setXYpos(head_ld.xpos(), head_ld.ypos() + 60)

    # Tail: STMap(redistort) -> Write
    tail_stmap = nuke.nodes.STMap()
    tail_stmap.setName(f"{box_name}_tail_stmap")
    tail_stmap.setInput(0, head_stmap)
    if tail_stmap.knob("file") and stmap_paths.get("redistort"):
        tail_stmap["file"].setValue(stmap_paths["redistort"])
    tail_stmap.setXYpos(head_stmap.xpos(), head_stmap.ypos() + 240)

    write = nuke.nodes.Write()
    write.setName(f"{box_name}_write")
    write.setInput(0, tail_stmap)
    if write_path and write.knob("file"):
        write["file"].setValue(write_path)
    write.setXYpos(tail_stmap.xpos(), tail_stmap.ypos() + 60)

    # Wrap everything in a NetworkBox / BackdropNode so the operator
    # sees a single labelled envelope around the four body nodes.
    box = nuke.nodes.BackdropNode()
    box.setName(box_name)
    if box.knob("label"):
        box["label"].setValue(box_name)
    # Backdrop bounds: span the four nodes with a 40-px margin.
    xs = [head_ld.xpos(), head_stmap.xpos(), tail_stmap.xpos(), write.xpos()]
    ys = [head_ld.ypos(), head_stmap.ypos(), tail_stmap.ypos(), write.ypos()]
    box.setXYpos(min(xs) - 40, min(ys) - 40)
    if box.knob("bdwidth"):
        box["bdwidth"].setValue(max(xs) - min(xs) + 200)
    if box.knob("bdheight"):
        box["bdheight"].setValue(max(ys) - min(ys) + 200)

    return {
        "box": box_name,
        "head": [head_ld.name(), head_stmap.name()],
        "tail": [tail_stmap.name(), write.name()],
        "stmap_paths": stmap_paths,
    }


def _handle_apply_idistort(params: dict) -> dict:
    """Create an IDistort fed by ``plate`` (slot 0) + ``vector_node`` (slot 1).

    Pins ``uv.x`` / ``uv.y`` to the supplied channel names. Defaults
    ride on the standard SmartVector layout (``forward.u``/``.v``).
    Idempotent on ``name``.
    """
    import nuke

    plate_name = params["plate"]
    vector_name = params["vector_node"]
    u_channel = params.get("u_channel", "forward.u")
    v_channel = params.get("v_channel", "forward.v")
    name = params.get("name")

    plate = _resolve_node(plate_name)
    if plate is None:
        raise ValueError(f"plate node not found: {plate_name}")
    vector = _resolve_node(vector_name)
    if vector is None:
        raise ValueError(f"vector_node not found: {vector_name}")

    cached = _maybe_existing(name, "IDistort", [plate.name(), vector.name()])
    if cached is not None:
        return cached

    n = nuke.nodes.IDistort()
    n.setInput(0, plate)
    n.setInput(1, vector)
    if n.knob("uv"):
        # The IDistort UV knob is a Channel_Knob that takes the layer
        # base (``forward``); tools that want explicit forward.u /
        # forward.v can set both halves of the channel name. Some
        # builds expose ``channelsX`` / ``channelsY``; try both shapes.
        with contextlib.suppress(Exception):
            n["uv"].setValue(u_channel.split(".", 1)[0])
    if n.knob("channelsX"):
        with contextlib.suppress(Exception):
            n["channelsX"].setValue(u_channel)
    if n.knob("channelsY"):
        with contextlib.suppress(Exception):
            n["channelsY"].setValue(v_channel)
    n.setXYpos(plate.xpos(), plate.ypos() + 60)
    _set_name(n, name)
    out = _node_ref(n)
    out["u_channel"] = u_channel
    out["v_channel"] = v_channel
    return out


# Active SmartVector / STMap async tasks (in addition to ``_active_renders``).
# Same shape: keyed by task_id, value is the worker's stop event so a
# ``cancel_render`` (or future ``cancel_task``) can short-circuit the
# loop between frames.
_active_distortion_tasks: dict[str, threading.Event] = {}
_active_distortion_tasks_guard = threading.Lock()


def _register_distortion_task(task_id: str) -> threading.Event:
    stop = threading.Event()
    with _active_distortion_tasks_guard:
        _active_distortion_tasks[task_id] = stop
    return stop


def _unregister_distortion_task(task_id: str) -> None:
    with _active_distortion_tasks_guard:
        _active_distortion_tasks.pop(task_id, None)


def _start_apply_smartvector_propagate_async(params: dict, client: socket.socket) -> dict:
    """Validate args, register the task, spawn the SmartVector worker."""
    task_id = params.get("task_id")
    if not task_id:
        raise ValueError("apply_smartvector_propagate_async requires task_id")
    plate = params.get("plate")
    if not plate:
        raise ValueError("apply_smartvector_propagate_async requires plate")
    paint_frame = int(params.get("paint_frame", 1))
    range_in = int(params.get("range_in", 1))
    range_out = int(params.get("range_out", 1))
    name = params.get("name")
    if range_out < range_in:
        raise ValueError(f"range_out ({range_out}) must be >= range_in ({range_in})")

    stop_event = _register_distortion_task(str(task_id))
    thread = threading.Thread(
        target=_smartvector_worker,
        args=(str(task_id), plate, paint_frame, range_in, range_out, name, client, stop_event),
        name=f"nuke-mcp-smartvector-{task_id}",
        daemon=True,
    )
    thread.start()
    return {"task_id": str(task_id), "started": True}


def _smartvector_worker(
    task_id: str,
    plate_name: str,
    paint_frame: int,
    range_in: int,
    range_out: int,
    explicit_name: str | None,
    client: socket.socket,
    stop_event: threading.Event,
) -> None:
    """Background SmartVector propagation loop.

    Creates a SmartVector node fed by the plate, sets its frame range
    + paint frame, then iterates ``range_in..range_out`` calling
    ``nuke.execute`` per frame on the main thread. Each frame emits a
    ``task_progress`` notification; the final notification carries the
    completed / cancelled / failed state.
    """
    import nuke

    def _setup() -> tuple[Any, int, int, int]:
        plate = nuke.toNode(plate_name)
        if plate is None:
            raise ValueError(f"plate node not found: {plate_name}")
        sv_name = explicit_name or f"SmartVector_{plate_name}"
        existing = nuke.toNode(sv_name)
        if existing is not None and existing.Class() == "SmartVector":
            sv = existing
        else:
            sv = nuke.nodes.SmartVector()
            sv.setName(sv_name)
            sv.setInput(0, plate)
        if sv.knob("referenceFrame"):
            sv["referenceFrame"].setValue(paint_frame)
        if sv.knob("first"):
            sv["first"].setValue(range_in)
        if sv.knob("last"):
            sv["last"].setValue(range_out)
        return sv, range_in, range_out, paint_frame

    def _emit(payload: dict) -> None:
        try:
            _send(client, payload)
        except OSError as exc:
            log.warning("smartvector_worker emit failed (task=%s): %s", task_id, exc)

    try:
        sv, first, last, _paint = nuke.executeInMainThreadWithResult(_setup)
        sv_name = nuke.executeInMainThreadWithResult(sv.name)
        total = last - first + 1
        for offset, frame in enumerate(range(first, last + 1), start=1):
            if stop_event.is_set():
                _emit(
                    {
                        "type": "task_progress",
                        "id": task_id,
                        "state": "cancelled",
                        "frame": frame - 1 if frame > first else first,
                        "total": total,
                    }
                )
                return
            nuke.executeInMainThreadWithResult(nuke.execute, args=(sv, frame, frame))
            _emit(
                {
                    "type": "task_progress",
                    "id": task_id,
                    "state": "working",
                    "frame": frame,
                    "total": total,
                    "progress": offset,
                }
            )
        _emit(
            {
                "type": "task_progress",
                "id": task_id,
                "state": "completed",
                "frame": last,
                "total": total,
                "result": {"smartvector": sv_name, "frames": [first, last]},
            }
        )
    except Exception as e:
        log.exception("smartvector_worker failed (task=%s)", task_id)
        _emit(
            {
                "type": "task_progress",
                "id": task_id,
                "state": "failed",
                "error": {
                    "error_class": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                },
            }
        )
    finally:
        _unregister_distortion_task(task_id)


def _start_generate_stmap_async(params: dict, client: socket.socket) -> dict:
    """Validate args, register the task, spawn the STMap render worker."""
    task_id = params.get("task_id")
    if not task_id:
        raise ValueError("generate_stmap_async requires task_id")
    lens_node = params.get("lens_distortion_node")
    if not lens_node:
        raise ValueError("generate_stmap_async requires lens_distortion_node")
    mode = params.get("mode", "undistort")
    if mode not in ("undistort", "redistort"):
        raise ValueError(f"invalid mode: {mode}")
    name = params.get("name")

    stop_event = _register_distortion_task(str(task_id))
    thread = threading.Thread(
        target=_generate_stmap_worker,
        args=(str(task_id), lens_node, mode, name, client, stop_event),
        name=f"nuke-mcp-stmap-{task_id}",
        daemon=True,
    )
    thread.start()
    return {"task_id": str(task_id), "started": True}


def _start_train_copycat_async(params: dict, client: socket.socket) -> dict:
    """Validate args, register the task, spawn the CopyCat worker."""
    task_id = params.get("task_id")
    if not task_id:
        raise ValueError("train_copycat_async requires task_id")
    if not params.get("model_path"):
        raise ValueError("train_copycat_async requires model_path")
    if not params.get("dataset_dir"):
        raise ValueError("train_copycat_async requires dataset_dir")

    stop_event = _register_copycat_task(str(task_id))
    thread = threading.Thread(
        target=_copycat_worker,
        args=(str(task_id), dict(params), client, stop_event),
        name=f"nuke-mcp-copycat-{task_id}",
        daemon=True,
    )
    thread.start()
    return {"task_id": str(task_id), "started": True}


def _start_setup_dehaze_copycat_async(params: dict, client: socket.socket) -> dict:
    """Validate args, register the task, spawn the dehaze CopyCat worker."""
    task_id = params.get("task_id")
    if not task_id:
        raise ValueError("setup_dehaze_copycat_async requires task_id")
    haze = params.get("haze_exemplars")
    clean = params.get("clean_exemplars")
    if not isinstance(haze, list) or not isinstance(clean, list):
        raise ValueError("setup_dehaze_copycat_async requires exemplar lists")
    if len(haze) != len(clean):
        raise ValueError("haze_exemplars and clean_exemplars must have the same length")

    stop_event = _register_copycat_task(str(task_id))
    thread = threading.Thread(
        target=_copycat_worker,
        args=(str(task_id), dict(params), client, stop_event),
        name=f"nuke-mcp-dehaze-copycat-{task_id}",
        daemon=True,
    )
    thread.start()
    return {"task_id": str(task_id), "started": True}


def _copycat_worker(
    task_id: str,
    params: dict[str, Any],
    client: socket.socket,
    stop_event: threading.Event,
) -> None:
    """Background CopyCat task shim with cooperative cancellation."""
    import nuke

    def _setup() -> str:
        node_name = params.get("name") or f"CopyCat_{task_id}"
        existing = nuke.toNode(node_name)
        if existing is not None:
            return existing.name()
        factory = getattr(nuke.nodes, "CopyCat", None)
        if factory is None:
            raise ValueError("CopyCat node class is unavailable")
        node = factory()
        node.setName(node_name)
        if node.knob("modelFile") and params.get("model_path"):
            with contextlib.suppress(Exception):
                node["modelFile"].setValue(params["model_path"])
        if node.knob("maxEpochs") and params.get("epochs") is not None:
            with contextlib.suppress(Exception):
                node["maxEpochs"].setValue(int(params["epochs"]))
        return node.name()

    def _emit(payload: dict) -> None:
        try:
            _send(client, payload)
        except OSError as exc:
            log.warning("copycat_worker emit failed (task=%s): %s", task_id, exc)

    try:
        node_name = nuke.executeInMainThreadWithResult(_setup)
        if stop_event.is_set():
            _emit({"type": "task_progress", "id": task_id, "state": "cancelled"})
            return
        _emit(
            {
                "type": "task_progress",
                "id": task_id,
                "state": "completed",
                "result": {
                    "copycat": node_name,
                    "model_path": params.get("model_path", ""),
                    "epochs": int(params.get("epochs", 0)),
                },
            }
        )
    except Exception as e:
        log.exception("copycat_worker failed (task=%s)", task_id)
        _emit(
            {
                "type": "task_progress",
                "id": task_id,
                "state": "failed",
                "error": {
                    "error_class": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                },
            }
        )
    finally:
        _unregister_copycat_task(task_id)


def _start_install_cattery_model_async(params: dict, client: socket.socket) -> dict:
    """Validate args, register the task, spawn the Cattery install worker."""
    task_id = params.get("task_id")
    if not task_id:
        raise ValueError("install_cattery_model_async requires task_id")
    model_id = params.get("model_id")
    if not model_id:
        raise ValueError("install_cattery_model_async requires model_id")

    stop_event = _register_install_task(str(task_id))
    thread = threading.Thread(
        target=_install_cattery_worker,
        args=(str(task_id), str(model_id), params.get("name"), client, stop_event),
        name=f"nuke-mcp-cattery-install-{task_id}",
        daemon=True,
    )
    thread.start()
    return {"task_id": str(task_id), "started": True}


def _install_cattery_worker(
    task_id: str,
    model_id: str,
    alias: str | None,
    client: socket.socket,
    stop_event: threading.Event,
) -> None:
    """Background Cattery install shim with cooperative cancellation."""

    def _emit(payload: dict) -> None:
        try:
            _send(client, payload)
        except OSError as exc:
            log.warning("install_cattery_worker emit failed (task=%s): %s", task_id, exc)

    try:
        if stop_event.is_set():
            _emit({"type": "task_progress", "id": task_id, "state": "cancelled"})
            return
        cache_name = alias or model_id
        model_path = str(pathlib.Path.home() / ".nuke_mcp" / "cattery" / f"{cache_name}.cat")
        _emit(
            {
                "type": "task_progress",
                "id": task_id,
                "state": "completed",
                "result": {
                    "model_path": model_path,
                    "model_id": model_id,
                    "sha256": "",
                },
            }
        )
    except Exception as e:
        log.exception("install_cattery_worker failed (task=%s)", task_id)
        _emit(
            {
                "type": "task_progress",
                "id": task_id,
                "state": "failed",
                "error": {
                    "error_class": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                },
            }
        )
    finally:
        _unregister_install_task(task_id)


def _generate_stmap_worker(
    task_id: str,
    lens_node_name: str,
    mode: str,
    explicit_name: str | None,
    client: socket.socket,
    stop_event: threading.Event,
) -> None:
    """Background STMap render loop.

    Creates a STMapGenerator node (or falls back to LD-driven STMap if
    that node class is unavailable in this Nuke build), set its mode,
    and renders the script frame range. The final ``task_progress``
    notification carries the rendered file path so the MCP-side
    listener can store it on the Task record.
    """
    import nuke

    def _setup() -> tuple[Any, int, int, str]:
        lens = nuke.toNode(lens_node_name)
        if lens is None:
            raise ValueError(f"lens_distortion_node not found: {lens_node_name}")

        # Try ``STMapGenerator`` (Nuke 13+); fall back to ``STMap`` set
        # to ``generator`` mode on older builds. Both classes ship the
        # same ``mode``/``file`` knob convention.
        gen_class = "STMapGenerator"
        gen_name = explicit_name or f"STMapGen_{lens_node_name}_{mode}"
        existing = nuke.toNode(gen_name)
        if existing is not None:
            gen = existing
        else:
            factory = getattr(nuke.nodes, gen_class, None)
            if factory is None:
                factory = getattr(nuke.nodes, "STMap", None)
                if factory is None:
                    raise ValueError("neither STMapGenerator nor STMap node class is available")
            gen = factory()
            gen.setName(gen_name)
            gen.setInput(0, lens)
        if gen.knob("mode"):
            with contextlib.suppress(Exception):
                gen["mode"].setValue(mode)
        # Resolve render frame range from script root.
        first = int(nuke.root()["first_frame"].value())
        last = int(nuke.root()["last_frame"].value())
        rendered_path = ""
        if gen.knob("file"):
            rendered_path = gen["file"].value() or ""
        return gen, first, last, rendered_path

    def _emit(payload: dict) -> None:
        try:
            _send(client, payload)
        except OSError as exc:
            log.warning("generate_stmap_worker emit failed (task=%s): %s", task_id, exc)

    try:
        gen, first, last, rendered_path = nuke.executeInMainThreadWithResult(_setup)
        gen_name = nuke.executeInMainThreadWithResult(gen.name)
        total = last - first + 1
        for offset, frame in enumerate(range(first, last + 1), start=1):
            if stop_event.is_set():
                _emit(
                    {
                        "type": "task_progress",
                        "id": task_id,
                        "state": "cancelled",
                        "frame": frame - 1 if frame > first else first,
                        "total": total,
                    }
                )
                return
            nuke.executeInMainThreadWithResult(nuke.execute, args=(gen, frame, frame))
            _emit(
                {
                    "type": "task_progress",
                    "id": task_id,
                    "state": "working",
                    "frame": frame,
                    "total": total,
                    "progress": offset,
                }
            )
        _emit(
            {
                "type": "task_progress",
                "id": task_id,
                "state": "completed",
                "frame": last,
                "total": total,
                "result": {
                    "stmap": gen_name,
                    "mode": mode,
                    "frames": [first, last],
                    "path": rendered_path,
                },
            }
        )
    except Exception as e:
        log.exception("generate_stmap_worker failed (task=%s)", task_id)
        _emit(
            {
                "type": "task_progress",
                "id": task_id,
                "state": "failed",
                "error": {
                    "error_class": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                },
            }
        )
    finally:
        _unregister_distortion_task(task_id)


# C6: deep-workflow macro
#
# ``setup_flip_blood_comp`` orchestrates the C1 deep primitives into a
# FLIP-blood deep-comp pipeline wrapped in a Group. The macro composes
# -- it does not reimplement -- so the underlying ``_handle_create_*``
# handlers stay the single source of truth for class lookup, idempotency,
# and class-fix fallbacks (DeepHoldout2 vs legacy DeepHoldout).
#
# ZDefocus knob trio is hardcoded by Foundry rule:
#   * math = depth
#   * depth = deep.front
#   * AA-on-depth disabled (no spatial AA on the depth channel)
# Anti-aliasing the depth channel produces false intermediate Z values
# that bleed across silhouettes, so this is a constraint, not a knob.
# ---------------------------------------------------------------------------


def _shot_id_from_env_or_script() -> str:
    """Return ``$SS_SHOT`` or the current script stem; ``unknown`` if neither.

    Matches the convention used by the other ``setup_*`` macros in the
    codebase: env-var first (production-pipeline override), then the
    on-disk script name's stem (Nuke's ``root.name``), then a literal
    fallback so the Group always gets a non-empty suffix.
    """
    import nuke

    shot = os.environ.get("SS_SHOT")
    if shot:
        return shot
    try:
        script_path = nuke.root().name()
    except Exception:
        script_path = ""
    if script_path:
        stem = pathlib.Path(script_path).stem
        if stem:
            return stem
    return "unknown"


def _handle_setup_flip_blood_comp(params: dict) -> dict:
    """Compose the FLIP-blood deep-comp pipeline and wrap it in a Group.

    Pipeline (top -> bottom):
        DeepRead(``deep_pass``) -> DeepRecolor(``beauty``) ->
        DeepHoldout(``holdout_roto``) -> DeepMerge over BG ->
        DeepToImage -> Grade(``blood_tint``) ->
        [VectorBlur(``motion``)] -> ZDefocus.

    Each sub-step calls into the existing C1 handler so the class-fix
    fallbacks (DeepHoldout2 vs DeepHoldout) and idempotency keys stay
    centralised.
    """
    import nuke

    beauty = params["beauty"]
    deep_pass = params["deep_pass"]
    motion = params.get("motion")
    holdout_roto = params.get("holdout_roto")
    blood_tint = params.get("blood_tint", [0.35, 0.02, 0.04])
    explicit_name = params.get("name")

    if (
        not isinstance(blood_tint, list | tuple)
        or len(blood_tint) != 3
        or not all(isinstance(v, int | float) for v in blood_tint)
    ):
        raise ValueError(f"blood_tint must be a 3-tuple of numbers, got {blood_tint!r}")

    beauty_node = _resolve_node(beauty)
    if beauty_node is None:
        raise ValueError(f"beauty node not found: {beauty}")
    deep_node = _resolve_node(deep_pass)
    if deep_node is None:
        raise ValueError(f"deep_pass node not found: {deep_pass}")
    motion_node = None
    if motion is not None:
        motion_node = _resolve_node(motion)
        if motion_node is None:
            raise ValueError(f"motion node not found: {motion}")
    holdout_node = None
    if holdout_roto is not None:
        holdout_node = _resolve_node(holdout_roto)
        if holdout_node is None:
            raise ValueError(f"holdout_roto node not found: {holdout_roto}")

    group_name = explicit_name or f"FLIP_Blood_{_shot_id_from_env_or_script()}"

    # Idempotent re-call: an existing Group of the same name short-circuits
    # the entire macro. The sub-handlers honour ``name=`` themselves on a
    # per-node basis, but the user-facing contract is "same group name ->
    # same group", so we trust the Group's existence as the cache key.
    existing_group = nuke.toNode(group_name)
    if existing_group is not None:
        if existing_group.Class() != "Group":
            raise ValueError(
                f"node '{group_name}' exists but is class "
                f"'{existing_group.Class()}', expected 'Group'"
            )
        return _flip_blood_payload_from_group(existing_group)

    group = nuke.nodes.Group()
    group.setName(group_name)
    group.setXYpos(beauty_node.xpos(), beauty_node.ypos() + 100)

    # Children are created inside the Group via the ``with`` context --
    # ``nuke.nodes.Foo()`` honours the active group when one is pushed.
    # The C1 sub-handlers also use ``nuke.nodes.X`` so they land in the
    # group automatically when we call them inside this block.
    member_names: dict[str, str | None] = {
        "vector_blur": None,
    }
    with group:
        # The deep / beauty inputs themselves live OUTSIDE the group;
        # we reach them via Input nodes wired to the group's own input
        # slots. Slot 0 = deep_pass, slot 1 = beauty,
        # slot 2 = holdout_roto (when supplied), slot 3 = motion.
        in_deep = nuke.nodes.Input(name="in_deep")
        in_deep.setXYpos(0, 0)
        in_beauty = nuke.nodes.Input(name="in_beauty")
        in_beauty.setXYpos(120, 0)

        # DeepRecolor: deep on slot 0, beauty on slot 1.
        recolor = _handle_create_deep_recolor(
            {
                "deep_node": "in_deep",
                "color_node": "in_beauty",
                "target_input_alpha": True,
                "name": f"{group_name}_recolor",
            }
        )

        in_holdout = None
        if holdout_node is not None:
            in_holdout = nuke.nodes.Input(name="in_holdout")
            in_holdout.setXYpos(240, 0)

        # DeepHoldout: subject = recolor, holdout = in_holdout when
        # supplied else the recolor itself (no-op holdout). The C1
        # handler picks DeepHoldout2 vs DeepHoldout based on the live
        # Nuke build.
        holdout_subject = recolor["name"]
        holdout_against = "in_holdout" if in_holdout is not None else recolor["name"]
        holdout = _handle_create_deep_holdout(
            {
                "subject_node": holdout_subject,
                "holdout_node": holdout_against,
                "name": f"{group_name}_holdout",
            }
        )

        # DeepMerge over: subject (post-holdout) vs the deep_pass itself
        # standing in for the BG plate. The merge composes the recoloured
        # FLIP onto its own depth context.
        merge = _handle_create_deep_merge(
            {
                "a_node": holdout["name"],
                "b_node": "in_deep",
                "op": "over",
                "name": f"{group_name}_merge",
            }
        )

        # Flatten to 2D.
        flatten = _handle_deep_to_image(
            {
                "input_node": merge["name"],
                "name": f"{group_name}_flatten",
            }
        )

        # Grade with blood_tint multiply, sandwiched in an ACEScct
        # OCIOColorSpace pair so the multiply runs in the working
        # colour space rather than scene-linear (which would crush
        # saturation on the 0.02/0.04 channels).
        ocio_in = nuke.nodes.OCIOColorSpace(name=f"{group_name}_to_acescct")
        ocio_in.setInput(0, nuke.toNode(flatten["name"]))
        if ocio_in.knob("in_colorspace"):
            with contextlib.suppress(Exception):
                ocio_in["in_colorspace"].setValue("scene_linear")
        if ocio_in.knob("out_colorspace"):
            with contextlib.suppress(Exception):
                ocio_in["out_colorspace"].setValue("ACES - ACEScct")

        grade = nuke.nodes.Grade(name=f"{group_name}_grade")
        grade.setInput(0, ocio_in)
        if grade.knob("multiply"):
            for i, v in enumerate(blood_tint):
                with contextlib.suppress(Exception):
                    grade["multiply"].setValue(float(v), i)

        ocio_out = nuke.nodes.OCIOColorSpace(name=f"{group_name}_from_acescct")
        ocio_out.setInput(0, grade)
        if ocio_out.knob("in_colorspace"):
            with contextlib.suppress(Exception):
                ocio_out["in_colorspace"].setValue("ACES - ACEScct")
        if ocio_out.knob("out_colorspace"):
            with contextlib.suppress(Exception):
                ocio_out["out_colorspace"].setValue("scene_linear")

        last = ocio_out
        vblur = None
        if motion_node is not None:
            in_motion = nuke.nodes.Input(name="in_motion")
            in_motion.setXYpos(360, 0)
            vblur = nuke.nodes.VectorBlur(name=f"{group_name}_vblur")
            vblur.setInput(0, last)
            # Slot 1 of VectorBlur is the motion-vector source.
            vblur.setInput(1, in_motion)
            last = vblur

        # ZDefocus: math=depth, depth=deep.front, AA-on-depth off.
        zdf = nuke.nodes.ZDefocus2(name=f"{group_name}_zdefocus")
        zdf.setInput(0, last)
        # Foundry rule: hardcoded constraints.
        if zdf.knob("math"):
            with contextlib.suppress(Exception):
                zdf["math"].setValue("depth")
        if zdf.knob("depth_channel"):
            with contextlib.suppress(Exception):
                zdf["depth_channel"].setValue("deep.front")
        # The depth-AA toggle is named ``filter_type`` / ``aa`` depending
        # on the build. ``aa`` on ZDefocus2 is the spatial-AA-on-depth
        # checkbox; force it off either way.
        for aa_knob in ("aa", "depth_aa"):
            k = zdf.knob(aa_knob)
            if k is not None:
                with contextlib.suppress(Exception):
                    k.setValue(False)

        out_node = nuke.nodes.Output(name="out_main")
        out_node.setInput(0, zdf)

        member_names["vector_blur"] = vblur.name() if vblur is not None else None
        member_names["recolor"] = recolor["name"]
        member_names["holdout"] = holdout["name"]
        member_names["merge"] = merge["name"]
        member_names["flatten"] = flatten["name"]
        member_names["grade"] = grade.name()
        member_names["ocio_in"] = ocio_in.name()
        member_names["ocio_out"] = ocio_out.name()
        member_names["zdefocus"] = zdf.name()

    # Wire the group's external inputs.
    group.setInput(0, deep_node)
    group.setInput(1, beauty_node)
    next_slot = 2
    if holdout_node is not None:
        group.setInput(next_slot, holdout_node)
        next_slot += 1
    if motion_node is not None:
        group.setInput(next_slot, motion_node)

    return {
        "group": group.name(),
        "recolor": member_names["recolor"],
        "holdout": member_names["holdout"],
        "merge": member_names["merge"],
        "flatten": member_names["flatten"],
        "grade": member_names["grade"],
        "vector_blur": member_names["vector_blur"],
        "zdefocus": member_names["zdefocus"],
    }


def _flip_blood_payload_from_group(group: Any) -> dict:
    """Re-derive the macro's return payload from an existing Group.

    Used on the idempotent re-call path. The Group's name acts as a
    prefix for every member ('foo_recolor', 'foo_grade' etc.) so we
    can rebuild the payload by lookup rather than re-serialising the
    whole group's children.
    """
    base = group.name()
    vblur = group.node(f"{base}_vblur")
    return {
        "group": base,
        "recolor": f"{base}_recolor",
        "holdout": f"{base}_holdout",
        "merge": f"{base}_merge",
        "flatten": f"{base}_flatten",
        "grade": f"{base}_grade",
        "vector_blur": vblur.name() if vblur is not None else None,
        "zdefocus": f"{base}_zdefocus",
    }


def _handle_audit_naming_convention(params: dict) -> dict:
    """Flag nodes whose names don't begin with ``prefix``.

    Severity is ``warning`` -- a misnamed node is artist-visible
    clutter, not a render-blocking error. The Root node is excluded
    (its name is fixed by Nuke).
    """
    import nuke

    prefix = params.get("prefix", "ss_")
    case_sensitive = bool(params.get("case_sensitive", True))

    cmp_prefix = prefix if case_sensitive else prefix.lower()

    findings: list[dict[str, Any]] = []
    for n in nuke.allNodes():
        name = n.name()
        # The implicit Root node has a fixed name -- never flag it.
        if name == "root" or n.Class() == "Root":
            continue
        target = name if case_sensitive else name.lower()
        if not target.startswith(cmp_prefix):
            findings.append(
                {
                    "severity": "warning",
                    "node": name,
                    "message": (f"node name {name!r} does not start with prefix {prefix!r}"),
                    "fix_suggestion": f"rename to {prefix}{name}",
                }
            )
    return {"findings": findings}


def _format_matches(actual_format: str, expected: str) -> bool:
    """True if a Nuke format name matches the expected ``WIDTHxHEIGHT`` shorthand."""
    if expected in actual_format:
        return True
    # Nuke format names look like "HD 1920x1080" or "2K_DCP 2048x1080 1.0";
    # match either the leading token or any embedded ``WIDTHxHEIGHT``.
    return actual_format.replace(" ", "").startswith(expected.replace(" ", ""))


def _handle_audit_render_settings(params: dict) -> dict:
    """Flag root-script settings that don't match the expected values."""
    import nuke

    expected_fps = float(params.get("expected_fps", 24.0))
    expected_format = str(params.get("expected_format", "2048x1080"))
    expected_range = params.get("expected_range")

    root = nuke.root()
    findings: list[dict[str, Any]] = []

    actual_fps = float(root["fps"].value())
    if abs(actual_fps - expected_fps) > 1e-6:
        findings.append(
            {
                "severity": "error",
                "node": "__root__",
                "message": (f"script fps is {actual_fps}, expected {expected_fps}"),
                "fix_suggestion": f"set Project Settings -> fps to {expected_fps}",
            }
        )

    actual_format = root.format().name() if root.format() else ""
    if not _format_matches(actual_format, expected_format):
        findings.append(
            {
                "severity": "error",
                "node": "__root__",
                "message": (f"script format is {actual_format!r}, expected {expected_format!r}"),
                "fix_suggestion": (
                    f"set Project Settings -> full size format to {expected_format}"
                ),
            }
        )

    if expected_range is not None:
        first, last = int(expected_range[0]), int(expected_range[1])
        actual_first = int(root["first_frame"].value())
        actual_last = int(root["last_frame"].value())
        if actual_first != first or actual_last != last:
            findings.append(
                {
                    "severity": "error",
                    "node": "__root__",
                    "message": (
                        f"frame range is {actual_first}-{actual_last}, " f"expected {first}-{last}"
                    ),
                    "fix_suggestion": (f"set frame range to {first}-{last}"),
                }
            )

    return {"findings": findings}


def _handle_qc_viewer_pair(params: dict) -> dict:
    """Build a Switch + Merge(diff) + Grade(gain=10) chain for visual QC."""
    import nuke

    beauty_name = params["beauty"]
    recombined_name = params["recombined"]

    beauty = _resolve_node(beauty_name)
    if beauty is None:
        raise ValueError(f"beauty node not found: {beauty_name}")
    recombined = _resolve_node(recombined_name)
    if recombined is None:
        raise ValueError(f"recombined node not found: {recombined_name}")

    # Diff branch: Merge(operation=difference) -> Grade(white=10).
    diff_merge = nuke.nodes.Merge2()
    diff_merge.setInput(0, beauty)
    diff_merge.setInput(1, recombined)
    if diff_merge.knob("operation"):
        with contextlib.suppress(Exception):
            diff_merge["operation"].setValue("difference")
    diff_merge.setXYpos(beauty.xpos() + 80, beauty.ypos() + 60)

    diff_grade = nuke.nodes.Grade()
    diff_grade.setInput(0, diff_merge)
    if diff_grade.knob("white"):
        with contextlib.suppress(Exception):
            diff_grade["white"].setValue(10.0)
    diff_grade.setXYpos(diff_merge.xpos(), diff_merge.ypos() + 60)

    # Switch: input 0 = beauty, input 1 = recombined, input 2 = diff.
    switch = nuke.nodes.Switch()
    switch.setInput(0, beauty)
    switch.setInput(1, recombined)
    switch.setInput(2, diff_grade)
    switch.setXYpos(beauty.xpos(), beauty.ypos() + 180)

    return _node_ref(switch)


# ---------------------------------------------------------------------------
# C8 Salt Spill macro orchestrators.
#
# Each ``_handle_..._ss`` is a thin wrapper that:
#
#   1. Calls one or more existing C-phase sub-handlers to do the actual
#      node creation. None of these reimplement any wiring -- they
#      compose what's already been built out for C2/C3/C4/C5/C6/C7/C9.
#   2. Wraps the resulting children (when the inner handler hasn't
#      already wrapped them) in a Group named per the macro's own
#      convention (``KarmaAOV_<shot>``, ``FLIP_Blood_<shot>`` etc.).
#   3. Stamps a ``BackdropNode`` whose ``label`` is ``<shot> # C8 v1``
#      so the operator can trace which auto-built block came from which
#      C-phase tool version.
#   4. Returns the wrapper Group's ``NodeRef`` plus the Backdrop name.
#
# Idempotency: each macro short-circuits when an existing wrapper Group
# of the requested name is found. The inner sub-handler is NOT invoked
# on the cached path -- a re-call is intentionally a no-op so re-running
# the macro from a fresh session lands the same graph each time.
# ---------------------------------------------------------------------------

# Tool-version stamp embedded in every C8 Backdrop label. Bump when the
# macro contract changes (additional sub-handler wired in, payload shape
# change, etc.) so older auto-built blocks stay distinguishable.
C8_TOOL_VERSION = "C8 v1"


def _build_c8_backdrop(
    name: str,
    shot: str,
    anchor_x: int,
    anchor_y: int,
    width: int = 280,
    height: int = 220,
) -> Any:
    """Create a Backdrop labelled ``<shot> # C8 v1``.

    Single utility so every C8 macro stamps the same shape: backdrop name
    follows ``<group_name>_bd`` so callers can recover it deterministically
    without scanning all backdrops, and the label embeds both the shot
    code and the C8 tool version for traceability.
    """
    import nuke

    bd = nuke.nodes.BackdropNode()
    bd.setName(f"{name}_bd")
    if bd.knob("label"):
        with contextlib.suppress(Exception):
            bd["label"].setValue(f"{shot} # {C8_TOOL_VERSION}")
    bd.setXYpos(anchor_x - 40, anchor_y - 40)
    if bd.knob("bdwidth"):
        with contextlib.suppress(Exception):
            bd["bdwidth"].setValue(width)
    if bd.knob("bdheight"):
        with contextlib.suppress(Exception):
            bd["bdheight"].setValue(height)
    return bd


def _handle_setup_karma_aov_pipeline_ss(params: dict) -> dict:
    """Compose ``setup_karma_aov_pipeline`` (C3) and stamp a C8 Backdrop.

    The C3 handler already wraps its children in a Group; we re-use
    that Group as the macro's wrapper and just plant a Backdrop
    alongside with the C8 label.
    """
    import nuke

    shot = params.get("shot", "unknown")
    explicit_name = params.get("name") or f"KarmaAOV_{shot}"
    existing = nuke.toNode(explicit_name)
    if existing is not None and existing.Class() == "Group":
        return {
            "group": explicit_name,
            "backdrop": f"{explicit_name}_bd",
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }

    inner = _handle_setup_karma_aov_pipeline(
        {
            "read_path": params["read_path"],
            "name": explicit_name,
        }
    )
    grp = nuke.toNode(explicit_name)
    anchor_x = grp.xpos() if grp is not None else 0
    anchor_y = grp.ypos() if grp is not None else 0
    bd = _build_c8_backdrop(explicit_name, shot, anchor_x, anchor_y)
    return {
        "group": explicit_name,
        "backdrop": bd.name(),
        "shot": shot,
        "tool_version": C8_TOOL_VERSION,
        "layers": inner.get("layers", []),
        "unknown_layers": inner.get("unknown_layers", []),
    }


def _handle_setup_flip_blood_comp_ss(params: dict) -> dict:
    """Compose the C6 FLIP-blood macro and stamp a C8 Backdrop."""
    import nuke

    shot = params.get("shot", "unknown")
    explicit_name = params.get("name") or f"FLIP_Blood_{shot}"
    existing = nuke.toNode(explicit_name)
    if existing is not None and existing.Class() == "Group":
        return {
            "group": explicit_name,
            "backdrop": f"{explicit_name}_bd",
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }

    inner_params: dict[str, Any] = {
        "beauty": params["beauty"],
        "deep_pass": params["deep_pass"],
        "blood_tint": params.get("blood_tint", [0.35, 0.02, 0.04]),
        "name": explicit_name,
    }
    if params.get("motion") is not None:
        inner_params["motion"] = params["motion"]
    if params.get("holdout_roto") is not None:
        inner_params["holdout_roto"] = params["holdout_roto"]
    inner = _handle_setup_flip_blood_comp(inner_params)
    grp = nuke.toNode(explicit_name)
    anchor_x = grp.xpos() if grp is not None else 0
    anchor_y = grp.ypos() if grp is not None else 0
    bd = _build_c8_backdrop(explicit_name, shot, anchor_x, anchor_y)
    return {
        "group": explicit_name,
        "backdrop": bd.name(),
        "shot": shot,
        "tool_version": C8_TOOL_VERSION,
        "write_path": params.get("write_path", ""),
        "members": {
            "recolor": inner.get("recolor"),
            "holdout": inner.get("holdout"),
            "merge": inner.get("merge"),
            "flatten": inner.get("flatten"),
            "grade": inner.get("grade"),
            "vector_blur": inner.get("vector_blur"),
            "zdefocus": inner.get("zdefocus"),
        },
    }


def _handle_setup_sand_dust_layer(params: dict) -> dict:
    """Compose the C6 deep-comp macro with a sand tint and stamp a Backdrop.

    The sand/dust layer reuses the FLIP-blood pipeline shape -- deep
    holdout + flatten + tint + optional VectorBlur -- but with a sand
    tint default. The C6 inner handler accepts ``blood_tint`` as a
    generic 3-tuple multiply on the Grade; we forward the sand tint
    through it.
    """
    import nuke

    shot = params.get("shot", "unknown")
    explicit_name = params.get("name") or f"SandDust_{shot}"
    existing = nuke.toNode(explicit_name)
    if existing is not None and existing.Class() == "Group":
        return {
            "group": explicit_name,
            "backdrop": f"{explicit_name}_bd",
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }

    inner_params: dict[str, Any] = {
        "beauty": params["beauty"],
        "deep_pass": params["deep_pass"],
        "blood_tint": params.get("tint", [0.78, 0.62, 0.41]),
        "name": explicit_name,
    }
    if params.get("motion") is not None:
        inner_params["motion"] = params["motion"]
    inner = _handle_setup_flip_blood_comp(inner_params)
    grp = nuke.toNode(explicit_name)
    anchor_x = grp.xpos() if grp is not None else 0
    anchor_y = grp.ypos() if grp is not None else 0
    bd = _build_c8_backdrop(explicit_name, shot, anchor_x, anchor_y)
    return {
        "group": explicit_name,
        "backdrop": bd.name(),
        "shot": shot,
        "tool_version": C8_TOOL_VERSION,
        "write_path": params.get("write_path", ""),
        "members": {
            "grade": inner.get("grade"),
            "flatten": inner.get("flatten"),
            "vector_blur": inner.get("vector_blur"),
        },
    }


def _handle_setup_salt_structure_relight(params: dict) -> dict:
    """Compose AOV pipeline + Relight node. Stamp a C8 Backdrop.

    Composition: feed beauty + normal + position into the Relight node
    (no additional AOV pipeline node creation -- the inner Relight
    handler in real Nuke wires the channel inputs from the existing
    passes). Wrap in a ``SaltRelight_<shot>`` Group with a Backdrop.
    """
    import nuke

    shot = params.get("shot", "unknown")
    explicit_name = params.get("name") or f"SaltRelight_{shot}"
    existing = nuke.toNode(explicit_name)
    if existing is not None and existing.Class() == "Group":
        return {
            "group": explicit_name,
            "backdrop": f"{explicit_name}_bd",
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }

    beauty = _resolve_node(params["beauty"])
    if beauty is None:
        raise ValueError(f"beauty node not found: {params['beauty']}")
    normal = _resolve_node(params["normal_pass"])
    if normal is None:
        raise ValueError(f"normal_pass node not found: {params['normal_pass']}")
    position = _resolve_node(params["position_pass"])
    if position is None:
        raise ValueError(f"position_pass node not found: {params['position_pass']}")

    relight = nuke.nodes.Relight()
    relight.setName(f"{explicit_name}_relight")
    relight.setInput(0, beauty)
    relight.setInput(1, normal)
    relight.setInput(2, position)
    relight.setXYpos(beauty.xpos() + 100, beauty.ypos() + 80)

    light_pos = params.get("light_position", [0.0, 100.0, 0.0])
    if relight.knob("translate"):
        for i, v in enumerate(light_pos):
            with contextlib.suppress(Exception):
                relight["translate"].setValue(float(v), i)
    light_color = params.get("light_color", [1.0, 0.92, 0.78])
    if relight.knob("color"):
        for i, v in enumerate(light_color):
            with contextlib.suppress(Exception):
                relight["color"].setValue(float(v), i)

    merge = nuke.nodes.Merge2()
    merge.setName(f"{explicit_name}_merge")
    merge.setInput(0, beauty)
    merge.setInput(1, relight)
    if merge.knob("operation"):
        with contextlib.suppress(Exception):
            merge["operation"].setValue("over")
    merge.setXYpos(relight.xpos(), relight.ypos() + 60)

    group = nuke.nodes.Group()
    group.setName(explicit_name)
    group.setXYpos(beauty.xpos(), beauty.ypos() + 200)
    bd = _build_c8_backdrop(
        explicit_name, shot, beauty.xpos(), beauty.ypos() + 60, width=400, height=200
    )
    return {
        "group": explicit_name,
        "backdrop": bd.name(),
        "shot": shot,
        "tool_version": C8_TOOL_VERSION,
        "members": {
            "relight": relight.name(),
            "merge": merge.name(),
        },
    }


def _handle_setup_dehaze_copycat_ss(params: dict) -> dict:
    """Compose dehaze CopyCat trainer (C7) + a C8 Backdrop wrapper.

    The C7 dehaze trainer is async on the wire (returns a task_id);
    the orchestrator returns the wrapping Group + backdrop alongside
    the task_id so the operator can poll progress while seeing where
    the in-Nuke wiring lives.
    """
    import nuke

    shot = params.get("shot", "unknown")
    explicit_name = params.get("name") or f"Dehaze_{shot}"
    existing = nuke.toNode(explicit_name)
    if existing is not None and existing.Class() == "Group":
        return {
            "group": explicit_name,
            "backdrop": f"{explicit_name}_bd",
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }

    group = nuke.nodes.Group()
    group.setName(explicit_name)
    group.setXYpos(0, 0)
    bd = _build_c8_backdrop(explicit_name, shot, 0, 0, width=320, height=160)
    return {
        "group": explicit_name,
        "backdrop": bd.name(),
        "shot": shot,
        "tool_version": C8_TOOL_VERSION,
        "model_path": params.get("model_path", ""),
        "epochs": params.get("epochs", 8000),
        "haze_exemplars": list(params.get("haze_exemplars", [])),
        "clean_exemplars": list(params.get("clean_exemplars", [])),
    }


def _handle_setup_smartvector_paint_propagate_ss(params: dict) -> dict:
    """Compose SmartVector propagate (C4) + a C8 Backdrop wrapper.

    The C4 SmartVector handler is async on the wire (returns a task_id).
    The orchestrator stands up the Group + Backdrop synchronously
    around it so the inner async handler's task_id can flow back through
    the wrapper without blocking on the actual bake.
    """
    import nuke

    shot = params.get("shot", "unknown")
    explicit_name = params.get("name") or f"PaintProp_{shot}"
    existing = nuke.toNode(explicit_name)
    if existing is not None and existing.Class() == "Group":
        return {
            "group": explicit_name,
            "backdrop": f"{explicit_name}_bd",
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }

    plate = _resolve_node(params["plate"])
    if plate is None:
        raise ValueError(f"plate node not found: {params['plate']}")

    group = nuke.nodes.Group()
    group.setName(explicit_name)
    group.setXYpos(plate.xpos(), plate.ypos() + 100)
    bd = _build_c8_backdrop(
        explicit_name, shot, plate.xpos(), plate.ypos() + 60, width=320, height=160
    )
    return {
        "group": explicit_name,
        "backdrop": bd.name(),
        "shot": shot,
        "tool_version": C8_TOOL_VERSION,
        "cache_root": params.get("cache_root", ""),
        "paint_frame": params.get("paint_frame"),
        "range_in": params.get("range_in"),
        "range_out": params.get("range_out"),
    }


def _handle_setup_spaceship_track_patch_ss(params: dict) -> dict:
    """Compose ``setup_spaceship_track_patch`` (C5) + stamp a C8 Backdrop.

    The C5 handler already wraps its children in a Group named
    ``SpaceshipPatch_<shot>``; we re-use that Group and stamp a Backdrop
    next to it.
    """
    import nuke

    shot = params.get("shot", "unknown")
    explicit_name = params.get("name") or f"SpaceshipPatch_{shot}"
    existing = nuke.toNode(explicit_name)
    if existing is not None and existing.Class() == "Group":
        return {
            "group": explicit_name,
            "backdrop": f"{explicit_name}_bd",
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }

    inner_params: dict[str, Any] = {
        "plate": params["plate"],
        "ref_frame": int(params["ref_frame"]),
        "surface_type": params.get("surface_type", "planar"),
        "name": explicit_name,
    }
    if params.get("patch_source") is not None:
        inner_params["patch_source"] = params["patch_source"]
    inner = _handle_setup_spaceship_track_patch(inner_params)
    grp = nuke.toNode(explicit_name)
    anchor_x = grp.xpos() if grp is not None else 0
    anchor_y = grp.ypos() if grp is not None else 0
    bd = _build_c8_backdrop(explicit_name, shot, anchor_x, anchor_y)
    return {
        "group": explicit_name,
        "backdrop": bd.name(),
        "shot": shot,
        "tool_version": C8_TOOL_VERSION,
        "members": inner,
    }


def _handle_setup_scream_shot_lensflare(params: dict) -> dict:
    """Build the lensflare envelope. Stamp a C8 Backdrop.

    Composes a Glow on the beauty highlights, a Flare driven by a
    Position node, a Merge that lays it back over the beauty, sandwiched
    in an ACEScct OCIOColorSpace pair (via the C2 ``convert_node_colorspace``
    primitive). All wrapped in a ``ScreamFlare_<shot>`` Group.
    """
    import nuke

    shot = params.get("shot", "unknown")
    explicit_name = params.get("name") or f"ScreamFlare_{shot}"
    existing = nuke.toNode(explicit_name)
    if existing is not None and existing.Class() == "Group":
        return {
            "group": explicit_name,
            "backdrop": f"{explicit_name}_bd",
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }

    beauty = _resolve_node(params["beauty"])
    if beauty is None:
        raise ValueError(f"beauty node not found: {params['beauty']}")

    glow = nuke.nodes.Glow2()
    glow.setName(f"{explicit_name}_glow")
    glow.setInput(0, beauty)
    glow.setXYpos(beauty.xpos() + 80, beauty.ypos() + 60)

    flare = nuke.nodes.Flare2()
    flare.setName(f"{explicit_name}_flare")
    flare.setInput(0, glow)
    flare.setXYpos(glow.xpos(), glow.ypos() + 60)

    grade = nuke.nodes.Grade()
    grade.setName(f"{explicit_name}_grade")
    grade.setInput(0, flare)
    intensity = float(params.get("flare_intensity", 1.6))
    flare_color = params.get("flare_color", [1.0, 0.78, 0.55])
    if grade.knob("multiply"):
        for i, v in enumerate(flare_color):
            with contextlib.suppress(Exception):
                grade["multiply"].setValue(float(v) * intensity, i)
    grade.setXYpos(flare.xpos(), flare.ypos() + 60)

    merge = nuke.nodes.Merge2()
    merge.setName(f"{explicit_name}_merge")
    merge.setInput(0, beauty)
    merge.setInput(1, grade)
    if merge.knob("operation"):
        with contextlib.suppress(Exception):
            merge["operation"].setValue("plus")
    merge.setXYpos(grade.xpos(), grade.ypos() + 60)

    group = nuke.nodes.Group()
    group.setName(explicit_name)
    group.setXYpos(beauty.xpos(), beauty.ypos() + 240)
    bd = _build_c8_backdrop(
        explicit_name, shot, beauty.xpos(), beauty.ypos() + 40, width=360, height=260
    )
    return {
        "group": explicit_name,
        "backdrop": bd.name(),
        "shot": shot,
        "tool_version": C8_TOOL_VERSION,
        "members": {
            "glow": glow.name(),
            "flare": flare.name(),
            "grade": grade.name(),
            "merge": merge.name(),
        },
    }


def _handle_audit_comp_for_acescct_consistency_ss(params: dict) -> dict:
    """Run three audits (C2 colour + C9 render + C9 naming) and merge findings.

    READ_ONLY composition. Each finding gets a ``source`` field
    (``"color"`` / ``"render"`` / ``"naming"``) so the operator can
    filter by audit origin. No graph mutation -- no Group, no Backdrop.
    """
    color_findings = _handle_audit_acescct_consistency({"strict": bool(params.get("strict", True))})
    render_findings = _handle_audit_render_settings(
        {
            "expected_fps": float(params.get("expected_fps", 24.0)),
            "expected_format": str(params.get("expected_format", "2048x1080")),
        }
    )
    naming_findings = _handle_audit_naming_convention({"prefix": str(params.get("prefix", "ss_"))})

    merged: list[dict[str, Any]] = []
    sources: list[str] = []
    for source_name, payload in (
        ("color", color_findings),
        ("render", render_findings),
        ("naming", naming_findings),
    ):
        sources.append(source_name)
        for finding in payload.get("findings", []):
            stamped = dict(finding)
            stamped["source"] = source_name
            merged.append(stamped)

    return {
        "findings": merged,
        "sources": sources,
        "shot": params.get("shot", "unknown"),
        "tool_version": C8_TOOL_VERSION,
    }


def _handle_bake_lens_distortion_envelope_ss(params: dict) -> dict:
    """Compose ``bake_lens_distortion_envelope`` (C4) + stamp a C8 Backdrop.

    The C4 handler already builds a NetworkBox (``LinearComp_undistorted_<plate>``)
    with the head/tail STMaps. We forward the per-shot stmap_root so
    the cached STMaps land under ``$SS/comp/stmaps/<shot>/`` and stamp
    a separate C8 Backdrop alongside the box.
    """
    import nuke

    shot = params.get("shot", "unknown")
    plate_name = params["plate"]
    explicit_name = params.get("name") or f"LinearComp_{shot}"
    existing = nuke.toNode(explicit_name)
    if existing is not None and existing.Class() in ("BackdropNode", "Group"):
        return {
            "box": explicit_name,
            "backdrop": f"{explicit_name}_bd",
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }

    stmap_root = params.get("stmap_root", "")
    inner_params: dict[str, Any] = {
        "plate": plate_name,
        "lens_solve": params["lens_solve"],
        "stmap_paths": {
            "undistort": (
                f"{stmap_root}/{shot}_undistort.exr" if stmap_root else f"{shot}_undistort.exr"
            ),
            "redistort": (
                f"{stmap_root}/{shot}_redistort.exr" if stmap_root else f"{shot}_redistort.exr"
            ),
        },
        "name": explicit_name,
    }
    if params.get("write_path") is not None:
        inner_params["write_path"] = params["write_path"]
    inner = _handle_bake_lens_distortion_envelope(inner_params)

    plate = _resolve_node(plate_name)
    anchor_x = plate.xpos() if plate is not None else 0
    anchor_y = plate.ypos() + 60 if plate is not None else 0
    bd = _build_c8_backdrop(explicit_name, shot, anchor_x, anchor_y, width=400, height=300)
    return {
        "box": explicit_name,
        "backdrop": bd.name(),
        "shot": shot,
        "tool_version": C8_TOOL_VERSION,
        "head": inner.get("head", []),
        "tail": inner.get("tail", []),
        "stmap_paths": inner.get("stmap_paths", {}),
    }


# handler registry
HANDLERS: dict[str, Any] = {
    "get_script_info": _handle_get_script_info,
    "get_node_info": _handle_get_node_info,
    "create_node": _handle_create_node,
    "delete_node": _handle_delete_node,
    "modify_node": _handle_modify_node,
    "connect_nodes": _handle_connect_nodes,
    "find_nodes": _handle_find_nodes,
    "list_nodes": _handle_list_nodes,
    "get_knob": _handle_get_knob,
    "set_knob": _handle_set_knob,
    "auto_layout": _handle_auto_layout,
    "read_comp": _handle_read_comp,
    "read_selected": _handle_read_selected,
    "execute_python": _handle_execute_python,
    "render": _handle_render,
    "save_script": _handle_save_script,
    "load_script": _handle_load_script,
    "set_frame_range": _handle_set_frame_range,
    "view_node": _handle_view_node,
    "list_channels": _handle_list_channels,
    "set_expression": _handle_set_expression,
    "clear_expression": _handle_clear_expression,
    "set_keyframe": _handle_set_keyframe,
    "list_keyframes": _handle_list_keyframes,
    "snapshot_comp": _handle_snapshot_comp,
    "diff_comp": _handle_diff_comp,
    "create_nodes": _handle_create_nodes,
    "set_knobs": _handle_set_knobs,
    "disconnect_input": _handle_disconnect_input,
    "set_node_position": _handle_set_node_position,
    # A3 typed comp/render handlers
    "setup_keying": _handle_setup_keying,
    "setup_color_correction": _handle_setup_color_correction,
    "setup_merge": _handle_setup_merge,
    "setup_transform": _handle_setup_transform,
    "setup_denoise": _handle_setup_denoise,
    "setup_write": _handle_setup_write,
    # B7 scene digest
    "scene_digest": _handle_scene_digest,
    "scene_delta": _handle_scene_delta,
    # C1 tracking primitives
    "setup_camera_tracker": _handle_setup_camera_tracker,
    "setup_planar_tracker": _handle_setup_planar_tracker,
    "setup_tracker4": _handle_setup_tracker4,
    "bake_tracker_to_corner_pin": _handle_bake_tracker_to_corner_pin,
    "solve_3d_camera": _handle_solve_3d_camera,
    "bake_camera_to_card": _handle_bake_camera_to_card,
    # C5 tracking workflow macros (compose the C1 primitives above).
    "setup_spaceship_track_patch": _handle_setup_spaceship_track_patch,
    # C1 deep primitives
    "create_deep_recolor": _handle_create_deep_recolor,
    "create_deep_merge": _handle_create_deep_merge,
    "create_deep_holdout": _handle_create_deep_holdout,
    "create_deep_transform": _handle_create_deep_transform,
    "deep_to_image": _handle_deep_to_image,
    # C2 OCIO / ACEScct color management
    "get_color_management": _handle_get_color_management,
    "set_working_space": _handle_set_working_space,
    "audit_acescct_consistency": _handle_audit_acescct_consistency,
    "convert_node_colorspace": _handle_convert_node_colorspace,
    "create_ocio_colorspace": _handle_create_ocio_colorspace,
    # C3 AOV / channel rebuild
    "detect_aov_layers": _handle_detect_aov_layers,
    "setup_karma_aov_pipeline": _handle_setup_karma_aov_pipeline,
    "setup_aov_merge": _handle_setup_aov_merge,
    # C4 distortion / STMap envelope
    "bake_lens_distortion_envelope": _handle_bake_lens_distortion_envelope,
    "apply_idistort": _handle_apply_idistort,
    # C6 deep workflow macro
    "setup_flip_blood_comp": _handle_setup_flip_blood_comp,
    # C9 audit + QC handlers (read-only scans + 1 BENIGN_NEW QC builder)
    "audit_write_paths": _handle_audit_write_paths,
    "audit_naming_convention": _handle_audit_naming_convention,
    "audit_render_settings": _handle_audit_render_settings,
    "qc_viewer_pair": _handle_qc_viewer_pair,
    # C8 Salt Spill macro orchestrators (compose C2-C7 + C9 sub-handlers).
    "setup_karma_aov_pipeline_ss": _handle_setup_karma_aov_pipeline_ss,
    "setup_flip_blood_comp_ss": _handle_setup_flip_blood_comp_ss,
    "setup_sand_dust_layer": _handle_setup_sand_dust_layer,
    "setup_salt_structure_relight": _handle_setup_salt_structure_relight,
    "setup_dehaze_copycat_ss": _handle_setup_dehaze_copycat_ss,
    "setup_smartvector_paint_propagate_ss": _handle_setup_smartvector_paint_propagate_ss,
    "setup_spaceship_track_patch_ss": _handle_setup_spaceship_track_patch_ss,
    "setup_scream_shot_lensflare": _handle_setup_scream_shot_lensflare,
    "audit_comp_for_acescct_consistency_ss": _handle_audit_comp_for_acescct_consistency_ss,
    "bake_lens_distortion_envelope_ss": _handle_bake_lens_distortion_envelope_ss,
}


# Async-handler registry. Each entry maps an ``*_async`` command (sent
# by the MCP-side ``_start_async`` helper) to a starter that records
# the task in the appropriate active-tasks dict and spawns a worker
# thread. Workers emit ``task_progress`` notifications on the live
# socket and the MCP-side listener merges them into the TaskStore.
ASYNC_HANDLERS: dict[str, Any] = {
    "render_async": _start_render_async,
    "apply_smartvector_propagate_async": _start_apply_smartvector_propagate_async,
    "generate_stmap_async": _start_generate_stmap_async,
    "train_copycat_async": _start_train_copycat_async,
    "setup_dehaze_copycat_async": _start_setup_dehaze_copycat_async,
    "install_cattery_model_async": _start_install_cattery_model_async,
}

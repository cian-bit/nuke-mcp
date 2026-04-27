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
    import nuke

    cmd = msg.get("type", "")
    params = msg.get("params", {})
    rid = msg.get("_request_id")

    if cmd == "ping":
        resp = {"status": "ok", "result": {"pong": True}}
        if rid is not None:
            resp["_request_id"] = rid
        return resp

    # B2: async render returns immediately after spawning a worker.
    if cmd == "render_async":
        if client is None:
            resp = {
                "status": "error",
                "error": "render_async requires the client socket",
                "error_class": "ValueError",
            }
            if rid is not None:
                resp["_request_id"] = rid
            return resp
        try:
            result = _start_render_async(params, client)
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

    # build the code string to execute on the main thread
    handler = HANDLERS.get(cmd)
    if handler is None:
        resp = {"status": "error", "error": f"unknown command: {cmd}"}
        if rid is not None:
            resp["_request_id"] = rid
        return resp

    _request_local.node_cache = {}
    try:
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
    """Signal the worker for ``task_id`` to stop. Returns True if found."""
    with _active_renders_guard:
        stop = _active_renders.get(task_id)
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
    # C1 deep primitives
    "create_deep_recolor": _handle_create_deep_recolor,
    "create_deep_merge": _handle_create_deep_merge,
    "create_deep_holdout": _handle_create_deep_holdout,
    "create_deep_transform": _handle_create_deep_transform,
    "deep_to_image": _handle_deep_to_image,
}

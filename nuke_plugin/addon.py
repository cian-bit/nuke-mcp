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
import json
import logging
import os
import socket
import threading
import traceback
from typing import Any

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

                resp = _dispatch(msg)
                _send(client, resp)

        except TimeoutError:
            continue
        except OSError:
            break


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


def _dispatch(msg: dict[str, Any]) -> dict[str, Any]:
    """Route a command to the right handler, executed on Nuke's main thread.

    A2: echoes ``_request_id`` from the top-level payload back in the
    response so the MCP-side ``send()`` can assert round-trip identity.
    The id lives at the payload root, not inside ``params``.

    B7: installs a fresh per-request ``node_cache`` on ``_request_local``
    so handlers that touch the same node twice (or that participate in
    batch operations) avoid redundant ``nuke.toNode`` calls.
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


def _send(sock: socket.socket, data: dict) -> None:
    payload = json.dumps(_json_safe(data), separators=(",", ":")).encode("utf-8")
    sock.sendall(payload + b"\n")


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


def _handle_setup_write(params: dict) -> dict:
    """Create a Write node downstream of ``input_node`` with validated path/file_type.

    Path traversal: any ``..`` component in ``path`` is rejected. Absolute
    path policy is left to a future Phase D pass; for now we accept any
    absolute or relative path that doesn't traverse upward.
    """
    import nuke

    input_node = params["input_node"]
    path = params["path"]
    file_type = params.get("file_type", "exr")
    colorspace = params.get("colorspace", "scene_linear")

    if not isinstance(path, str) or ".." in path:
        raise ValueError("invalid path: path traversal not permitted")
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
}

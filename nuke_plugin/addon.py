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


def _server_loop(port: int) -> None:
    global _server_socket
    try:
        _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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


def _dispatch(msg: dict[str, Any]) -> dict[str, Any]:
    """Route a command to the right handler, executed on Nuke's main thread."""
    import nuke

    cmd = msg.get("type", "")
    params = msg.get("params", {})

    if cmd == "ping":
        return {"status": "ok", "result": {"pong": True}}

    # build the code string to execute on the main thread
    handler = HANDLERS.get(cmd)
    if handler is None:
        return {"status": "error", "error": f"unknown command: {cmd}"}

    try:
        result = nuke.executeInMainThreadWithResult(handler, args=(params,))
        return {"status": "ok", "result": result}
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc(),
        }


def _send(sock: socket.socket, data: dict) -> None:
    payload = json.dumps(data, separators=(",", ":"), default=str).encode("utf-8")
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
            with contextlib.suppress(Exception):
                knobs[k] = knob.value()

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

    node_type = params["type"]
    name = params.get("name")
    position = params.get("position")
    connect_to = params.get("connect_to")
    knobs_to_set = params.get("knobs", {})

    node = nuke.createNode(node_type, inpanel=False)
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

    for k, v in knobs.items():
        knob = node.knob(k)
        if knob:
            knob.setValue(v)

    if position:
        node.setXYpos(int(position[0]), int(position[1]))
    if new_name:
        node.setName(new_name)

    return {"name": node.name(), "type": node.Class()}


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

    knob.setValue(params["value"])
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

    if root_name:
        root_node = nuke.toNode(root_name)
        if root_node is None:
            raise ValueError(f"root node not found: {root_name}")
        nodes = root_node.nodes() if hasattr(root_node, "nodes") else [root_node]
    else:
        nodes = nuke.allNodes()

    result = []
    for n in nodes:
        entry = {
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

        changed: dict[str, Any] = {}
        for k in n.knobs():
            if k in _SKIP_KNOBS:
                continue
            knob = n.knob(k)
            if (
                knob.isAnimated()
                or knob.hasExpression()
                or (hasattr(knob, "isDefault") and not knob.isDefault())
            ):
                try:
                    val = knob.value()
                    if isinstance(val, str) and len(val) > 500:
                        changed[k] = f"<{len(val)} chars>"
                    elif isinstance(val, int | float | str | bool | list):
                        changed[k] = val
                except Exception:
                    pass
        if changed:
            entry["knobs"] = changed

        if n.hasError():
            entry["error"] = True

        # expressions
        exprs = {}
        for k in n.knobs():
            knob = n.knob(k)
            if knob.hasExpression():
                exprs[k] = knob.expression()
        if exprs:
            entry["expressions"] = exprs

        # group internals (one level only to save tokens)
        if hasattr(n, "nodes") and depth > 0:
            children = n.nodes()
            if children:
                entry["children"] = [{"name": c.name(), "type": c.Class()} for c in children]

        result.append(entry)

    return {"nodes": result, "count": len(result)}


def _handle_read_selected(params: dict) -> dict:
    import nuke

    nodes = nuke.selectedNodes()
    if not nodes:
        return {"nodes": [], "count": 0}

    result = []
    for n in nodes:
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
            if (
                knob.isAnimated()
                or knob.hasExpression()
                or (hasattr(knob, "isDefault") and not knob.isDefault())
            ):
                try:
                    val = knob.value()
                    if isinstance(val, str) and len(val) > 500:
                        changed[k] = f"<{len(val)} chars>"
                    elif isinstance(val, int | float | str | bool | list):
                        changed[k] = val
                except Exception:
                    pass
        if changed:
            entry["knobs"] = changed

        if n.hasError():
            entry["error"] = True

        result.append(entry)

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
    return exec_globals.get("__result__", {})


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
}

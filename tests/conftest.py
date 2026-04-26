"""Mock Nuke server for testing without a running Nuke instance."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest


class MockNukeServer:
    """Fake Nuke socket server. Maintains a minimal node graph in memory
    so tests can verify the full command/response flow."""

    def __init__(self, port: int = 0):
        self.port = port
        self.nodes: dict[str, dict] = {}
        self.connections: dict[str, list[str | None]] = {}
        self.selected: set[str] = set()
        self.expressions: dict[str, dict[str, str]] = {}
        self.keyframes: dict[str, dict[str, list[dict]]] = {}
        self._snapshots: dict[str, dict] = {}
        self._snap_counter = 0
        self.script_info = {
            "script": "/tmp/test.nk",
            "first_frame": 1001,
            "last_frame": 1100,
            "fps": 24.0,
            "format": "HD 1920x1080",
            "colorspace": "ACES",
            "node_count": 0,
        }
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> int:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(1)
        self._sock.settimeout(1.0)
        self.port = self._sock.getsockname()[1]

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._running = False
        if self._sock:
            self._sock.close()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while self._running:
            try:
                client, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self._handle(client)

    def _handle(self, client: socket.socket) -> None:
        # handshake
        handshake = {"nuke_version": "15.1v3", "variant": "NukeX", "pid": 12345}
        client.sendall(json.dumps(handshake).encode() + b"\n")

        buf = b""
        while self._running:
            try:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    msg = json.loads(line)
                    resp = self._dispatch(msg)
                    client.sendall(json.dumps(resp).encode() + b"\n")
            except (OSError, json.JSONDecodeError):
                break
        client.close()

    def _dispatch(self, msg: dict) -> dict:
        cmd = msg.get("type", "")
        params = msg.get("params", {})

        handler = {
            "ping": self._ping,
            "get_script_info": self._get_script_info,
            "get_node_info": self._get_node_info,
            "create_node": self._create_node,
            "delete_node": self._delete_node,
            "modify_node": self._modify_node,
            "connect_nodes": self._connect_nodes,
            "find_nodes": self._find_nodes,
            "list_nodes": self._list_nodes,
            "get_knob": self._get_knob,
            "set_knob": self._set_knob,
            "auto_layout": self._auto_layout,
            "read_comp": self._read_comp,
            "read_selected": self._read_selected,
            "execute_python": self._execute_python,
            "render": self._render,
            "save_script": self._save_script,
            "load_script": self._load_script,
            "set_frame_range": self._set_frame_range,
            "view_node": self._view_node,
            "list_channels": self._list_channels,
            "set_expression": self._set_expression,
            "clear_expression": self._clear_expression,
            "set_keyframe": self._set_keyframe,
            "list_keyframes": self._list_keyframes,
            "snapshot_comp": self._snapshot_comp,
            "diff_comp": self._diff_comp,
            "create_nodes": self._create_nodes,
            "set_knobs": self._set_knobs,
            "disconnect_input": self._disconnect_input,
            "set_node_position": self._set_node_position,
        }.get(cmd)

        if handler is None:
            return {"status": "error", "error": f"unknown command: {cmd}"}

        try:
            result = handler(params)
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _ping(self, p: dict) -> dict:
        return {"pong": True}

    def _get_script_info(self, p: dict) -> dict:
        self.script_info["node_count"] = len(self.nodes)
        return self.script_info

    def _get_node_info(self, p: dict) -> dict:
        name = p["name"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        node = self.nodes[name]
        return {
            "name": name,
            "type": node["type"],
            "inputs": self.connections.get(name, []),
            "knobs": node.get("knobs", {}),
            "error": False,
            "warning": False,
            "x": node.get("x", 0),
            "y": node.get("y", 0),
        }

    def _create_node(self, p: dict) -> dict:
        node_type = p["type"]
        name = p.get("name") or f"{node_type}1"
        # ensure unique name
        base = name
        i = 1
        while name in self.nodes:
            i += 1
            name = f"{base}{i}"

        self.nodes[name] = {
            "type": node_type,
            "knobs": p.get("knobs", {}),
            "x": p.get("position", [0, 0])[0] if p.get("position") else 0,
            "y": p.get("position", [0, 0])[1] if p.get("position") else 0,
        }
        self.connections[name] = []

        if p.get("connect_to") and p["connect_to"] in self.nodes:
            self.connections[name] = [p["connect_to"]]

        return {"name": name, "type": node_type, "x": 0, "y": 0}

    def _delete_node(self, p: dict) -> dict:
        name = p["name"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        del self.nodes[name]
        self.connections.pop(name, None)
        return {"deleted": name}

    def _modify_node(self, p: dict) -> dict:
        name = p["name"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        node = self.nodes[name]
        if p.get("knobs"):
            node.setdefault("knobs", {}).update(p["knobs"])
        if p.get("new_name"):
            self.nodes[p["new_name"]] = self.nodes.pop(name)
            name = p["new_name"]
        return {"name": name, "type": node["type"]}

    def _connect_nodes(self, p: dict) -> dict:
        src = p["from"]
        dst = p["to"]
        if src not in self.nodes:
            raise ValueError(f"source node not found: {src}")
        if dst not in self.nodes:
            raise ValueError(f"target node not found: {dst}")
        idx = p.get("input", 0)
        conns = self.connections.setdefault(dst, [])
        while len(conns) <= idx:
            conns.append(None)
        conns[idx] = src
        return {"connected": f"{src} -> {dst}[{idx}]"}

    def _find_nodes(self, p: dict) -> dict:
        results = []
        for name, node in self.nodes.items():
            if p.get("type") and node["type"] != p["type"]:
                continue
            if p.get("pattern") and p["pattern"].lower() not in name.lower():
                continue
            results.append({"name": name, "type": node["type"], "error": False})
        return {"nodes": results, "count": len(results)}

    def _list_nodes(self, p: dict) -> dict:
        nodes = [{"name": n, "type": d["type"]} for n, d in self.nodes.items()]
        return {"nodes": nodes, "count": len(nodes)}

    def _get_knob(self, p: dict) -> dict:
        name = p["node"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        knobs = self.nodes[name].get("knobs", {})
        knob_name = p["knob"]
        return {
            "value": knobs.get(knob_name, 0),
            "type": "Double_Knob",
            "animated": False,
            "default": knob_name not in knobs,
        }

    def _set_knob(self, p: dict) -> dict:
        name = p["node"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        self.nodes[name].setdefault("knobs", {})[p["knob"]] = p["value"]
        return {"node": name, "knob": p["knob"], "value": p["value"]}

    def _auto_layout(self, p: dict) -> dict:
        return {"laid_out": len(self.nodes)}

    def _read_comp(self, p: dict) -> dict:
        all_nodes = []
        type_filter = p.get("type")
        summary = p.get("summary", False)
        for name, data in self.nodes.items():
            if type_filter and data["type"] != type_filter:
                continue
            entry: dict[str, Any] = {"name": name, "type": data["type"]}
            conns = self.connections.get(name, [])
            if any(conns):
                entry["inputs"] = conns
            if not summary:
                knobs = data.get("knobs", {})
                if knobs:
                    entry["knobs"] = knobs
            all_nodes.append(entry)
        total = len(all_nodes)
        offset = p.get("offset", 0)
        limit = p.get("limit", 0)
        if offset:
            all_nodes = all_nodes[offset:]
        if limit:
            all_nodes = all_nodes[:limit]
        return {"nodes": all_nodes, "count": len(all_nodes), "total": total}

    def _read_selected(self, p: dict) -> dict:
        if not self.selected:
            return {"nodes": [], "count": 0}
        nodes = []
        for name in self.selected:
            if name not in self.nodes:
                continue
            data = self.nodes[name]
            entry: dict[str, Any] = {"name": name, "type": data["type"]}
            conns = self.connections.get(name, [])
            if any(conns):
                entry["inputs"] = conns
            knobs = data.get("knobs", {})
            if knobs:
                entry["knobs"] = knobs
            nodes.append(entry)
        return {"nodes": nodes, "count": len(nodes)}

    def _execute_python(self, p: dict) -> dict:
        # in tests, just return empty result
        return {}

    def _render(self, p: dict) -> dict:
        return {"rendered": "Write1", "frames": [1001, 1100]}

    def _save_script(self, p: dict) -> dict:
        return {"saved": self.script_info["script"]}

    def _load_script(self, p: dict) -> dict:
        return {"loaded": p["path"]}

    def _set_frame_range(self, p: dict) -> dict:
        if "first" in p:
            self.script_info["first_frame"] = p["first"]
        if "last" in p:
            self.script_info["last_frame"] = p["last"]
        return {
            "first": self.script_info["first_frame"],
            "last": self.script_info["last_frame"],
        }

    def _view_node(self, p: dict) -> dict:
        name = p["node"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        return {"viewing": name}

    def _set_expression(self, p: dict) -> dict:
        node, knob, expr = p["node"], p["knob"], p["expression"]
        if node not in self.nodes:
            raise ValueError(f"node not found: {node}")
        self.expressions.setdefault(node, {})[knob] = expr
        return {"node": node, "knob": knob, "expression": expr}

    def _clear_expression(self, p: dict) -> dict:
        node, knob = p["node"], p["knob"]
        if node not in self.nodes:
            raise ValueError(f"node not found: {node}")
        self.expressions.get(node, {}).pop(knob, None)
        return {"node": node, "cleared": knob}

    def _set_keyframe(self, p: dict) -> dict:
        node, knob = p["node"], p["knob"]
        frame, value = p["frame"], p["value"]
        if node not in self.nodes:
            raise ValueError(f"node not found: {node}")
        kfs = self.keyframes.setdefault(node, {}).setdefault(knob, [])
        kfs = [k for k in kfs if k["frame"] != frame]
        kfs.append({"frame": frame, "value": value})
        kfs.sort(key=lambda k: k["frame"])
        self.keyframes[node][knob] = kfs
        return {"node": node, "knob": knob, "frame": frame, "value": value}

    def _list_keyframes(self, p: dict) -> dict:
        node, knob = p["node"], p["knob"]
        if node not in self.nodes:
            raise ValueError(f"node not found: {node}")
        kfs = self.keyframes.get(node, {}).get(knob, [])
        return {"node": node, "knob": knob, "keyframes": kfs}

    def _snapshot_comp(self, p: dict) -> dict:
        self._snap_counter += 1
        snap_id = str(self._snap_counter)
        self._snapshots[snap_id] = self._read_comp({})
        if len(self._snapshots) > 5:
            oldest = min(self._snapshots.keys(), key=int)
            del self._snapshots[oldest]
        return {"snapshot_id": snap_id, "node_count": self._snapshots[snap_id]["count"]}

    def _diff_comp(self, p: dict) -> dict:
        snap_id = p.get("snapshot_id")
        if not snap_id or snap_id not in self._snapshots:
            raise ValueError(f"snapshot not found: {snap_id}")
        before = self._snapshots[snap_id]
        current = self._read_comp({})
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
        return {"added": added, "removed": removed, "changed": []}

    def _list_channels(self, p: dict) -> dict:
        name = p["node"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        return {"layers": {"rgba": ["red", "green", "blue", "alpha"]}}

    def _create_nodes(self, p: dict) -> dict:
        results = []
        for spec in p["nodes"]:
            results.append(self._create_node(spec))
        return {"nodes": results, "count": len(results)}

    def _set_knobs(self, p: dict) -> dict:
        results = []
        for op in p["operations"]:
            results.append(self._set_knob(op))
        return {"results": results, "count": len(results)}

    def _disconnect_input(self, p: dict) -> dict:
        node = p["node"]
        idx = p["input"]
        if node not in self.nodes:
            raise ValueError(f"node not found: {node}")
        conns = self.connections.get(node, [])
        if idx < len(conns):
            conns[idx] = None
        return {"node": node, "input": idx, "disconnected": True}

    def _set_node_position(self, p: dict) -> dict:
        positions = p.get("positions", [])
        results = []
        for pos in positions:
            name = pos["node"]
            if name not in self.nodes:
                results.append({"node": name, "error": "not found"})
                continue
            x, y = int(pos["x"]), int(pos["y"])
            self.nodes[name]["x"] = x
            self.nodes[name]["y"] = y
            results.append({"node": name, "x": x, "y": y})
        return {"results": results, "count": len(results)}


@pytest.fixture
def mock_server():
    server = MockNukeServer()
    port = server.start()
    time.sleep(0.1)  # let server bind
    yield server, port
    server.stop()


@pytest.fixture
def connected(mock_server):
    """Connect to mock server, disconnect after test."""
    from nuke_mcp import connection

    server, port = mock_server
    connection.connect("localhost", port)
    yield server
    connection.disconnect()

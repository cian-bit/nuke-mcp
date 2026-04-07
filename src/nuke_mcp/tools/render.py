"""Render and precomp tools."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool()
    @nuke_command("setup_write")
    def setup_write(
        input_node: str,
        path: str,
        file_type: str = "exr",
        colorspace: str = "scene_linear",
    ) -> dict:
        """Create a Write node connected to input_node with production defaults.

        Args:
            input_node: node to connect as input.
            path: output file path (use #### for frame padding).
            file_type: exr, png, jpg, dpx, etc.
            colorspace: output colorspace.
        """
        code = f"""
import nuke
src = nuke.toNode({input_node!r})
if not src:
    raise ValueError("node not found: {input_node}")
w = nuke.createNode("Write", inpanel=False)
w.setInput(0, src)
w["file"].setValue({path!r})
w["file_type"].setValue({file_type!r})
if w.knob("colorspace"):
    w["colorspace"].setValue({colorspace!r})
__result__ = {{"name": w.name(), "path": {path!r}}}
"""
        return connection.send("execute_python", code=code)

    @ctx.mcp.tool(
        annotations={"destructiveHint": True},
    )
    @nuke_command("render_frames")
    def render_frames(
        write_node: str | None = None,
        first_frame: int | None = None,
        last_frame: int | None = None,
        confirm: bool = False,
    ) -> dict:
        """Render frames through a Write node.

        Args:
            write_node: name of Write node. uses first Write in script if omitted.
            first_frame: start frame. uses script range if omitted.
            last_frame: end frame. uses script range if omitted.
            confirm: must be True to render. call with False to preview.
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
        return connection.send_raw("render", timeout=300.0, **params)

    @ctx.mcp.tool()
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
import nuke, os

src = nuke.toNode({source_node!r})
if not src:
    raise ValueError("node not found: {source_node}")

# auto-generate path from script location
script_path = nuke.root().name()
script_dir = os.path.dirname(script_path) if script_path else "/tmp"
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

w = nuke.createNode("Write", inpanel=False)
w.setName("{precomp_name}_write")
w.setInput(0, src)
w["file"].setValue(out_path)
w["file_type"].setValue("exr")
w["first"].setValue(first)
w["last"].setValue(last)
w.setXYpos(src.xpos(), src.ypos() + 80)

# create Read
r = nuke.createNode("Read", inpanel=False)
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

    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
    )
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

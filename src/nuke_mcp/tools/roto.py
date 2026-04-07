"""Roto and rotopaint node tools."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool()
    @nuke_command("create_roto")
    def create_roto(input_node: str, roto_type: str = "Roto") -> dict:
        """Create a Roto or RotoPaint node connected to the input.

        Args:
            input_node: node to roto on top of.
            roto_type: Roto or RotoPaint.
        """
        code = f"""
import nuke
src = nuke.toNode({input_node!r})
if not src:
    raise ValueError("node not found: {input_node}")
r = nuke.createNode({roto_type!r}, inpanel=False)
r.setInput(0, src)
r.setXYpos(src.xpos(), src.ypos() + 60)
__result__ = {{"name": r.name(), "type": r.Class()}}
"""
        return connection.send("execute_python", code=code)

    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
    )
    @nuke_command("list_roto_shapes")
    def list_roto_shapes(node: str) -> dict:
        """List all shapes and strokes in a Roto or RotoPaint node.

        Args:
            node: name of a Roto or RotoPaint node.
        """
        code = f"""
import nuke
n = nuke.toNode({node!r})
if not n:
    raise ValueError("node not found: {node}")
curve_knob = n.knob("curves")
if not curve_knob:
    raise ValueError("not a roto node: {node}")
root_layer = curve_knob.rootLayer
shapes = []
def walk(layer, prefix=""):
    for i in range(layer.getNumItems()):
        item = layer.getItem(i)
        name = item.name if hasattr(item, "name") else str(i)
        item_type = type(item).__name__
        shapes.append({{"name": prefix + name, "type": item_type}})
        if hasattr(item, "getNumItems"):
            walk(item, prefix + name + "/")
walk(root_layer)
__result__ = {{"node": {node!r}, "shapes": shapes, "count": len(shapes)}}
"""
        return connection.send("execute_python", code=code)

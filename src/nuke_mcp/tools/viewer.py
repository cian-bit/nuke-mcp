"""Viewer control tools."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.annotations import IDEMPOTENT
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    # ``view_node`` toggles which node the viewer displays. Treated as benign
    # state change -- does not lose work, but is not idempotent across calls
    # (it overwrites whichever input the viewer was on).
    @ctx.mcp.tool(annotations={"destructiveHint": False}, output_schema=None)
    @nuke_command("view_node")
    def view_node(node: str) -> dict:
        """Set the viewer to display a specific node's output.

        Args:
            node: name of the node to view.
        """
        return connection.send("view_node", node=node)

    @ctx.mcp.tool(annotations=IDEMPOTENT, output_schema=None)
    @nuke_command("set_viewer_lut")
    def set_viewer_lut(lut: str) -> dict:
        """Switch the viewer's display LUT/colorspace.

        Args:
            lut: LUT or colorspace name (e.g. 'sRGB', 'Cineon', 'None').
        """
        code = f"""
import nuke
viewer = nuke.activeViewer()
if not viewer:
    raise ValueError("no active viewer")
vnode = viewer.node()
if vnode.knob("viewerProcess"):
    vnode["viewerProcess"].setValue({lut!r})
__result__ = {{"lut": {lut!r}}}
"""
        return connection.send("execute_python", code=code)

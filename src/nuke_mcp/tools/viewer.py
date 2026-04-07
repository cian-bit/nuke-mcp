"""Viewer control tools."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool()
    @nuke_command("view_node")
    def view_node(node: str) -> dict:
        """Set the viewer to display a specific node's output.

        Args:
            node: name of the node to view.
        """
        return connection.send("view_node", node=node)

    @ctx.mcp.tool()
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

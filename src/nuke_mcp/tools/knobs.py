"""Knob get/set tools."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
    )
    @nuke_command("get_knob")
    def get_knob(node: str, knob: str) -> dict:
        """Read a knob value from a node.

        Args:
            node: node name.
            knob: knob name (e.g. 'size', 'mix', 'file', 'channels').
        """
        return connection.send("get_knob", node=node, knob=knob)

    @ctx.mcp.tool()
    @nuke_command("set_knob")
    def set_knob(node: str, knob: str, value: str | int | float | bool) -> dict:
        """Set a knob value on a node.

        Args:
            node: node name.
            knob: knob name.
            value: value to set. type depends on the knob.
        """
        return connection.send("set_knob", node=node, knob=knob, value=value)

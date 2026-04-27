"""Knob get/set tools."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.annotations import IDEMPOTENT, READ_ONLY
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(
        annotations=READ_ONLY,
        output_schema=None,
    )
    @nuke_command("get_knob")
    def get_knob(node: str, knob: str) -> dict:
        """Read a knob value from a node.

        Args:
            node: node name.
            knob: knob name (e.g. 'size', 'mix', 'file', 'channels').
        """
        return connection.send("get_knob", node=node, knob=knob)

    @ctx.mcp.tool(annotations=IDEMPOTENT, output_schema=None)
    @nuke_command("set_knob")
    def set_knob(node: str, knob: str, value: str | int | float | bool) -> dict:
        """Set a knob value on a node.

        Args:
            node: node name.
            knob: knob name.
            value: value to set. type depends on the knob.
        """
        return connection.send("set_knob", node=node, knob=knob, value=value)

    @ctx.mcp.tool(annotations=IDEMPOTENT, output_schema=None)
    @nuke_command("set_knobs")
    def set_knobs(operations: str) -> dict:
        """Set multiple knobs across multiple nodes in one call. Saves round-trips.

        Args:
            operations: JSON array of {node, knob, value} objects.
                        example: '[{"node":"Grade1","knob":"mix","value":0.5},{"node":"Blur1","knob":"size","value":10}]'
        """
        import json as _json

        parsed = _json.loads(operations)
        return connection.send("set_knobs", operations=parsed)

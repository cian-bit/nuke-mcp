"""Expression and keyframe tools for Nuke knob animation."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(output_schema=None)
    @nuke_command("set_expression")
    def set_expression(node: str, knob: str, expression: str) -> dict:
        """Set a TCL expression on a knob. The expression is evaluated per-frame.

        Common expressions: frame, frame/24.0, sin(frame*0.1), [value other_node.knob]

        Args:
            node: node name.
            knob: knob name to set expression on.
            expression: Nuke TCL expression string.
        """
        return connection.send("set_expression", node=node, knob=knob, expression=expression)

    @ctx.mcp.tool(output_schema=None)
    @nuke_command("clear_expression")
    def clear_expression(node: str, knob: str) -> dict:
        """Remove an expression or animation from a knob, leaving it at its current value.

        Args:
            node: node name.
            knob: knob name to clear.
        """
        return connection.send("clear_expression", node=node, knob=knob)

    @ctx.mcp.tool(output_schema=None)
    @nuke_command("set_keyframe")
    def set_keyframe(node: str, knob: str, frame: int, value: float) -> dict:
        """Set a keyframe on a knob at a specific frame. Creates animation if
        the knob is not already animated.

        Args:
            node: node name.
            knob: knob name.
            frame: frame number.
            value: value at that frame.
        """
        return connection.send("set_keyframe", node=node, knob=knob, frame=frame, value=value)

    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
        output_schema=None,
    )
    @nuke_command("list_keyframes")
    def list_keyframes(node: str, knob: str) -> dict:
        """List all keyframes on a knob. Returns frame/value pairs.

        Args:
            node: node name.
            knob: knob name to inspect.
        """
        return connection.send("list_keyframes", node=node, knob=knob)

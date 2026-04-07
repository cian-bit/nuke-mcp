"""Tools for reading and understanding Nuke scripts.

This is the core differentiator: Claude can natively read a comp
without screenshots. read_comp serializes the node graph into
structured data with only non-default knob values to save tokens.
"""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
    )
    @nuke_command("read_comp")
    def read_comp(root: str | None = None, depth: int = 999) -> dict:
        """Read the full node graph (or a subtree). Returns every node with its
        type, connections, non-default knob values, and error state.

        Use this to understand a script before making changes. Only knobs that
        differ from their defaults are included to keep the output compact.

        Args:
            root: read children of this node only (e.g. a Group name). omit for entire script.
            depth: how many levels deep to recurse into groups. default 999.
        """
        params = {"depth": depth}
        if root:
            params["root"] = root
        return connection.send("read_comp", **params)

    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
    )
    @nuke_command("read_node_detail")
    def read_node_detail(name: str) -> dict:
        """Deep inspection of a single node. Returns all non-default knobs,
        input/output connections, expressions, animation state, and error info.
        For Groups/Gizmos, also shows internal node structure.

        Args:
            name: node name to inspect.
        """
        return connection.send("get_node_info", name=name)

    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
    )
    @nuke_command("read_selected")
    def read_selected() -> dict:
        """Read only the currently selected nodes and their connections.
        Use when the user says 'look at this' or 'what do you think of this section'.
        """
        return connection.send("read_selected")

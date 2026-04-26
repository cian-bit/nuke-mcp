"""Tools for reading and understanding Nuke scripts.

This is the core differentiator: Claude can natively read a comp
without screenshots. read_comp serializes the node graph into
structured data with only non-default knob values to save tokens.
"""

from __future__ import annotations

from typing import Any

from nuke_mcp import connection
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
        output_schema=None,
    )
    @nuke_command("read_comp")
    def read_comp(
        root: str | None = None,
        depth: int = 999,
        summary: bool = False,
        type: str | None = None,
        offset: int = 0,
        limit: int = 0,
    ) -> dict:
        """Read the node graph (or a subtree). Returns nodes with type,
        connections, non-default knob values, and error state.

        For large comps (200+ nodes), use summary=True for a compact overview
        (names and types only, no knobs), or use offset/limit to paginate.

        Args:
            root: read children of this node only (e.g. a Group name). omit for entire script.
            depth: how many levels deep to recurse into groups. default 999.
            summary: if True, return only name/type/connections per node, skip knobs. faster for large comps.
            type: filter to only this node class (e.g. 'Grade', 'Read').
            offset: skip this many nodes (for pagination). default 0.
            limit: max nodes to return. 0 means all. use with offset to page through large comps.
        """
        params: dict[str, Any] = {"depth": depth}
        if root:
            params["root"] = root
        if summary:
            params["summary"] = True
        if type:
            params["type"] = type
        if offset:
            params["offset"] = offset
        if limit:
            params["limit"] = limit
        return connection.send("read_comp", **params)

    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
        output_schema=None,
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
        output_schema=None,
    )
    @nuke_command("read_selected")
    def read_selected() -> dict:
        """Read only the currently selected nodes and their connections.
        Use when the user says 'look at this' or 'what do you think of this section'.
        """
        return connection.send("read_selected")

    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
        output_schema=None,
    )
    @nuke_command("snapshot_comp")
    def snapshot_comp() -> dict:
        """Take a snapshot of the current comp state. Returns a snapshot_id
        you can pass to diff_comp later to see what changed.

        Snapshots are stored server-side (max 5). Use before making changes.
        """
        return connection.send("snapshot_comp")

    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
        output_schema=None,
    )
    @nuke_command("diff_comp")
    def diff_comp(snapshot_id: str) -> dict:
        """Compare the current comp to a previous snapshot. Shows nodes
        added, removed, and knobs changed. Call snapshot_comp first.

        Args:
            snapshot_id: ID from a previous snapshot_comp call.
        """
        return connection.send("diff_comp", snapshot_id=snapshot_id)

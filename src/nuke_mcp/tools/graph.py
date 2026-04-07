"""Node graph manipulation tools."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(output_schema=None)
    @nuke_command("create_node")
    def create_node(
        type: str,
        name: str | None = None,
        connect_to: str | None = None,
    ) -> dict:
        """Create a node and optionally wire it to an existing node.

        Args:
            type: Nuke node class (Grade, Blur, Merge2, Read, Write, etc.)
            name: optional name. Nuke auto-names if omitted.
            connect_to: name of a node to connect as input 0. for Merge nodes, connects to B pipe.
        """
        params: dict = {"type": type}
        if name:
            params["name"] = name
        if connect_to:
            params["connect_to"] = connect_to
        return connection.send("create_node", **params)

    @ctx.mcp.tool(
        annotations={"destructiveHint": True},
        output_schema=None,
    )
    @nuke_command("delete_node")
    def delete_node(name: str, confirm: bool = False) -> dict:
        """Delete a node from the script.

        Args:
            name: node to delete.
            confirm: must be True to actually delete. call with False first to preview.
        """
        if not confirm:
            return {
                "preview": f"will delete node '{name}'. call again with confirm=True to proceed."
            }
        return connection.send("delete_node", name=name)

    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
        output_schema=None,
    )
    @nuke_command("find_nodes")
    def find_nodes(
        type: str | None = None,
        pattern: str | None = None,
        errors_only: bool = False,
    ) -> dict:
        """Search for nodes by type, name pattern, or error state.

        Args:
            type: filter by node class (e.g. 'Grade', 'Read').
            pattern: filter by name substring (case-insensitive).
            errors_only: if True, only return nodes with errors.
        """
        params: dict = {}
        if type:
            params["type"] = type
        if pattern:
            params["pattern"] = pattern
        if errors_only:
            params["errors_only"] = True
        return connection.send("find_nodes", **params)

    @ctx.mcp.tool(
        annotations={"readOnlyHint": True},
        output_schema=None,
    )
    @nuke_command("list_nodes")
    def list_nodes(root: str | None = None) -> dict:
        """List all nodes in the script, or children of a Group node.

        Args:
            root: optional Group node name to list children of.
        """
        params: dict = {}
        if root:
            params["root"] = root
        return connection.send("list_nodes", **params)

    @ctx.mcp.tool(output_schema=None)
    @nuke_command("connect_nodes")
    def connect_nodes(
        from_node: str,
        to_node: str,
        input_index: int | None = None,
    ) -> dict:
        """Connect two nodes. For Merge nodes, defaults to B pipe (input 1).

        Args:
            from_node: source node (output).
            to_node: target node (input).
            input_index: which input on the target. defaults to 0, or 1 for Merge nodes.
        """
        params: dict = {"from": from_node, "to": to_node}
        if input_index is not None:
            params["input"] = input_index
        return connection.send("connect_nodes", **params)

    @ctx.mcp.tool(output_schema=None)
    @nuke_command("auto_layout")
    def auto_layout(selected_only: bool = False) -> dict:
        """Auto-arrange the node graph layout.

        Args:
            selected_only: if True, only arrange selected nodes.
        """
        return connection.send("auto_layout", selected_only=selected_only)

    @ctx.mcp.tool(output_schema=None)
    @nuke_command("modify_node")
    def modify_node(
        name: str,
        new_name: str | None = None,
        update_expressions: bool = True,
    ) -> dict:
        """Rename a node. Checks for expressions that reference this node
        and optionally updates them to use the new name.

        Args:
            name: current node name.
            new_name: new name to set.
            update_expressions: auto-fix expressions referencing the old name. default True.
        """
        params: dict = {"name": name}
        if new_name:
            params["new_name"] = new_name
            params["update_expressions"] = update_expressions
        return connection.send("modify_node", **params)

    @ctx.mcp.tool(output_schema=None)
    @nuke_command("create_nodes")
    def create_nodes(nodes: str) -> dict:
        """Create multiple nodes in one call. Much faster than individual create_node calls.

        Args:
            nodes: JSON array of node specs. each spec has: type (required), name (optional),
                   connect_to (optional). example: '[{"type":"Grade"},{"type":"Blur","connect_to":"Grade1"}]'
        """
        import json as _json

        parsed = _json.loads(nodes)
        return connection.send("create_nodes", nodes=parsed)

    @ctx.mcp.tool(output_schema=None)
    @nuke_command("disconnect_input")
    def disconnect_node_input(node: str, input_index: int) -> dict:
        """Disconnect a specific input on a node.

        Args:
            node: node name.
            input_index: which input to disconnect (0-based).
        """
        return connection.send("disconnect_input", node=node, input=input_index)

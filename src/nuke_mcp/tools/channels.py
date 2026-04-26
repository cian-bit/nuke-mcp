"""Channel and layer management tools."""

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
    @nuke_command("list_channels")
    def list_channels(node: str) -> dict:
        """List all channels/layers available at a node's output, grouped by layer.

        Args:
            node: node name to inspect channels of.
        """
        return connection.send("list_channels", node=node)

    # ``shuffle_channels`` creates a fresh Shuffle2 node -- not idempotent.
    @ctx.mcp.tool(annotations={"destructiveHint": False}, output_schema=None)
    @nuke_command("shuffle_channels")
    def shuffle_channels(
        input_node: str,
        from_layer: str,
        to_layer: str = "rgba",
    ) -> dict:
        """Create a Shuffle node to move channels between layers.

        Args:
            input_node: source node.
            from_layer: source layer (e.g. 'diffuse', 'specular', 'depth').
            to_layer: target layer. defaults to 'rgba'.
        """
        code = f"""
import nuke
src = nuke.toNode({input_node!r})
if not src:
    raise ValueError("node not found: {input_node}")
s = nuke.nodes.Shuffle2()
s.setInput(0, src)
s["in1"].setValue({from_layer!r})
s["out1"].setValue({to_layer!r})
__result__ = {{"name": s.name(), "from": {from_layer!r}, "to": {to_layer!r}}}
"""
        return connection.send("execute_python", code=code)

    @ctx.mcp.tool(annotations=IDEMPOTENT, output_schema=None)
    @nuke_command("setup_aov_merge")
    def setup_aov_merge(read_nodes: str) -> dict:
        """Merge multiple AOV Read nodes together (additive). Common EXR workflow.

        Args:
            read_nodes: comma-separated list of Read node names to merge.
        """
        names = [n.strip() for n in read_nodes.split(",")]
        names_repr = repr(names)
        code = f"""
import nuke
names = {names_repr}
nodes = []
for n in names:
    node = nuke.toNode(n)
    if not node:
        raise ValueError(f"node not found: {{n}}")
    nodes.append(node)

if len(nodes) < 2:
    raise ValueError("need at least 2 nodes to merge")

prev = nodes[0]
merges = []
for i in range(1, len(nodes)):
    m = nuke.nodes.Merge2()
    m["operation"].setValue("plus")
    m.setInput(1, prev)
    m.setInput(0, nodes[i])
    prev = m
    merges.append(m.name())

__result__ = {{"merges": merges, "final": merges[-1], "inputs": names}}
"""
        return connection.send("execute_python", code=code)

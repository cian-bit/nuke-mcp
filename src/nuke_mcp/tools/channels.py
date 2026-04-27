"""Channel and layer management tools.

``setup_aov_merge`` lived here historically; Phase C3 migrated it to
``tools/aov.py`` along with the new ``detect_aov_layers`` and
``setup_karma_aov_pipeline`` workflow tools. The legacy comma-separated
signature is preserved at the new home.
"""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.annotations import BENIGN_NEW, READ_ONLY
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="core", annotations=READ_ONLY)
    @nuke_command("list_channels")
    def list_channels(node: str) -> dict:
        """List all channels/layers available at a node's output, grouped by layer.

        Args:
            node: node name to inspect channels of.
        """
        return connection.send("list_channels", node=node)

    # ``shuffle_channels`` creates a fresh Shuffle2 node -- not idempotent.
    @nuke_tool(ctx, profile="core", annotations=BENIGN_NEW)
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

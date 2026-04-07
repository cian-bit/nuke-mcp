"""Compositing workflow tools. These create multi-node setups for
common comp operations, not just individual nodes."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool()
    @nuke_command("setup_keying")
    def setup_keying(input_node: str, keyer_type: str = "Keylight") -> dict:
        """Set up a keying pipeline: keyer, erode, edge blur, premult.

        Args:
            input_node: node to key (usually a Read with greenscreen footage).
            keyer_type: Keylight, Primatte, IBKGizmo, or Cryptomatte.
        """
        code = f"""
import nuke

src = nuke.toNode({input_node!r})
if not src:
    raise ValueError("node not found: {input_node}")

keyer_type = {keyer_type!r}
x, y = src.xpos(), src.ypos()

keyer = nuke.createNode(keyer_type, inpanel=False)
keyer.setInput(0, src)
keyer.setXYpos(x, y + 60)

erode = nuke.createNode("FilterErode", inpanel=False)
erode.setInput(0, keyer)
erode["channels"].setValue("alpha")
erode["size"].setValue(-0.5)
erode.setXYpos(x, y + 120)

edge = nuke.createNode("EdgeBlur", inpanel=False)
edge.setInput(0, erode)
edge["size"].setValue(3)
edge.setXYpos(x, y + 180)

premult = nuke.createNode("Premult", inpanel=False)
premult.setInput(0, edge)
premult.setXYpos(x, y + 240)

__result__ = {{
    "keyer": keyer.name(),
    "erode": erode.name(),
    "edge_blur": edge.name(),
    "premult": premult.name(),
    "tip": "adjust the keyer node settings and erode size to refine the matte",
}}
"""
        return connection.send("execute_python", code=code)

    @ctx.mcp.tool()
    @nuke_command("setup_color_correction")
    def setup_color_correction(input_node: str, operation: str = "Grade") -> dict:
        """Create a color correction node connected to the input.

        Args:
            input_node: node to colour correct.
            operation: Grade, ColorCorrect, or HueCorrect.
        """
        code = f"""
import nuke

src = nuke.toNode({input_node!r})
if not src:
    raise ValueError("node not found: {input_node}")

cc = nuke.createNode({operation!r}, inpanel=False)
cc.setInput(0, src)
cc.setXYpos(src.xpos(), src.ypos() + 60)

__result__ = {{"name": cc.name(), "type": cc.Class()}}
"""
        return connection.send("execute_python", code=code)

    @ctx.mcp.tool()
    @nuke_command("setup_merge")
    def setup_merge(
        fg: str,
        bg: str,
        operation: str = "over",
    ) -> dict:
        """Merge foreground over background. Auto-connects fg to B pipe.

        Args:
            fg: foreground node name.
            bg: background node name.
            operation: merge operation (over, plus, multiply, screen, etc.)
        """
        code = f"""
import nuke

fg_node = nuke.toNode({fg!r})
bg_node = nuke.toNode({bg!r})
if not fg_node:
    raise ValueError("fg node not found: {fg}")
if not bg_node:
    raise ValueError("bg node not found: {bg}")

merge = nuke.createNode("Merge2", inpanel=False)
merge["operation"].setValue({operation!r})
merge.setInput(0, bg_node)  # A pipe = bg
merge.setInput(1, fg_node)  # B pipe = fg
merge.setXYpos(
    (fg_node.xpos() + bg_node.xpos()) // 2,
    max(fg_node.ypos(), bg_node.ypos()) + 80,
)

__result__ = {{"name": merge.name(), "operation": {operation!r}}}
"""
        return connection.send("execute_python", code=code)

    @ctx.mcp.tool()
    @nuke_command("setup_transform")
    def setup_transform(input_node: str, operation: str = "Transform") -> dict:
        """Create a transform node.

        Args:
            input_node: node to transform.
            operation: Transform, CornerPin2D, or Reformat.
        """
        code = f"""
import nuke

src = nuke.toNode({input_node!r})
if not src:
    raise ValueError("node not found: {input_node}")

t = nuke.createNode({operation!r}, inpanel=False)
t.setInput(0, src)
t.setXYpos(src.xpos(), src.ypos() + 60)

__result__ = {{"name": t.name(), "type": t.Class()}}
"""
        return connection.send("execute_python", code=code)

    @ctx.mcp.tool()
    @nuke_command("setup_denoise")
    def setup_denoise(input_node: str) -> dict:
        """Create a Denoise node with production defaults.

        Args:
            input_node: node to denoise.
        """
        code = f"""
import nuke

src = nuke.toNode({input_node!r})
if not src:
    raise ValueError("node not found: {input_node}")

dn = nuke.createNode("Denoise2", inpanel=False)
dn.setInput(0, src)
dn.setXYpos(src.xpos(), src.ypos() + 60)

__result__ = {{"name": dn.name()}}
"""
        return connection.send("execute_python", code=code)

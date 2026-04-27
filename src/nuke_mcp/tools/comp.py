"""Compositing workflow tools. These create multi-node setups for
common comp operations, not just individual nodes.

A3: every tool here is now a thin dispatch to a typed addon handler via
``run_on_main``. The previous f-string ``execute_python`` blobs have been
moved into ``nuke_plugin/addon.py`` (``_handle_setup_keying`` et al) so
the wire payload is a small ``params`` dict and the addon-side does
operation/file_type allowlist checks.
"""

from __future__ import annotations

from nuke_mcp.annotations import BENIGN_NEW
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("setup_keying")
    def setup_keying(input_node: str, keyer_type: str = "Keylight") -> dict:
        """Set up a keying pipeline: keyer, erode, edge blur, premult.

        Args:
            input_node: node to key (usually a Read with greenscreen footage).
            keyer_type: Keylight, Primatte, IBKGizmo, or Cryptomatte.
        """
        return run_on_main(
            "setup_keying",
            {"input_node": input_node, "keyer_type": keyer_type},
            "mutate",
        )

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("setup_color_correction")
    def setup_color_correction(input_node: str, operation: str = "Grade") -> dict:
        """Create a color correction node connected to the input.

        Args:
            input_node: node to colour correct.
            operation: Grade, ColorCorrect, HueCorrect, or OCIOColorSpace.
        """
        return run_on_main(
            "setup_color_correction",
            {"input_node": input_node, "operation": operation},
            "mutate",
        )

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
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
        return run_on_main(
            "setup_merge",
            {"fg": fg, "bg": bg, "operation": operation},
            "mutate",
        )

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("setup_transform")
    def setup_transform(input_node: str, operation: str = "Transform") -> dict:
        """Create a transform node.

        Args:
            input_node: node to transform.
            operation: Transform, CornerPin2D, Reformat, or Tracker4.
        """
        return run_on_main(
            "setup_transform",
            {"input_node": input_node, "operation": operation},
            "mutate",
        )

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("setup_denoise")
    def setup_denoise(input_node: str) -> dict:
        """Create a Denoise node with production defaults.

        Args:
            input_node: node to denoise.
        """
        return run_on_main(
            "setup_denoise",
            {"input_node": input_node},
            "mutate",
        )

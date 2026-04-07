"""Nuke menu integration. Add this to your menu.py or source it from there."""

import nuke
from nuke_mcp_addon import is_running, start, stop


def _toggle():
    if is_running():
        stop()
        nuke.message("nuke-mcp stopped")
    else:
        start()
        nuke.message("nuke-mcp started on port 9876")


toolbar = nuke.menu("Nodes")
mcp_menu = toolbar.addMenu("MCP")
mcp_menu.addCommand("Toggle Server", _toggle)

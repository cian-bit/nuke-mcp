"""Script-level tools: info, save, load, frame range."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.annotations import DESTRUCTIVE_OPEN, IDEMPOTENT, OPEN_WORLD, READ_ONLY
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="core", annotations=READ_ONLY)
    @nuke_command("get_script_info")
    def get_script_info() -> dict:
        """Get current script metadata: path, frame range, fps, format, colorspace, node count."""
        return connection.send("get_script_info")

    @nuke_tool(ctx, profile="core", annotations=OPEN_WORLD)
    @nuke_command("save_script")
    def save_script(path: str | None = None) -> dict:
        """Save the script. If path is given, saves as a new file.

        Args:
            path: optional file path for save-as. omit to save in place.
        """
        params: dict = {}
        if path:
            params["path"] = path
        return connection.send("save_script", **params)

    @nuke_tool(ctx, profile="core", annotations=DESTRUCTIVE_OPEN)
    @nuke_command("load_script")
    def load_script(path: str, confirm: bool = False) -> dict:
        """Open a Nuke script. Replaces current script.

        Args:
            path: .nk file to open.
            confirm: must be True to proceed. call with False first to preview.
        """
        if not confirm:
            return {
                "preview": f"will open '{path}', replacing current script. call with confirm=True."
            }
        return connection.send("load_script", path=path)

    @nuke_tool(ctx, profile="core", annotations=IDEMPOTENT)
    @nuke_command("set_frame_range")
    def set_frame_range(
        first: int | None = None,
        last: int | None = None,
        current: int | None = None,
    ) -> dict:
        """Set the timeline frame range and/or current frame.

        Args:
            first: first frame number.
            last: last frame number.
            current: jump to this frame.
        """
        params: dict = {}
        if first is not None:
            params["first"] = first
        if last is not None:
            params["last"] = last
        if current is not None:
            params["current"] = current
        return connection.send("set_frame_range", **params)

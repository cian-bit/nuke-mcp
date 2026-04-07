"""Execute arbitrary Python code in Nuke."""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(
        annotations={"destructiveHint": True},
    )
    @nuke_command("execute_python")
    def execute_python(code: str, confirm: bool = False) -> dict:
        """Run Python code inside Nuke's interpreter. Set __result__ to return data.

        Some operations are blocked: os.remove, subprocess, sys.exit, nuke.scriptClose.

        Args:
            code: Python code to execute. assign to __result__ to return data.
            confirm: must be True to run. call with False to preview the code.
        """
        if not confirm:
            return {"preview": f"will execute:\n{code}\ncall with confirm=True to run."}
        return connection.send("execute_python", code=code)

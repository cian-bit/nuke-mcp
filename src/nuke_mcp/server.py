"""FastMCP server for Nuke. Registers tools and handles transport."""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP

from nuke_mcp import connection
from nuke_mcp.tools import channels, code, comp, graph, knobs, read, render, script, viewer

log = logging.getLogger(__name__)

NUKE_HOST = os.environ.get("NUKE_HOST", "localhost")
NUKE_PORT = int(os.environ.get("NUKE_PORT", "9876"))


def build_server(mock: bool = False) -> FastMCP:
    mcp = FastMCP(
        "nuke-mcp",
        instructions=(
            "MCP server for Foundry Nuke compositing. "
            "Use read_comp to understand the current script before making changes. "
            "Destructive tools (delete, execute, render, load) require confirm=True. "
            "Call them first without confirm to preview what will happen."
        ),
    )

    version = connection.connect(NUKE_HOST, NUKE_PORT) if not mock else None

    # register tool modules
    ctx = ServerContext(mcp=mcp, version=version, mock=mock)

    read.register(ctx)
    graph.register(ctx)
    knobs.register(ctx)
    script.register(ctx)
    render.register(ctx)
    channels.register(ctx)
    viewer.register(ctx)
    code.register(ctx)
    comp.register(ctx)

    return mcp


class ServerContext:
    """Passed to tool modules during registration."""

    def __init__(
        self,
        mcp: FastMCP,
        version: connection.NukeVersion | None,
        mock: bool = False,
    ):
        self.mcp = mcp
        self.version = version
        self.mock = mock

    @property
    def is_nukex(self) -> bool:
        return self.version is not None and self.version.is_nukex

    def at_least(self, major: int, minor: int = 0) -> bool:
        return self.version is not None and self.version.at_least(major, minor)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()],
    )
    mcp = build_server()
    mcp.run(transport="stdio")

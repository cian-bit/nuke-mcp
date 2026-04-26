"""FastMCP server for Nuke. Registers tools and handles transport."""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP

from nuke_mcp import connection
from nuke_mcp.tools import (
    channels,
    code,
    comp,
    digest,
    expressions,
    graph,
    knobs,
    read,
    render,
    roto,
    script,
    viewer,
)

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

    # B7 warm connect: try to attach to Nuke up front so the first tool
    # call doesn't pay the connect+handshake cost. If Nuke isn't running,
    # the lazy reconnect path in ``connection.send`` still kicks in once
    # it does come up. Always seed ``_last_host`` / ``_last_port`` so the
    # reconnect has a target.
    version: connection.NukeVersion | None = None
    if not mock:
        connection._last_host = NUKE_HOST
        connection._last_port = NUKE_PORT
        try:
            version = connection.connect(NUKE_HOST, NUKE_PORT)
        except (connection.ConnectionError, OSError) as exc:
            log.warning(
                "nuke not yet running on %s:%d (%s); will retry on first tool call",
                NUKE_HOST,
                NUKE_PORT,
                exc,
            )

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
    expressions.register(ctx)
    roto.register(ctx)
    digest.register(ctx)

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

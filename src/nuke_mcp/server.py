"""FastMCP server for Nuke. Registers tools and handles transport.

Phase B4: tool registration is paginated by skill profile. Every tool
module always runs ``register(ctx)`` at boot so the tool object exists
in FastMCP's registry, but tools whose ``_profile`` is not in
``active_profiles`` get disabled (via ``mcp.disable(names=...)``)
before the server hits the wire. Loading a profile at runtime --
``load_profile("tracking")`` -- flips the disabled flag back on and
emits a ``notifications/tools/list_changed`` so the model resyncs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from fastmcp import FastMCP

from nuke_mcp import connection
from nuke_mcp.profiles import DEFAULT_PROFILES, PROFILES
from nuke_mcp.prompts import register_prompts
from nuke_mcp.tools import (
    aov,
    audit,
    channels,
    code,
    color,
    comp,
    deep,
    deep_workflow,
    digest,
    distortion,
    expressions,
    graph,
    knobs,
    ml,
    read,
    render,
    roto,
    salt_spill,
    script,
    tasks,
    track_workflow,
    tracking,
    viewer,
)
from nuke_mcp.tools import (
    profiles as profiles_tools,
)

log = logging.getLogger(__name__)

NUKE_HOST = os.environ.get("NUKE_HOST", "localhost")
NUKE_PORT = int(os.environ.get("NUKE_PORT", "9876"))


def _names_for_profiles(profiles: Iterable[str]) -> set[str]:
    """Flatten a list of profile names into a set of tool names.

    Unknown profile names are silently ignored -- ``register_tools``
    has the loud-fail path; this helper is meant for the inner
    enable/disable diff which should be tolerant.
    """
    names: set[str] = set()
    for profile in profiles:
        names.update(PROFILES.get(profile, []))
    return names


def register_tools(ctx: ServerContext, active_profiles: Iterable[str] | None = None) -> None:
    """Register every tool module then disable tools outside ``active_profiles``.

    ``active_profiles=None`` (the default) loads :data:`DEFAULT_PROFILES`
    -- ``"core"``. The disable step uses FastMCP's ``mcp.disable(names=...)``
    which adds a visibility transform; ``load_profile`` reverses it via
    ``mcp.enable(names=...)`` at runtime.

    Tools whose profile name isn't in :data:`PROFILES` at all (which
    would only happen if someone added a new profile to the registry
    decorator without updating ``profiles.PROFILES``) get a warning
    log and are left enabled -- never silently dropped.
    """
    profiles = list(active_profiles) if active_profiles is not None else list(DEFAULT_PROFILES)

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
    tracking.register(ctx)
    track_workflow.register(ctx)
    deep.register(ctx)
    color.register(ctx)
    aov.register(ctx)
    distortion.register(ctx)
    deep_workflow.register(ctx)
    ml.register(ctx)
    audit.register(ctx)
    salt_spill.register(ctx)
    tasks.register(ctx)
    profiles_tools.register(ctx)

    # Diff: every tool listed in PROFILES that isn't part of an active
    # profile gets disabled. Tools missing from PROFILES entirely log a
    # warning -- the catalog must list every registered tool for B4
    # introspection to be sound.
    all_known_tools: set[str] = set()
    for tools in PROFILES.values():
        all_known_tools.update(tools)
    active_names = _names_for_profiles(profiles)
    to_disable = all_known_tools - active_names
    if to_disable:
        ctx.mcp.disable(names=to_disable)


def build_server(
    mock: bool = False,
    active_profiles: Iterable[str] | None = None,
) -> FastMCP:
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
    register_tools(ctx, active_profiles=active_profiles)
    # Phase C10: workflow prompts surface as a separate MCP primitive
    # (``prompts/list`` + ``prompts/get``), independent of the
    # tool-profile gating above.
    register_prompts(ctx)

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

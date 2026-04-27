"""Runtime profile-loading tools (Phase B4 commit 2).

Three tools surface to MCP clients:

* ``load_profile(name)`` -- enable every tool in the named profile
  via ``mcp.enable(names=...)`` and ping the client with
  ``ToolListChangedNotification`` so its tool surface refreshes.
* ``unload_profile(name)`` -- the inverse. ``"core"`` is rejected
  (always-on) so the model can't lock itself out of read paths.
* ``list_profiles()`` -- catalog with descriptions, tool counts, and
  current-loaded state. The model picks ``load_profile`` arguments
  from this without trial and error.

These three live in the always-loaded set so they remain reachable
even when no other profiles are loaded. The catalog is sourced from
:mod:`nuke_mcp.profiles`; the registry decorator's ``_profile`` stamp
is the source of truth for which tool belongs where.
"""

from __future__ import annotations

import logging
from typing import Any

import mcp.types as mt
from fastmcp import Context

from nuke_mcp import profiles as profiles_module
from nuke_mcp.annotations import READ_ONLY
from nuke_mcp.registry import nuke_tool

if False:
    from nuke_mcp.server import ServerContext

log = logging.getLogger(__name__)


async def _loaded_profile_names(mcp: Any) -> set[str]:
    """Return the set of profile names whose tools are currently visible.

    A profile counts as "loaded" when *all* of its tools surface in
    ``list_tools()`` -- a partial overlap returns False so flips that
    only reach half a profile (rare, but possible if a tool was
    renamed) don't lie about state.

    Async because FastMCP's ``list_tools`` is an async coroutine; the
    callers here are all async-by-Context too.
    """
    visible = {t.name for t in await mcp.list_tools()}
    loaded: set[str] = set()
    for profile_name, tool_names in profiles_module.PROFILES.items():
        if all(name in visible for name in tool_names):
            loaded.add(profile_name)
    return loaded


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="core", annotations=READ_ONLY)
    async def list_profiles() -> dict:
        """List every skill profile with its description, tool count,
        and current-loaded state.

        Pass the returned ``name`` to ``load_profile`` to surface the
        profile's tools at runtime. Profiles already in ``loaded=True``
        are visible right now.
        """
        loaded = await _loaded_profile_names(ctx.mcp)
        return {
            "profiles": {
                name: {
                    "description": profiles_module.PROFILE_DESCRIPTIONS.get(name, ""),
                    "tool_count": len(profiles_module.PROFILES[name]),
                    "loaded": name in loaded,
                }
                for name in profiles_module.all_profile_names()
            },
            "default": list(profiles_module.DEFAULT_PROFILES),
        }

    @nuke_tool(ctx, profile="core", annotations={"destructiveHint": False})
    async def load_profile(name: str, mcp_ctx: Context) -> dict:
        """Surface every tool in profile ``name``. Idempotent --
        loading an already-loaded profile is a no-op.

        Emits ``notifications/tools/list_changed`` after the flip so
        the client refreshes its tool surface.

        Args:
            name: profile name (call ``list_profiles`` for the catalog).
        """
        if name not in profiles_module.PROFILES:
            return {
                "status": "error",
                "error": f"unknown profile {name!r}",
                "known_profiles": profiles_module.all_profile_names(),
            }

        loaded = await _loaded_profile_names(ctx.mcp)
        tool_names = set(profiles_module.PROFILES[name])
        if name in loaded:
            return {
                "status": "ok",
                "profile": name,
                "already_loaded": True,
                "tool_count": len(tool_names),
            }

        ctx.mcp.enable(names=tool_names)

        # Notify the client that the tool list changed. Best-effort:
        # a stand-alone test invocation has no session, in which case
        # ``send_notification`` will raise -- swallow so the tool
        # still reports the local state change.
        try:
            await mcp_ctx.send_notification(
                mt.ToolListChangedNotification(method="notifications/tools/list_changed")
            )
        except Exception as exc:
            log.debug("tools/list_changed notification skipped: %s", exc)

        return {
            "status": "ok",
            "profile": name,
            "loaded": True,
            "tool_count": len(tool_names),
            "tools": sorted(tool_names),
        }

    @nuke_tool(ctx, profile="core", annotations={"destructiveHint": False})
    async def unload_profile(name: str, mcp_ctx: Context) -> dict:
        """Disable every tool in profile ``name``. ``core`` is locked
        on -- unloading it would strip the read paths and the
        ``load_profile`` tool itself.

        Args:
            name: profile name to disable.
        """
        if name == "core":
            return {
                "status": "error",
                "error": "core profile cannot be unloaded",
            }
        if name not in profiles_module.PROFILES:
            return {
                "status": "error",
                "error": f"unknown profile {name!r}",
                "known_profiles": profiles_module.all_profile_names(),
            }

        loaded = await _loaded_profile_names(ctx.mcp)
        if name not in loaded:
            return {
                "status": "ok",
                "profile": name,
                "already_unloaded": True,
            }

        tool_names = set(profiles_module.PROFILES[name])
        ctx.mcp.disable(names=tool_names)

        try:
            await mcp_ctx.send_notification(
                mt.ToolListChangedNotification(method="notifications/tools/list_changed")
            )
        except Exception as exc:
            log.debug("tools/list_changed notification skipped: %s", exc)

        return {
            "status": "ok",
            "profile": name,
            "unloaded": True,
            "tool_count": len(tool_names),
        }

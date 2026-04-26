"""Compact scene-digest tools for token-efficient state tracking.

Two read-only tools, ported from
``houdini-mcp-beta/houdini_mcp/tools/digest.py``:

- ``scene_digest`` -- one-shot fingerprint of the entire script: counts
  by class, errors, warnings, selection, viewer, plus an md5 hex[:8]
  hash. Cheap to call -- no per-node round-trips.
- ``scene_delta`` -- compare against a previous hash. Returns
  ``{"changed": False, "hash": prev_hash}`` when nothing has changed,
  otherwise the full digest with ``changed=True``.

The hash and the per-call body are both built addon-side
(``_handle_scene_digest`` / ``_handle_scene_delta``). The MCP-side
tools are thin dispatchers.
"""

from __future__ import annotations

from nuke_mcp import connection
from nuke_mcp.annotations import READ_ONLY
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(annotations=READ_ONLY, output_schema=None)
    @nuke_command("scene_digest")
    def scene_digest() -> dict:
        """Compact fingerprint of the script: counts by class, errors,
        warnings, selection, active viewer, display node, plus an md5
        hex[:8] hash for delta comparison.

        Stable across no-op turns -- pair with ``scene_delta`` to skip
        re-rendering large response payloads when nothing has changed.
        """
        return connection.send("scene_digest")

    @ctx.mcp.tool(annotations=READ_ONLY, output_schema=None)
    @nuke_command("scene_delta")
    def scene_delta(prev_hash: str) -> dict:
        """Compare current scene state against a previous hash.

        If unchanged, returns ``{"changed": False, "hash": prev_hash}``
        with no graph enumeration leaking into the response. If changed,
        returns the full digest body with ``changed=True``.

        Args:
            prev_hash: the ``hash`` from a previous ``scene_digest`` (or
                ``scene_delta``) call.
        """
        return connection.send("scene_delta", prev_hash=prev_hash)

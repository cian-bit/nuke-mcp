"""Tools for reading and understanding Nuke scripts.

This is the core differentiator: Claude can natively read a comp
without screenshots. read_comp serializes the node graph into
structured data with only non-default knob values to save tokens.

B5 plumbs the addon dict through ``NodeInfo`` / ``DiffResult`` at the
tool boundary: the model parses the wire payload, then we re-emit
via ``model_dump(by_alias=True, exclude_none=True, exclude_unset=True)``.
``extra="allow"`` + ``exclude_unset`` together mean any field the
addon didn't send doesn't materialise in the response, so the wire
shape stays byte-stable for existing test fixtures while typed
attribute access becomes available downstream.
"""

from __future__ import annotations

import logging
from typing import Any

from nuke_mcp import connection
from nuke_mcp.annotations import READ_ONLY
from nuke_mcp.models import DiffResult, NodeInfo
from nuke_mcp.models._warnings import warn_once
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext

log = logging.getLogger(__name__)


def _model_dump(model: Any) -> dict[str, Any]:
    """``model_dump`` with the canonical B5 flag set.

    ``by_alias=True`` preserves wire keys; ``exclude_none`` drops null
    placeholders; ``exclude_unset`` keeps the dump byte-stable when
    the addon omitted a field.
    """
    return model.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="core", annotations=READ_ONLY)
    @nuke_command("read_comp")
    def read_comp(
        root: str | None = None,
        depth: int = 999,
        summary: bool = False,
        type: str | None = None,
        offset: int = 0,
        limit: int = 0,
    ) -> dict:
        """Read the node graph (or a subtree). Returns nodes with type,
        connections, non-default knob values, and error state.

        For large comps (200+ nodes), use summary=True for a compact overview
        (names and types only, no knobs), or use offset/limit to paginate.

        Args:
            root: read children of this node only (e.g. a Group name). omit for entire script.
            depth: how many levels deep to recurse into groups. default 999.
            summary: if True, return only name/type/connections per node, skip knobs. faster for large comps.
            type: filter to only this node class (e.g. 'Grade', 'Read').
            offset: skip this many nodes (for pagination). default 0.
            limit: max nodes to return. 0 means all. use with offset to page through large comps.
        """
        params: dict[str, Any] = {"depth": depth}
        if root:
            params["root"] = root
        if summary:
            params["summary"] = True
        if type:
            params["type"] = type
        if offset:
            params["offset"] = offset
        if limit:
            params["limit"] = limit
        result = connection.send("read_comp", **params)
        # Per-node validation through NodeInfo. We mutate ``result`` in
        # place so the envelope (count / total / offset / limit / extras)
        # passes through untouched -- only the ``nodes`` list goes through
        # the model.
        if isinstance(result, dict) and isinstance(result.get("nodes"), list):
            typed_nodes: list[dict[str, Any]] = []
            for entry in result["nodes"]:
                if isinstance(entry, dict):
                    try:
                        typed_nodes.append(_model_dump(NodeInfo.model_validate(entry)))
                    except Exception as exc:
                        # Defensive: never fail closed on a model error -- a
                        # malformed node entry shouldn't sink the whole call.
                        warn_once(
                            log,
                            "read_comp.nodes",
                            "read_comp: NodeInfo validation failed; returning raw node entry: %s",
                            exc,
                        )
                        typed_nodes.append(entry)
                else:
                    typed_nodes.append(entry)
            result["nodes"] = typed_nodes
        return result

    @nuke_tool(ctx, profile="core", annotations=READ_ONLY, output_model=NodeInfo)
    @nuke_command("read_node_detail")
    def read_node_detail(name: str) -> dict:
        """Deep inspection of a single node. Returns all non-default knobs,
        input/output connections, expressions, animation state, and error info.
        For Groups/Gizmos, also shows internal node structure.

        Args:
            name: node name to inspect.
        """
        result = connection.send("get_node_info", name=name)
        if isinstance(result, dict):
            try:
                return _model_dump(NodeInfo.model_validate(result))
            except Exception as exc:
                warn_once(
                    log,
                    "read_node_detail",
                    "read_node_detail: NodeInfo validation failed; returning raw payload: %s",
                    exc,
                )
                return result
        return result

    @nuke_tool(ctx, profile="core", annotations=READ_ONLY)
    @nuke_command("read_selected")
    def read_selected() -> dict:
        """Read only the currently selected nodes and their connections.
        Use when the user says 'look at this' or 'what do you think of this section'.
        """
        return connection.send("read_selected")

    @nuke_tool(ctx, profile="core", annotations=READ_ONLY)
    @nuke_command("snapshot_comp")
    def snapshot_comp() -> dict:
        """Take a snapshot of the current comp state. Returns a snapshot_id
        you can pass to diff_comp later to see what changed.

        Snapshots are stored server-side (max 5). Use before making changes.
        """
        return connection.send("snapshot_comp")

    @nuke_tool(ctx, profile="core", annotations=READ_ONLY, output_model=DiffResult)
    @nuke_command("diff_comp")
    def diff_comp(snapshot_id: str) -> dict:
        """Compare the current comp to a previous snapshot. Shows nodes
        added, removed, and knobs changed. Call snapshot_comp first.

        Args:
            snapshot_id: ID from a previous snapshot_comp call.
        """
        result = connection.send("diff_comp", snapshot_id=snapshot_id)
        if isinstance(result, dict) and "added" in result and "removed" in result:
            try:
                return _model_dump(DiffResult.model_validate(result))
            except Exception as exc:
                warn_once(
                    log,
                    "diff_comp",
                    "diff_comp: DiffResult validation failed; returning raw payload: %s",
                    exc,
                )
                return result
        return result

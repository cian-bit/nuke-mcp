"""Pydantic v2 structured-output models for the nuke-mcp tool boundary.

Phase B5 introduces a thin Pydantic layer at the boundary of five
high-leverage tools. Models are intentionally permissive
(``extra="allow"``) so the existing wire format passes through
untouched -- a follow-up phase will tighten them once the schema is
stable across the addon and tools.

Each model parses the addon return dict, then re-emits via
``model_dump(by_alias=True, exclude_none=True)`` so wire keys are
preserved (alias > Python field name) and ``None`` defaults don't
leak into the response envelope.
"""

from __future__ import annotations

from nuke_mcp.models.comp import DiffResult, ScriptInfo
from nuke_mcp.models.node import KnobValue, NodeInfo, NodeSummary
from nuke_mcp.models.render import RenderResult

__all__ = [
    "DiffResult",
    "KnobValue",
    "NodeInfo",
    "NodeSummary",
    "RenderResult",
    "ScriptInfo",
]

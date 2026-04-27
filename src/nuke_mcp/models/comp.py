"""Comp-level models: ScriptInfo and DiffResult.

``ScriptInfo`` mirrors ``_handle_get_script_info`` (addon.py L304) --
the wire payload is ``{script, first_frame, last_frame, fps, format,
colorspace, node_count}``. We surface it as the more idiomatic
``path`` / ``frame_range`` while keeping ``populate_by_name=True`` so
either spelling works on construction. ``extra="allow"`` lets
non-canonical fields like ``colorspace`` round-trip untouched.

``DiffResult`` matches the addon's ``_handle_diff_comp`` shape
exactly: three lists -- ``added`` and ``removed`` of ``{name, type}``
dicts, ``changed`` of free-form per-node diff dicts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScriptInfo(BaseModel):
    """Top-level script metadata (path, frame range, fps, format).

    Construct via wire keys ``script`` / ``first_frame`` / ``last_frame``
    or via Python aliases ``path`` / ``frame_range``. The validator
    folds ``first_frame`` / ``last_frame`` into a 2-tuple ``frame_range``
    when only the wire keys are present.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    path: str | None = Field(default=None, alias="script")
    node_count: int = 0
    frame_range: tuple[int, int] = (0, 0)
    fps: float = 0.0
    format: str = ""

    @model_validator(mode="before")
    @classmethod
    def _fold_frame_range(cls, data: Any) -> Any:
        """Build ``frame_range`` from wire ``first_frame`` / ``last_frame``.

        The addon emits the bounds as separate keys; the Python model
        prefers a tuple. Leave both spellings in the dict so the round
        trip back to wire still produces ``first_frame`` / ``last_frame``
        via ``extra="allow"``.
        """
        if not isinstance(data, dict):
            return data
        if "frame_range" not in data and "first_frame" in data and "last_frame" in data:
            data = {**data, "frame_range": (int(data["first_frame"]), int(data["last_frame"]))}
        return data


class DiffResult(BaseModel):
    """Output of ``diff_comp``: lists of added / removed / changed nodes.

    Each ``added`` / ``removed`` entry is ``{name, type}``; ``changed``
    entries are free-form diff dicts (per-knob before/after, input
    rewires, etc.).
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    added: list[dict[str, Any]] = Field(default_factory=list)
    removed: list[dict[str, Any]] = Field(default_factory=list)
    changed: list[dict[str, Any]] = Field(default_factory=list)

"""Render result model.

The addon's ``_handle_render`` emits ``{rendered, frames}`` where
``rendered`` is the Write-node name and ``frames`` is the
``[first, last]`` pair that was executed. The Python-side model
expands that into the more useful ``frames_written`` /
``output_path`` / timing fields; ``model_validator`` folds the wire
shape on the way in. Unknown fields pass through via
``extra="allow"`` so future addon-side enrichment doesn't require a
model bump.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RenderResult(BaseModel):
    """Result of a render through a Write node.

    Wire shape ``{rendered, frames: [first, last]}`` is folded into:
      * ``frames_written``: explicit ``[first, ..., last]`` list when
        the addon hands us a 2-element ``frames`` array.
      * ``output_path``: alias of ``rendered`` -- the Write-node name
        the render went through.

    Timing / error fields are placeholders -- the addon doesn't fill
    them yet. They round-trip out via ``exclude_none=True`` so they
    don't pollute the wire when zero/empty.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    frames_written: list[int] = Field(default_factory=list)
    duration_seconds: float = 0.0
    average_fps: float = 0.0
    output_path: str = Field(default="", alias="rendered")
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _expand_frame_pair(cls, data: Any) -> Any:
        """Turn wire ``frames=[first, last]`` into a full inclusive list.

        Only fires when ``frames_written`` is absent. Caps expansion at
        ten thousand frames so a malformed payload can't blow memory --
        the addon never renders that wide in one call anyway.
        """
        if not isinstance(data, dict):
            return data
        if "frames_written" in data:
            return data
        frames = data.get("frames")
        if isinstance(frames, list) and len(frames) == 2:
            try:
                first, last = int(frames[0]), int(frames[1])
            except (TypeError, ValueError):
                return data
            if 0 <= last - first < 10_000:
                data = {**data, "frames_written": list(range(first, last + 1))}
        return data

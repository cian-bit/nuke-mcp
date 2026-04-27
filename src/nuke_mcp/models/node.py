"""Node-shaped models: NodeSummary / NodeInfo / KnobValue.

The wire format coming back from the addon's ``get_node_info`` /
``read_comp`` paths uses ``type`` (Nuke's class name) and ``x`` / ``y``
(coordinates) -- so this module aliases the Python-friendly
``class_`` / ``xpos`` / ``ypos`` field names onto the actual wire keys
via Pydantic ``Field(alias=...)``. ``populate_by_name=True`` means
constructors accept either spelling.

``KnobValue`` is the union of every primitive type a Nuke knob value
can serialize to over the wire: int / float / str / bool / list /
None. Pydantic v2's ``Annotated[Union[...], Field(...)]`` is the
preferred shape for a non-discriminated union; we don't need a tag
because the addon already validates types upstream.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

# A knob's serialized value. The addon's ``read_comp`` / ``get_knob``
# paths cast through ``isinstance(val, int | float | str | bool | list |
# tuple)`` before emitting, so this union covers every shape that can
# show up. ``list[Any]`` rather than ``list[float]`` because color and
# enum knobs return mixed-element lists.
KnobValue = Annotated[
    int | float | str | bool | list[Any] | None,
    Field(description="Serialized Nuke knob value."),
]


class NodeSummary(BaseModel):
    """Minimal node identity: name, class, position.

    Wire keys ``type`` / ``x`` / ``y`` map to ``class_`` / ``xpos`` /
    ``ypos`` here; ``populate_by_name=True`` lets callers construct
    with either spelling. ``extra="allow"`` so any wire-only field
    (``error``, ``warning``, ``children`` etc.) survives the
    round-trip.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str
    class_: str = Field(alias="type")
    xpos: int = Field(default=0, alias="x")
    ypos: int = Field(default=0, alias="y")


class NodeInfo(NodeSummary):
    """Detailed node payload used by ``read_node_detail``.

    Adds inputs, knob dict, metadata bag, and an optional error
    message. ``inputs`` may contain ``None`` entries (unconnected
    slots); ``knobs`` carries only non-default values.
    """

    inputs: list[str | None] = Field(default_factory=list)
    knobs: dict[str, KnobValue] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | bool | None = None

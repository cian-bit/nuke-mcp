"""Knob get/set tools.

B5 puts the ``value`` field through the ``KnobValue`` type adapter so
malformed payloads (a tuple where Pydantic expects a list, an opaque
object Nuke decided to hand back) get coerced into the canonical
serialised shape before they hit the wire. Validation is best-effort:
on failure we keep the original payload so a quirky knob type can't
sink the call.
"""

from __future__ import annotations

import contextlib
from typing import Any

from pydantic import TypeAdapter, ValidationError

from nuke_mcp import connection
from nuke_mcp.annotations import IDEMPOTENT, READ_ONLY
from nuke_mcp.models import KnobValue
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


# TypeAdapter is the v2 way to validate a non-BaseModel union. Built
# once at import; a per-call instance would burn ~50us extra.
_KNOB_VALUE_ADAPTER: TypeAdapter[Any] = TypeAdapter(KnobValue)


def _coerce_knob_value(payload: dict[str, Any]) -> dict[str, Any]:
    """In-place coerce ``payload['value']`` through the KnobValue union.

    Tuples become lists (the most common Nuke quirk -- ``XY_Knob``
    values come back as ``(x, y)`` tuples); other types pass through
    untouched. Validation failures are swallowed so a knob whose
    ``value()`` returns an exotic object still flows.
    """
    if "value" not in payload:
        return payload
    raw = payload["value"]
    if isinstance(raw, tuple):
        raw = list(raw)
    # Defensive: keep the original value rather than fail closed if a
    # quirky knob type slips past the union.
    with contextlib.suppress(ValidationError):
        payload["value"] = _KNOB_VALUE_ADAPTER.validate_python(raw)
    return payload


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(
        annotations=READ_ONLY,
        output_schema=None,
    )
    @nuke_command("get_knob")
    def get_knob(node: str, knob: str) -> dict:
        """Read a knob value from a node.

        Args:
            node: node name.
            knob: knob name (e.g. 'size', 'mix', 'file', 'channels').
        """
        result = connection.send("get_knob", node=node, knob=knob)
        if isinstance(result, dict):
            _coerce_knob_value(result)
        return result

    @ctx.mcp.tool(annotations=IDEMPOTENT, output_schema=None)
    @nuke_command("set_knob")
    def set_knob(node: str, knob: str, value: str | int | float | bool) -> dict:
        """Set a knob value on a node.

        Args:
            node: node name.
            knob: knob name.
            value: value to set. type depends on the knob.
        """
        result = connection.send("set_knob", node=node, knob=knob, value=value)
        if isinstance(result, dict):
            _coerce_knob_value(result)
        return result

    @ctx.mcp.tool(annotations=IDEMPOTENT, output_schema=None)
    @nuke_command("set_knobs")
    def set_knobs(operations: str) -> dict:
        """Set multiple knobs across multiple nodes in one call. Saves round-trips.

        Args:
            operations: JSON array of {node, knob, value} objects.
                        example: '[{"node":"Grade1","knob":"mix","value":0.5},{"node":"Blur1","knob":"size","value":10}]'
        """
        import json as _json

        parsed = _json.loads(operations)
        return connection.send("set_knobs", operations=parsed)

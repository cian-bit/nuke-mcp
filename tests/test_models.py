"""Tests for the B5 Pydantic models.

Each model gets:
  * a round-trip check (dict in -> validate -> dump -> dict out)
  * a check that ``extra="allow"`` preserves wire-only fields
  * a check that the model_dump kwargs the tool boundary uses
    (``by_alias``, ``exclude_none``, ``exclude_unset``) produce the
    expected wire shape.

The integration test for ``read_node_detail`` exercises the tool
boundary end-to-end through the mock server fixture so a regression
in either the model or the plumbing surfaces here.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from nuke_mcp import connection
from nuke_mcp.models import (
    DiffResult,
    KnobValue,
    NodeInfo,
    NodeSummary,
    RenderResult,
    ScriptInfo,
)
from nuke_mcp.models._warnings import reset_for_tests

# ---------------------------------------------------------------------------
# NodeInfo
# ---------------------------------------------------------------------------


def test_node_info_round_trip_preserves_extras() -> None:
    """Wire payload with ``warning`` / ``children`` survives the round trip."""
    wire: dict[str, Any] = {
        "name": "Grade1",
        "type": "Grade",
        "x": 100,
        "y": 200,
        "inputs": ["Plate", None],
        "knobs": {"mix": 0.5, "white": [1.0, 1.0, 1.0, 1.0]},
        "warning": False,
        "children": [],
    }
    n = NodeInfo.model_validate(wire)
    out = n.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)
    # Every wire key shows up on the way out.
    for k in wire:
        assert k in out, f"missing wire key {k!r} in dump: {out}"
    # ``type`` is the alias, not the Python field name.
    assert out["type"] == "Grade"
    assert "class_" not in out


def test_node_info_typed_attribute_access() -> None:
    """Python field names work even though we constructed by alias."""
    n = NodeInfo.model_validate({"name": "g", "type": "Grade", "x": 0, "y": 0})
    assert n.class_ == "Grade"
    assert n.xpos == 0


def test_node_summary_round_trip_aliases() -> None:
    """NodeSummary handles the ``class`` alias both directions."""
    n = NodeSummary.model_validate({"name": "g", "type": "Grade", "x": 50, "y": 60})
    out = n.model_dump(by_alias=True, exclude_unset=True)
    assert out == {"name": "g", "type": "Grade", "x": 50, "y": 60}


# ---------------------------------------------------------------------------
# ScriptInfo (alias: script <-> path; folds first/last_frame -> frame_range)
# ---------------------------------------------------------------------------


def test_script_info_alias_round_trip() -> None:
    """Wire ``script`` field round-trips alongside the Python ``path`` alias."""
    wire = {
        "script": "/tmp/test.nk",
        "first_frame": 1001,
        "last_frame": 1100,
        "fps": 24.0,
        "format": "HD 1920x1080",
        "colorspace": "ACES",
        "node_count": 5,
    }
    s = ScriptInfo.model_validate(wire)
    # Python attribute access via alias.
    assert s.path == "/tmp/test.nk"
    assert s.frame_range == (1001, 1100)
    out = s.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)
    # Wire key preserved (NOT swapped to ``path``).
    assert out["script"] == "/tmp/test.nk"
    # ``colorspace`` is unknown to the model but rides through extras.
    assert out["colorspace"] == "ACES"


# ---------------------------------------------------------------------------
# RenderResult (model_dump kwargs in the boundary helper)
# ---------------------------------------------------------------------------


def test_render_result_dump_kwargs() -> None:
    """``by_alias=True, exclude_none=True`` is the contract -- prove it."""
    r = RenderResult.model_validate({"rendered": "Write1", "frames": [1001, 1003]})
    out = r.model_dump(by_alias=True, exclude_none=True)
    # Default fields come through (since exclude_unset is NOT set here).
    assert "duration_seconds" in out
    assert "average_fps" in out
    assert out["rendered"] == "Write1"
    # Validator expanded the inclusive frame range.
    assert out["frames_written"] == [1001, 1002, 1003]


def test_render_result_exclude_unset_strips_defaults() -> None:
    """The boundary helper uses ``exclude_unset=True`` -- defaults disappear."""
    r = RenderResult.model_validate({"rendered": "Write1", "frames": [1001, 1100]})
    out = r.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)
    # Defaults like ``duration_seconds`` are NOT in the dump.
    assert "duration_seconds" not in out
    assert "average_fps" not in out
    # Wire keys preserved.
    assert out["rendered"] == "Write1"
    assert out["frames"] == [1001, 1100]


# ---------------------------------------------------------------------------
# DiffResult (empty lists round-trip)
# ---------------------------------------------------------------------------


def test_diff_result_empty_lists_round_trip() -> None:
    """All-empty diff is valid and dumps with the three list fields."""
    wire = {"added": [], "removed": [], "changed": []}
    d = DiffResult.model_validate(wire)
    out = d.model_dump(by_alias=True, exclude_unset=True)
    assert out == wire


def test_diff_result_with_entries() -> None:
    """A populated diff round-trips list contents intact."""
    wire = {
        "added": [{"name": "A", "type": "Grade"}],
        "removed": [{"name": "B", "type": "Blur"}],
        "changed": [{"name": "C", "knobs": {"mix": {"before": 0.0, "after": 0.5}}}],
    }
    d = DiffResult.model_validate(wire)
    out = d.model_dump(by_alias=True, exclude_unset=True)
    assert out == wire


# ---------------------------------------------------------------------------
# KnobValue scalar variants
# ---------------------------------------------------------------------------


def test_knob_value_scalar_variants() -> None:
    """Every primitive type the addon emits is a valid KnobValue."""
    adapter: TypeAdapter[Any] = TypeAdapter(KnobValue)
    for value in (0, 1.5, "rgba", True, False, None, [1.0, 1.0, 1.0, 1.0]):
        # No exception = accepted.
        out = adapter.validate_python(value)
        assert out == value


def test_knob_value_list_of_mixed_floats() -> None:
    """Color knobs come back as ``list[float]`` -- ensure that path works."""
    adapter: TypeAdapter[Any] = TypeAdapter(KnobValue)
    out = adapter.validate_python([1.0, 0.5, 0.25, 1.0])
    assert out == [1.0, 0.5, 0.25, 1.0]


# ---------------------------------------------------------------------------
# Integration: read_node_detail through the tool boundary
# ---------------------------------------------------------------------------


class _StubMCP:
    """Minimal MCP-tool registry stub, mirroring tests/test_render.py."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        def decorator(func: Any) -> Any:
            self.registered[func.__name__] = func
            return func

        return decorator


class _StubCtx:
    def __init__(self) -> None:
        self.mcp = _StubMCP()
        self.version = None
        self.mock = True


def test_read_node_detail_wire_shape_unchanged(connected) -> None:  # type: ignore[no-untyped-def]
    """Mock addon dict -> NodeInfo -> wire dict shape unchanged.

    The mock server's ``get_node_info`` returns the same fields the
    real addon does (name, type, inputs, knobs, error, warning, x, y).
    We invoke the tool via its registered MCP wrapper and assert each
    wire key passes through.
    """
    from nuke_mcp.tools import read

    connection.send("create_node", type="Grade", name="g")
    connection.send("set_knob", node="g", knob="mix", value=0.5)

    ctx = _StubCtx()
    read.register(ctx)
    result = ctx.mcp.registered["read_node_detail"]("g")

    # Wire keys all present on the way out.
    for k in ("name", "type", "inputs", "knobs"):
        assert k in result, f"missing wire key {k!r} in {result}"
    assert result["name"] == "g"
    assert result["type"] == "Grade"
    # Knob value flows through the model.
    assert result["knobs"]["mix"] == 0.5


def test_read_comp_model_validation_warning_once(monkeypatch, caplog) -> None:
    """Malformed addon payloads remain best-effort but are logged."""
    from nuke_mcp.tools import read

    reset_for_tests()
    monkeypatch.setattr(
        connection,
        "send",
        lambda *_args, **_kwargs: {"nodes": [{"type": "Grade"}], "count": 1},
    )
    ctx = _StubCtx()
    read.register(ctx)

    first = ctx.mcp.registered["read_comp"]()
    second = ctx.mcp.registered["read_comp"]()

    assert first["nodes"] == [{"type": "Grade"}]
    assert second["nodes"] == [{"type": "Grade"}]
    warnings = [
        record for record in caplog.records if "NodeInfo validation failed" in record.getMessage()
    ]
    assert len(warnings) == 1

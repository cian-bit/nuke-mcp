"""Tests for the schema-from-signature decorator (Phase B3).

Each test exercises one slice of the decorator:

* ``_build_input_schema`` -- signature -> JSONSchema, including
  ``Optional`` / ``Literal`` / nested-model parameters.
* ``_build_output_schema`` -- ``output_model`` arg flows through
  Pydantic's ``TypeAdapter`` and lands as a JSONSchema dict.
* metadata stamping -- ``_profile`` / ``_annotations`` / ``_output_model``
  attributes survive on both the wrapped function and the
  ``FunctionTool``.
* registration with a real ``FastMCP`` instance through ``ServerContext``.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastmcp import FastMCP
from pydantic import BaseModel

from nuke_mcp.annotations import IDEMPOTENT, READ_ONLY
from nuke_mcp.registry import (
    _build_input_schema,
    _build_output_schema,
    _description_from_doc,
    nuke_tool,
)

# ---------------------------------------------------------------------------
# Tiny test fixtures: a stand-in ``ServerContext`` and a Pydantic output model.
# ---------------------------------------------------------------------------


class _Ctx:
    """Minimal ServerContext stand-in; only ``mcp`` is touched."""

    def __init__(self) -> None:
        self.mcp = FastMCP("test")


class _Out(BaseModel):
    name: str
    x: int = 0


# ---------------------------------------------------------------------------
# _build_input_schema
# ---------------------------------------------------------------------------


def test_input_schema_required_and_default() -> None:
    """Required params land in ``required``, defaults in per-property dict."""

    def f(node: str, frame: int = 1) -> dict:
        return {}

    schema = _build_input_schema(f)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["node"]
    assert schema["properties"]["node"] == {"type": "string"}
    assert schema["properties"]["frame"]["default"] == 1


def test_input_schema_optional_union() -> None:
    """``str | None`` becomes a JSONSchema ``anyOf``."""

    def f(name: str | None = None) -> dict:
        return {}

    schema = _build_input_schema(f)
    assert "required" not in schema
    prop = schema["properties"]["name"]
    assert prop["default"] is None
    # Pydantic emits ``anyOf: [{type:string},{type:null}]`` for ``Optional``.
    assert "anyOf" in prop


def test_input_schema_literal() -> None:
    """``Literal[...]`` survives and produces an ``enum`` schema."""

    def f(op: Literal["over", "holdout"] = "over") -> dict:
        return {}

    schema = _build_input_schema(f)
    prop = schema["properties"]["op"]
    assert prop.get("enum") == ["over", "holdout"]


def test_input_schema_no_annotation_falls_back_to_str() -> None:
    """Unannotated params don't sink the call -- they default to ``str``."""

    def f(thing) -> dict:  # type: ignore[no-untyped-def]
        return {}

    schema = _build_input_schema(f)
    assert schema["properties"]["thing"] == {"type": "string"}
    assert schema["required"] == ["thing"]


def test_input_schema_skips_var_args() -> None:
    """``*args`` / ``**kwargs`` aren't represented in the schema."""

    def f(node: str, *args: object, **kwargs: object) -> dict:
        return {}

    schema = _build_input_schema(f)
    assert list(schema["properties"].keys()) == ["node"]


# ---------------------------------------------------------------------------
# _build_output_schema
# ---------------------------------------------------------------------------


def test_output_schema_from_pydantic_model() -> None:
    schema = _build_output_schema(_Out)
    assert schema is not None
    assert schema["type"] == "object"
    assert "name" in schema["properties"]
    assert "x" in schema["properties"]
    assert schema["required"] == ["name"]


def test_output_schema_none_passthrough() -> None:
    """No ``output_model`` -> ``None`` (FastMCP opt-out)."""
    assert _build_output_schema(None) is None


# ---------------------------------------------------------------------------
# _description_from_doc
# ---------------------------------------------------------------------------


def test_description_from_doc_strips_leading_whitespace() -> None:
    def f() -> dict:
        """First line.

        Body line.
        """
        return {}

    desc = _description_from_doc(f)
    assert desc is not None
    assert desc.startswith("First line.")
    assert "Body line." in desc


def test_description_from_doc_handles_no_doc() -> None:
    def f() -> dict:
        return {}

    assert _description_from_doc(f) is None


# ---------------------------------------------------------------------------
# nuke_tool decorator end-to-end
# ---------------------------------------------------------------------------


def test_nuke_tool_stamps_metadata_on_function() -> None:
    """The wrapped function carries ``_profile``, ``_annotations``,
    ``_output_model`` for B4 to introspect later.

    FastMCP's ``mcp.tool`` returns the original function unchanged
    after registering it, so attributes stamped before registration
    survive on the same object the call site receives.
    """
    ctx = _Ctx()

    @nuke_tool(ctx, profile="tracking", annotations=READ_ONLY, output_model=_Out)
    def my_tool(node: str) -> _Out:
        """Inspect node."""
        return _Out(name=node)

    assert my_tool._profile == "tracking"  # type: ignore[attr-defined]
    assert my_tool._annotations == READ_ONLY  # type: ignore[attr-defined]
    assert my_tool._output_model is _Out  # type: ignore[attr-defined]


def test_nuke_tool_default_profile_is_core() -> None:
    ctx = _Ctx()

    @nuke_tool(ctx)
    def t1(x: int = 0) -> dict:
        """T1."""
        return {}

    assert t1._profile == "core"  # type: ignore[attr-defined]


def test_nuke_tool_registers_with_fastmcp() -> None:
    """End-to-end: tool surfaces via ``mcp.list_tools`` with the right
    name, description, input schema, output schema, and annotations.
    """
    ctx = _Ctx()

    @nuke_tool(ctx, profile="core", annotations=READ_ONLY, output_model=_Out)
    def read_thing(node: str, frame: int = 1) -> _Out:
        """Look up a thing."""
        return _Out(name=node, x=frame)

    tools = asyncio.run(ctx.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert "read_thing" in by_name
    t = by_name["read_thing"]
    assert (t.description or "").startswith("Look up a thing.")
    # FastMCP exposes the input schema as ``parameters``. Required
    # params, defaults, and additionalProperties=False all flow.
    params = t.parameters
    assert params["required"] == ["node"]
    assert params["properties"]["node"]["type"] == "string"
    assert params["properties"]["frame"]["default"] == 1
    # Output schema came from ``_Out``.
    assert t.output_schema is not None
    assert t.output_schema["properties"]["name"]["type"] == "string"
    # Annotation forwarded.
    assert t.annotations.readOnlyHint is True


def test_nuke_tool_custom_name_overrides_function_name() -> None:
    ctx = _Ctx()

    @nuke_tool(ctx, name="renamed_tool")
    def actually_named_this(x: int = 0) -> dict:
        """Doc."""
        return {}

    tools = asyncio.run(ctx.mcp.list_tools())
    names = {t.name for t in tools}
    assert "renamed_tool" in names
    assert "actually_named_this" not in names


def test_nuke_tool_custom_description_overrides_doc() -> None:
    ctx = _Ctx()

    @nuke_tool(ctx, description="Override desc.")
    def t(x: int = 0) -> dict:
        """Original doc."""
        return {}

    tools = asyncio.run(ctx.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert by_name["t"].description == "Override desc."


def test_nuke_tool_idempotent_annotations_propagate() -> None:
    ctx = _Ctx()

    @nuke_tool(ctx, annotations=IDEMPOTENT)
    def t(x: int = 0) -> dict:
        """Doc."""
        return {}

    tools = asyncio.run(ctx.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    ann = by_name["t"].annotations
    assert ann.idempotentHint is True
    assert ann.destructiveHint is False


def test_nuke_tool_no_output_model_means_no_output_schema() -> None:
    """Tools without ``output_model`` opt out of structured output --
    matches the existing ``output_schema=None`` repo convention.
    """
    ctx = _Ctx()

    @nuke_tool(ctx, annotations=READ_ONLY)
    def t(x: int = 0) -> dict:
        """Doc."""
        return {}

    tools = asyncio.run(ctx.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert by_name["t"].output_schema is None

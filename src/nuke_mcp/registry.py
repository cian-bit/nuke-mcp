"""Schema-from-signature decorator (Phase B3).

The ``@nuke_tool(...)`` decorator is a thin wrapper around FastMCP's
``@mcp.tool`` that pulls four pieces of metadata out of the function
itself:

* ``inspect.signature`` -> JSONSchema input shape (built via Pydantic v2
  ``TypeAdapter`` so unions, ``None`` defaults, ``Literal``, and
  ``BaseModel`` parameters all resolve the same way they would in a
  hand-written Pydantic model).
* ``__doc__`` -> tool description (when no explicit ``description=`` is
  passed).
* ``output_model`` -> JSONSchema output shape (forwarded to FastMCP's
  ``output_schema=`` kwarg).
* ``profile`` / ``annotations`` -> stamped on the wrapped function as
  ``_profile`` / ``_annotations`` for B4 (paginated profile loading) to
  introspect later.

Coexists with raw ``@mcp.tool`` -- migration is opt-in. Each module can
flip its tools one at a time without affecting the others.

The decorator factory takes the ``mcp`` instance up front because tool
modules register against ``ctx.mcp`` (a single ``FastMCP`` server is
built per ``build_server`` call). A free-standing decorator that
"finds" the active server would force global state; the explicit
factory keeps registration scoped to the call site.

Usage::

    from nuke_mcp.registry import nuke_tool
    from nuke_mcp.annotations import READ_ONLY
    from nuke_mcp.models import NodeInfo

    def register(ctx):
        @nuke_tool(ctx, profile="core", annotations=READ_ONLY,
                   output_model=NodeInfo)
        @nuke_command("read_node_detail")
        def read_node_detail(name: str) -> dict:
            '''Inspect a single node.'''
            return connection.send("get_node_info", name=name)
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter

F = TypeVar("F", bound=Callable[..., Any])


def _resolve_hints(func: Callable[..., Any]) -> dict[str, Any]:
    """Resolve ``func``'s annotations through ``typing.get_type_hints``.

    With ``from __future__ import annotations`` (the entire repo) every
    annotation arrives at ``inspect.signature`` as a string. Passing
    those strings straight to ``TypeAdapter`` works for module-level
    functions, but a function defined inside another function (e.g. in
    a test) loses access to its closure when Pydantic eval-resolves the
    string -- ``Literal`` and similar typing imports vanish and the
    schema collapses to ``{}``.

    ``typing.get_type_hints`` uses the function's ``__globals__`` plus
    an ``include_extras=True`` option so ``Annotated``-wrapped types
    survive intact. We swallow any failure (a recursive forward
    reference, a custom non-Pydantic generic) so the decorator doesn't
    blow up at import time.
    """
    try:
        return typing.get_type_hints(func, include_extras=True)
    except Exception:
        return {}


def _build_input_schema(func: Callable[..., Any]) -> dict[str, Any]:
    """Build a JSONSchema for ``func``'s parameters.

    Each parameter gets its annotation run through ``TypeAdapter`` so
    we inherit Pydantic v2's full type-resolution machinery (``Union``,
    ``Literal``, ``Annotated``, nested ``BaseModel`` etc.). Parameters
    without annotations fall back to ``str`` -- matching FastMCP's
    default.

    Defaults are emitted into the per-property schema (so callers see
    the actual default), and a parameter is added to ``required`` only
    when it has no default.
    """
    sig = inspect.signature(func)
    hints = _resolve_hints(func)
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in {"self", "cls"} or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        annotation: Any = hints.get(name, param.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = str
        try:
            schema = TypeAdapter(annotation).json_schema()
        except Exception:
            # Defensive: a quirky annotation (forward-ref string that
            # never resolved, custom non-Pydantic class) shouldn't
            # block tool registration. Fall back to a permissive
            # placeholder so the tool still surfaces.
            schema = {}

        if param.default is not inspect.Parameter.empty:
            schema["default"] = param.default
        else:
            required.append(name)

        properties[name] = schema

    out: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        out["required"] = required
    return out


def _build_output_schema(output_model: type[BaseModel] | None) -> dict[str, Any] | None:
    """Build a JSONSchema for the tool's return type, if a model is set.

    Tools that don't supply an ``output_model`` opt out of structured
    output entirely (FastMCP receives ``None`` and falls back to no
    schema). This matches the existing ``output_schema=None`` pattern
    every tool in the repo currently uses.
    """
    if output_model is None:
        return None
    return TypeAdapter(output_model).json_schema()


def _description_from_doc(func: Callable[..., Any]) -> str | None:
    """Pull ``func.__doc__`` and clean leading/trailing whitespace.

    ``inspect.getdoc`` normalises common-leading-whitespace stripping
    so multi-line docstrings emit the way the source intends.
    """
    return inspect.getdoc(func)


def nuke_tool(
    ctx: Any,
    profile: str = "core",
    annotations: dict[str, Any] | None = None,
    output_model: type[BaseModel] | None = None,
    name: str | None = None,
    description: str | None = None,
) -> Callable[[F], F]:
    """Register ``func`` with FastMCP, deriving schema from its signature.

    Args:
        ctx: ``ServerContext`` carrying the ``FastMCP`` instance. The
            decorator forwards to ``ctx.mcp.tool(...)`` so tools land in
            the same registry as raw ``@mcp.tool`` registrations.
        profile: Skill profile name used by Phase B4 to gate which
            tools FastMCP advertises at startup. Defaults to ``"core"``.
        annotations: MCP tool annotation dict (``READ_ONLY``,
            ``DESTRUCTIVE`` etc.). Forwarded verbatim to FastMCP.
        output_model: Optional Pydantic model class -- when set, becomes
            the tool's ``outputSchema``. Tools that already validate
            their wire payload through a model (``read_comp``,
            ``read_node_detail`` ...) should pass it here so the schema
            is consistent end-to-end.
        name: Override the registered tool name. Defaults to
            ``func.__name__``.
        description: Override the description. Defaults to
            ``func.__doc__``.

    Returns:
        A decorator that registers ``func`` and returns the wrapped
        ``FunctionTool``. The original function gets stamped with
        ``_profile``, ``_annotations``, and ``_output_model`` for later
        introspection (Phase B4 ``profiles.PROFILES`` cross-references
        these).
    """
    annotations = annotations or {}

    def decorator(func: F) -> F:
        tool_name = name or func.__name__
        tool_description = description or _description_from_doc(func)
        input_schema = _build_input_schema(func)
        output_schema = _build_output_schema(output_model)

        # Stamp metadata on the function itself before FastMCP wraps
        # it. ``functools.wraps`` chains in ``nuke_command`` preserve
        # ``__wrapped__`` so the attributes remain reachable.
        func._profile = profile  # type: ignore[attr-defined]
        func._annotations = dict(annotations)  # type: ignore[attr-defined]
        func._output_model = output_model  # type: ignore[attr-defined]
        func._input_schema = input_schema  # type: ignore[attr-defined]
        func._output_schema = output_schema  # type: ignore[attr-defined]

        # FastMCP infers parameters from signatures itself; we still
        # pass ``annotations`` / ``output_schema`` / ``description``
        # explicitly so the decorator's call site is the single
        # source of truth.
        kwargs: dict[str, Any] = {
            "name": tool_name,
            "annotations": annotations,
        }
        if tool_description:
            kwargs["description"] = tool_description
        if output_schema is not None:
            kwargs["output_schema"] = output_schema
        else:
            # Match the existing repo convention -- explicit None opts
            # out of FastMCP's auto-derivation.
            kwargs["output_schema"] = None

        # FastMCP's ``mcp.tool`` returns ``func`` itself after
        # registering it -- the metadata we already stamped on ``func``
        # therefore survives on the value the call site receives.
        registered = ctx.mcp.tool(**kwargs)(func)
        return registered  # type: ignore[return-value]

    return decorator


__all__ = ["nuke_tool"]

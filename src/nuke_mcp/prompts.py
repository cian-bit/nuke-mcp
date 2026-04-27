"""Phase C10: workflow prompts as MCP prompts (separate primitive from tools).

Each ``.md`` file under :mod:`nuke_mcp.prompts` carries a YAML-style
front-matter block followed by a Markdown body that the model gets
back when it calls ``prompts/get``. Front-matter shape::

    ---
    name: build_aov_relight_pipeline
    description: One-line description for prompts/list.
    arguments:
      - name: read_node
        description: What this argument means.
        required: true
      - name: shot
        description: Optional argument.
        required: false
    ---

    # Markdown body

    Use ``{read_node}`` and ``{shot}`` for argument substitution.

The loader builds one ``FunctionPrompt`` per ``.md`` file at registration
time so prompts surface via the standard ``prompts/list`` MCP method,
distinct from ``tools/list``. Prompts are *not* tools -- they don't
appear in :mod:`nuke_mcp.profiles`, they aren't gated by destructive
confirms, and they bypass the registry decorator entirely.

Templating is plain ``str.format``-style ``{name}`` substitution -- no
Jinja2 dependency -- but the renderer only replaces placeholders whose
name matches a declared argument. Stray ``{`` / ``}`` in code blocks,
JSON examples, ASCII diagrams, etc. pass through verbatim. Front-matter
is parsed by a tiny hand-rolled scanner (the format is fixed and shallow,
so a YAML library would be overkill and add a wire-time dep).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp.prompts.function_prompt import FunctionPrompt
from mcp.types import PromptArgument

if TYPE_CHECKING:
    from nuke_mcp.server import ServerContext

log = logging.getLogger(__name__)

# Directory holding the .md prompt files. Resolved at import time so the
# tests can monkeypatch it cleanly if needed.
PROMPTS_DIR: Path = Path(__file__).parent / "prompts"


# ---------------------------------------------------------------------------
# Front-matter parsing
# ---------------------------------------------------------------------------


class PromptParseError(ValueError):
    """Raised when a prompt .md file's front-matter is malformed."""


@dataclass
class PromptArgSpec:
    """One declared argument for a prompt."""

    name: str
    description: str = ""
    required: bool = False


@dataclass
class PromptSpec:
    """Parsed front-matter + body for one prompt file."""

    name: str
    description: str
    arguments: list[PromptArgSpec] = field(default_factory=list)
    body: str = ""
    source_path: Path | None = None

    def render(self, **values: str) -> str:
        """Substitute argument values into the Markdown body.

        Only placeholders whose name matches a declared argument get
        substituted -- stray ``{`` and ``}`` in code blocks, JSON, or
        ASCII diagrams pass through verbatim. This is the difference
        between this renderer and bare ``str.format``.

        Required arguments must appear in ``values`` -- a missing required
        argument raises :class:`PromptParseError` with a clear message.
        Optional arguments default to the empty string when omitted.
        ``None`` values are coerced to empty string for the same reason.
        """
        rendered_values: dict[str, str] = {}
        for arg in self.arguments:
            if arg.name in values and values[arg.name] is not None:
                rendered_values[arg.name] = str(values[arg.name])
            elif arg.required:
                raise PromptParseError(
                    f"prompt {self.name!r}: missing required argument {arg.name!r}"
                )
            else:
                rendered_values[arg.name] = ""

        # Replace only declared-argument placeholders. The regex matches
        # ``{ident}`` with an exact identifier inside; non-matching braces
        # stay where they are. Sub uses a lambda so we can choose per-match
        # whether to substitute.
        declared = {arg.name for arg in self.arguments}

        def _sub(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in declared:
                return rendered_values[key]
            return match.group(0)

        return _PLACEHOLDER_RE.sub(_sub, self.body)


# Matches ``{ident}`` where ident is a Python identifier. Limited to
# identifiers (no dotted access, no format specs) to keep the surface
# small and predictable for prompt authors.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a Markdown file into ``(front_matter_dict, body)``.

    The front-matter block must:
    - start with ``---`` on the first line,
    - end with ``---`` on its own line,
    - contain only the keys ``name``, ``description``, and ``arguments``,
    - express ``arguments`` as a list of ``- name: ...`` mappings.

    Anything else raises :class:`PromptParseError` with a useful message.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PromptParseError("missing leading '---' front-matter delimiter")

    closing = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            closing = idx
            break
    if closing == -1:
        raise PromptParseError("missing closing '---' front-matter delimiter")

    fm_lines = lines[1:closing]
    body = "\n".join(lines[closing + 1 :]).lstrip("\n")

    data: dict[str, Any] = {}
    arguments: list[dict[str, Any]] = []
    current_arg: dict[str, Any] | None = None
    in_arguments = False

    for raw in fm_lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # Top-level key: <key>: <value>  -- detected by zero leading spaces.
        if not raw.startswith(" ") and not raw.startswith("\t"):
            in_arguments = False
            current_arg = None
            if ":" not in raw:
                raise PromptParseError(f"front-matter line missing ':' -> {raw!r}")
            key, _, value = raw.partition(":")
            key = key.strip()
            value = value.strip()
            if key == "arguments":
                in_arguments = True
                # value may be empty (list follows) or "[]"
                if value and value not in ("[]", "[ ]"):
                    raise PromptParseError(
                        "arguments must be a YAML-style block list, " "not an inline value"
                    )
                continue
            if key in {"name", "description"}:
                data[key] = value
                continue
            raise PromptParseError(f"unknown front-matter key {key!r}")
        # Indented line: must be inside arguments.
        if not in_arguments:
            raise PromptParseError(f"unexpected indented line outside arguments: {raw!r}")
        stripped = raw.strip()
        if stripped.startswith("- "):
            # Start a new arg dict.
            current_arg = {}
            arguments.append(current_arg)
            stripped = stripped[2:]
        if current_arg is None:
            raise PromptParseError(f"argument continuation without leading '-': {raw!r}")
        if ":" not in stripped:
            raise PromptParseError(f"argument line missing ':' -> {raw!r}")
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if key not in {"name", "description", "required"}:
            raise PromptParseError(f"unknown argument key {key!r}")
        if key == "required":
            if value.lower() not in {"true", "false"}:
                raise PromptParseError(f"argument 'required' must be true or false, got {value!r}")
            current_arg[key] = value.lower() == "true"
        else:
            current_arg[key] = value

    if arguments:
        data["arguments"] = arguments
    return data, body


def _spec_from_text(text: str, source_path: Path | None = None) -> PromptSpec:
    """Build a :class:`PromptSpec` from a single ``.md`` file's content."""
    fm, body = _parse_frontmatter(text)
    if "name" not in fm:
        raise PromptParseError("front-matter missing required 'name'")
    if "description" not in fm:
        raise PromptParseError("front-matter missing required 'description'")
    args_raw: list[dict[str, Any]] = fm.get("arguments") or []
    arguments: list[PromptArgSpec] = []
    for entry in args_raw:
        if "name" not in entry:
            raise PromptParseError("argument entry missing 'name'")
        arguments.append(
            PromptArgSpec(
                name=entry["name"],
                description=entry.get("description", ""),
                required=bool(entry.get("required", False)),
            )
        )
    return PromptSpec(
        name=fm["name"],
        description=fm["description"],
        arguments=arguments,
        body=body,
        source_path=source_path,
    )


def load_prompt_specs(directory: Path | None = None) -> list[PromptSpec]:
    """Load every ``.md`` file in ``directory`` into a :class:`PromptSpec`.

    Files are discovered via ``directory.glob("*.md")`` and sorted by
    filename so the registration order is deterministic.
    """
    target = directory or PROMPTS_DIR
    if not target.exists():
        return []
    specs: list[PromptSpec] = []
    for path in sorted(target.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        try:
            specs.append(_spec_from_text(text, source_path=path))
        except PromptParseError as exc:
            raise PromptParseError(f"{path.name}: {exc}") from exc
    return specs


# ---------------------------------------------------------------------------
# FastMCP registration
# ---------------------------------------------------------------------------


def _build_function_prompt(spec: PromptSpec) -> FunctionPrompt:
    """Build a FastMCP ``FunctionPrompt`` for one spec.

    A dynamic function is exec'd with a real signature (one parameter
    per declared argument) so FastMCP's introspection picks up the
    argument names; the body is a single call to ``spec.render`` with
    the arguments threaded through. After construction we overwrite
    the prompt's ``arguments`` list so the front-matter descriptions
    win over FastMCP's auto-generated ones.
    """
    # Build a function with a real per-argument signature. FastMCP's
    # FunctionPrompt rejects *args / **kwargs, so we generate a literal
    # parameter list. All args are typed ``str`` -- prompts only flow
    # text -- so FastMCP doesn't append its "provide as JSON string"
    # schema note to argument descriptions.
    param_parts: list[str] = []
    call_parts: list[str] = []
    for arg in spec.arguments:
        if arg.required:
            param_parts.append(f"{arg.name}: str")
        else:
            param_parts.append(f"{arg.name}: str = ''")
        call_parts.append(f"{arg.name}={arg.name}")
    sig_src = ", ".join(param_parts)
    call_src = ", ".join(call_parts)
    # The exec'd function captures ``spec`` from the enclosing scope via
    # the namespace dict so each prompt has its own template.
    src = f"def _prompt_fn({sig_src}) -> str:\n" f"    return _spec.render({call_src})\n"
    ns: dict[str, Any] = {"_spec": spec}
    exec(src, ns)  # noqa: S102 -- spec.body is loaded from a trusted package file
    fn = ns["_prompt_fn"]
    fn.__name__ = spec.name
    fn.__doc__ = spec.description

    prompt = FunctionPrompt.from_function(fn, name=spec.name, description=spec.description)
    # Patch the auto-generated arguments with our front-matter descriptions.
    # FunctionPrompt is a Pydantic model with frozen=False so direct set works,
    # but keep object.__setattr__ for safety against future frozen=True flips.
    object.__setattr__(
        prompt,
        "arguments",
        [
            PromptArgument(
                name=arg.name,
                description=arg.description or None,
                required=arg.required,
            )
            for arg in spec.arguments
        ],
    )
    return prompt


def register_prompts(ctx: ServerContext, directory: Path | None = None) -> list[str]:
    """Load every prompt under ``directory`` and register with the FastMCP server.

    Returns the list of registered prompt names in registration order.
    Called from :func:`nuke_mcp.server.build_server` after ``register_tools``.

    Failures during front-matter parsing raise :class:`PromptParseError`
    -- the server boot path is loud-fail by design so a malformed
    prompt file never silently disappears from the surface.
    """
    specs = load_prompt_specs(directory)
    registered: list[str] = []
    for spec in specs:
        prompt = _build_function_prompt(spec)
        ctx.mcp.add_prompt(prompt)
        registered.append(spec.name)
        log.debug("registered prompt %s from %s", spec.name, spec.source_path)
    return registered

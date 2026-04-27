"""Phase C10 tests: workflow prompts as MCP prompts.

Covers:

* All 8 markdown files load into PromptSpec instances.
* Front-matter parsing extracts name / description / arguments correctly,
  including the ``required`` flag.
* Argument substitution replaces only declared placeholders -- stray
  ``{`` and ``}`` in code blocks pass through verbatim.
* Missing a required argument raises a clear ``PromptParseError``.
* Optional arguments default to empty string when omitted.
* The rendered Markdown body is returned (not the front-matter).
* End-to-end: ``register_prompts`` exposes all 8 via FastMCP's
  ``prompts/list`` and ``prompts/get`` MCP methods.
* Malformed front-matter is rejected with a structured error.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from nuke_mcp.prompts import (
    PROMPTS_DIR,
    PromptParseError,
    PromptSpec,
    _spec_from_text,
    load_prompt_specs,
    register_prompts,
)
from nuke_mcp.server import build_server

# Names declared in the spec.
EXPECTED_PROMPT_NAMES = {
    "build_aov_relight_pipeline",
    "build_deep_holdout_chain",
    "build_smartvector_paint_propagate",
    "build_copycat_dehaze",
    "build_stmap_distortion_envelope",
    "build_planar_track_clean_plate",
    "build_3d_camera_track_project",
    "audit_acescct_consistency",
}


# ---------------------------------------------------------------------------
# Discovery + front-matter parsing
# ---------------------------------------------------------------------------


def test_all_eight_prompts_load() -> None:
    specs = load_prompt_specs()
    names = {s.name for s in specs}
    assert (
        names == EXPECTED_PROMPT_NAMES
    ), f"missing or extra prompts: expected {EXPECTED_PROMPT_NAMES}, got {names}"


def test_every_spec_has_description_and_body() -> None:
    """No silent empties -- every shipped prompt must populate the
    front-matter ``description`` (used by ``prompts/list``) and have a
    non-trivial Markdown body (used by ``prompts/get``).
    """
    for spec in load_prompt_specs():
        assert spec.description, f"{spec.name} has empty description"
        assert spec.body.strip(), f"{spec.name} has empty body"
        # Reasonable lower bound -- workflow prompts are multi-step
        # walkthroughs, not one-liners.
        assert len(spec.body) > 500, f"{spec.name} body too short: {len(spec.body)}"


def test_frontmatter_parses_arguments_with_required_flag() -> None:
    text = textwrap.dedent(
        """\
        ---
        name: demo
        description: Demo prompt.
        arguments:
          - name: alpha
            description: First arg.
            required: true
          - name: beta
            description: Optional second arg.
            required: false
        ---
        # Body

        Use {alpha} and optionally {beta}.
        """
    )
    spec = _spec_from_text(text)
    assert spec.name == "demo"
    assert spec.description == "Demo prompt."
    assert len(spec.arguments) == 2
    assert spec.arguments[0].name == "alpha"
    assert spec.arguments[0].required is True
    assert spec.arguments[1].name == "beta"
    assert spec.arguments[1].required is False


# ---------------------------------------------------------------------------
# Argument substitution
# ---------------------------------------------------------------------------


def test_render_substitutes_declared_placeholders() -> None:
    spec = _spec_from_text(
        textwrap.dedent(
            """\
            ---
            name: demo
            description: d
            arguments:
              - name: read_node
                description: r
                required: true
            ---
            Read = {read_node}
            """
        )
    )
    out = spec.render(read_node="MyRead")
    assert "Read = MyRead" in out


def test_render_leaves_unrelated_braces_alone() -> None:
    """Stray ``{`` / ``}`` in code blocks, JSON, ASCII diagrams must
    pass through. The renderer only substitutes declared argument names.
    """
    spec = _spec_from_text(
        textwrap.dedent(
            """\
            ---
            name: demo
            description: d
            arguments:
              - name: scope
                description: s
                required: false
            ---
            Scope: {scope}

            Example JSON:
            {
              "key": {"nested": "value"}
            }
            """
        )
    )
    out = spec.render(scope="all")
    assert "Scope: all" in out
    assert '"key": {"nested": "value"}' in out
    # Verify the JSON braces survived untouched.
    assert out.count("{") >= 2
    assert out.count("}") >= 2


def test_render_missing_required_arg_raises() -> None:
    spec = _spec_from_text(
        textwrap.dedent(
            """\
            ---
            name: demo
            description: d
            arguments:
              - name: must_have
                description: m
                required: true
            ---
            {must_have}
            """
        )
    )
    with pytest.raises(PromptParseError) as excinfo:
        spec.render()
    assert "must_have" in str(excinfo.value)
    assert "missing required" in str(excinfo.value)


def test_render_optional_arg_defaults_to_empty_string() -> None:
    spec = _spec_from_text(
        textwrap.dedent(
            """\
            ---
            name: demo
            description: d
            arguments:
              - name: shot
                description: optional
                required: false
            ---
            shot=[{shot}]
            """
        )
    )
    out = spec.render()
    assert "shot=[]" in out
    out2 = spec.render(shot="ss_0170")
    assert "shot=[ss_0170]" in out2


def test_render_returns_body_not_frontmatter() -> None:
    """The rendered output must not contain the front-matter delimiters
    or YAML keys -- only the Markdown body, with placeholders filled in.
    """
    for spec in load_prompt_specs():
        # Render with TEST_<argname> for required args, defaults for others.
        kwargs = {a.name: f"TEST_{a.name}" for a in spec.arguments if a.required}
        body = spec.render(**kwargs)
        assert not body.startswith("---"), f"{spec.name} body still has front-matter"
        assert (
            "name:" not in body.splitlines()[0]
        ), f"{spec.name} first line looks like front-matter"


# ---------------------------------------------------------------------------
# Malformed front-matter
# ---------------------------------------------------------------------------


def test_missing_leading_delimiter_raises() -> None:
    with pytest.raises(PromptParseError) as excinfo:
        _spec_from_text("name: x\ndescription: y\n# Body")
    assert "leading '---'" in str(excinfo.value)


def test_missing_closing_delimiter_raises() -> None:
    with pytest.raises(PromptParseError) as excinfo:
        _spec_from_text("---\nname: x\ndescription: y\n# Body")
    assert "closing '---'" in str(excinfo.value)


def test_unknown_front_matter_key_raises() -> None:
    with pytest.raises(PromptParseError):
        _spec_from_text(
            textwrap.dedent(
                """\
                ---
                name: x
                description: y
                bogus: nope
                ---
                body
                """
            )
        )


def test_missing_name_raises() -> None:
    with pytest.raises(PromptParseError) as excinfo:
        _spec_from_text(
            textwrap.dedent(
                """\
                ---
                description: y
                ---
                body
                """
            )
        )
    assert "'name'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# End-to-end via FastMCP Client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_exposes_all_eight_prompts_via_prompts_list() -> None:
    """Build the full server in mock mode and verify ``prompts/list``
    returns every Phase C10 prompt with the right argument count.
    """
    mcp = build_server(mock=True)
    async with Client(mcp) as client:
        prompts = await client.list_prompts()
        names = {p.name for p in prompts}
        assert EXPECTED_PROMPT_NAMES.issubset(
            names
        ), f"missing prompts: {EXPECTED_PROMPT_NAMES - names}"
        # Per-prompt argument count matches the front-matter.
        by_name = {p.name: p for p in prompts}
        specs = {s.name: s for s in load_prompt_specs()}
        for name in EXPECTED_PROMPT_NAMES:
            assert len(by_name[name].arguments) == len(
                specs[name].arguments
            ), f"{name} arg count mismatch"


@pytest.mark.asyncio
async def test_get_prompt_renders_markdown_body() -> None:
    """``prompts/get`` returns the rendered Markdown wrapped as a user
    message. Verify the substitution actually happened on the wire.
    """
    mcp = build_server(mock=True)
    async with Client(mcp) as client:
        result = await client.get_prompt(
            "build_aov_relight_pipeline",
            {"read_node": "MyRead", "shot": "ss_0170"},
        )
        assert len(result.messages) == 1
        text = result.messages[0].content.text
        assert "MyRead" in text
        assert "ss_0170" in text
        # Make sure we got the Markdown body, not the front-matter.
        assert text.lstrip().startswith("# ")


@pytest.mark.asyncio
async def test_prompts_arguments_carry_descriptions() -> None:
    """Argument descriptions from the front-matter must surface on the
    wire so MCP clients can show useful hints.
    """
    mcp = build_server(mock=True)
    async with Client(mcp) as client:
        prompts = await client.list_prompts()
        by_name = {p.name: p for p in prompts}
        aov = by_name["build_aov_relight_pipeline"]
        read_node_arg = next(a for a in aov.arguments if a.name == "read_node")
        assert read_node_arg.required is True
        assert read_node_arg.description
        assert "Read" in read_node_arg.description


# ---------------------------------------------------------------------------
# Coverage filler -- exercise the registration helper directly
# ---------------------------------------------------------------------------


def test_register_prompts_returns_registered_names(tmp_path: Path) -> None:
    """``register_prompts`` against an empty directory returns an empty
    list -- the loud-fail path only triggers for malformed files.
    """
    mcp = FastMCP("test")

    class Ctx:
        def __init__(self, m: FastMCP) -> None:
            self.mcp = m

    names = register_prompts(Ctx(mcp), directory=tmp_path)
    assert names == []


def test_register_prompts_loud_fails_on_bad_file(tmp_path: Path) -> None:
    """A malformed prompt file at boot is a programmer error -- the
    loader should raise rather than silently drop the prompt.
    """
    bad = tmp_path / "broken.md"
    bad.write_text("no front matter here", encoding="utf-8")
    mcp = FastMCP("test")

    class Ctx:
        def __init__(self, m: FastMCP) -> None:
            self.mcp = m

    with pytest.raises(PromptParseError):
        register_prompts(Ctx(mcp), directory=tmp_path)


def test_prompts_dir_constant_resolves() -> None:
    """Sanity: the package ships its prompt files at the expected path."""
    assert PROMPTS_DIR.exists()
    assert PROMPTS_DIR.is_dir()
    md_files = list(PROMPTS_DIR.glob("*.md"))
    assert len(md_files) == 8


def test_prompt_spec_dataclass_round_trip() -> None:
    """PromptSpec is a thin dataclass -- exercise it directly so the
    coverage gate doesn't get cute about line counts.
    """
    spec = PromptSpec(name="x", description="y", arguments=[], body="z")
    assert spec.name == "x"
    assert spec.body == "z"
    assert spec.arguments == []

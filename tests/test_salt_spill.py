"""Tests for salt_spill.py (Phase C8).

The C8 macros are flag-planters: each one composes existing C2-C7 + C9
primitives, wraps the result in a Group, and stamps a Backdrop labelled
with ``<shot> # C8 v1`` so the operator can identify which auto-built
block came from which C-phase tool version.

These tests cover, per macro:

* happy-path return shape (group + backdrop + tool_version);
* idempotency on ``name=``;
* ``SS_SHOT`` env-var drives the auto-name;
* the Backdrop's ``label`` carries shot + tool version.

Plus one composition test per audit/composition path:

* ``audit_comp_for_acescct_consistency_ss`` aggregates findings from C2
  + C9 primitives with the ``source`` field stamped per origin.
* The deep-comp macros (FLIP blood, sand/dust) record the inner
  ``setup_flip_blood_comp`` call in ``server.typed_calls`` so we can
  see they didn't reimplement the wiring.

The mock-side handlers in ``conftest.MockNukeServer._setup_*_ss`` mirror
the addon orchestrator: each appends its own ``typed_calls`` entry, calls
the relevant inner mock sub-handler, and stamps a BackdropNode entry on
``server.nodes``. The 9 builders register their wrapper Group as a Group
node; the audit composition is read-only and creates no nodes.
"""

from __future__ import annotations

from typing import Any

import pytest

from nuke_mcp.tools import salt_spill


def test_module_importable() -> None:
    """Module is importable and exposes register()."""
    assert salt_spill is not None
    assert hasattr(salt_spill, "register")


# ---------------------------------------------------------------------------
# Stub MCP infrastructure (mirrors the other workflow test modules).
# ---------------------------------------------------------------------------


class _StubMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def decorator(func):
            self.registered[func.__name__] = func
            return func

        return decorator


class _StubCtx:
    def __init__(self) -> None:
        self.mcp = _StubMCP()
        self.version = None
        self.mock = True


@pytest.fixture
def ss_tools(mock_script, monkeypatch):
    """Register C8 macros against a connected mock server.

    Pre-seeds every input node referenced by any of the 10 macros so each
    test can call any macro without setting up its own fixtures. Also
    sets ``SS_SHOT=ss_0170`` and the Salt Spill colour-management defaults
    so the auto-derived names land deterministically.
    """
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    monkeypatch.setenv("NUKE_MCP_SS_ROOT", "/tmp/fmp")  # noqa: S108 -- deterministic fixture root
    server, script = mock_script

    # Plates / beauty / FX inputs.
    for name in (
        "plate",
        "beauty",
        "deep_pass",
        "motion",
        "holdoutRoto",
        "normalPass",
        "positionPass",
        "lensSolve",
        "patchSrc",
    ):
        server.nodes[name] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
        server.connections[name] = []
    # ACEScg working space so audit_acescct_consistency fires the
    # Grade-without-ACEScct heuristic when warranted.
    server.color_management["working_space"] = "ACES - ACEScg"

    ctx = _StubCtx()
    salt_spill.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# Signature/registration pin
# ---------------------------------------------------------------------------


def test_all_ten_tools_registered(ss_tools) -> None:
    _server, _script, tools = ss_tools
    expected = {
        "setup_karma_aov_pipeline_ss",
        "setup_flip_blood_comp_ss",
        "setup_sand_dust_layer",
        "setup_salt_structure_relight",
        "setup_dehaze_copycat_ss",
        "setup_smartvector_paint_propagate_ss",
        "setup_spaceship_track_patch_ss",
        "setup_scream_shot_lensflare",
        "audit_comp_for_acescct_consistency_ss",
        "bake_lens_distortion_envelope_ss",
    }
    assert expected.issubset(set(tools.keys())), sorted(expected - set(tools.keys()))


# ---------------------------------------------------------------------------
# Happy-path: each macro returns the expected wrapper payload
# ---------------------------------------------------------------------------


def test_setup_karma_aov_pipeline_ss_happy_path(ss_tools) -> None:
    server, _script, tools = ss_tools
    result = tools["setup_karma_aov_pipeline_ss"]()
    assert result.get("status") != "error"
    assert result["group"] == "KarmaAOV_ss_0170"
    assert result["backdrop"] == "KarmaAOV_ss_0170_bd"
    assert result["tool_version"] == "C8 v1"
    # The Backdrop carries the shot + tool-version label.
    bd_label = server.nodes[result["backdrop"]]["knobs"]["label"]
    assert bd_label == "ss_0170 # C8 v1"
    # Read-path defaulted under $NUKE_MCP_SS_ROOT.
    cmd_params = next(p for cmd, p in server.typed_calls if cmd == "setup_karma_aov_pipeline_ss")
    assert cmd_params["read_path"].endswith("ss_0170.####.exr")


def test_setup_flip_blood_comp_ss_happy_path(ss_tools) -> None:
    server, _script, tools = ss_tools
    result = tools["setup_flip_blood_comp_ss"]("beauty", "deep_pass")
    assert result.get("status") != "error"
    assert result["group"] == "FLIP_Blood_ss_0170"
    assert result["backdrop"] == "FLIP_Blood_ss_0170_bd"
    assert result["tool_version"] == "C8 v1"
    # Default write path lands under $SS/renders/<shot>_blood/.
    assert "_blood" in result["write_path"]
    # The macro composed the C6 inner handler -- typed_calls records
    # the inner ``setup_flip_blood_comp`` after the orchestrator's own
    # entry.
    cmds = [c for c, _p in server.typed_calls]
    assert "setup_flip_blood_comp_ss" in cmds
    assert "setup_flip_blood_comp" in cmds


def test_setup_sand_dust_layer_happy_path(ss_tools) -> None:
    server, _script, tools = ss_tools
    result = tools["setup_sand_dust_layer"]("beauty", "deep_pass")
    assert result["group"] == "SandDust_ss_0170"
    assert result["backdrop"] == "SandDust_ss_0170_bd"
    assert result["tool_version"] == "C8 v1"
    assert "_dust" in result["write_path"]
    # Composition: sand-dust orchestrator -> inner FLIP-blood handler.
    cmds = [c for c, _p in server.typed_calls]
    assert "setup_sand_dust_layer" in cmds
    assert "setup_flip_blood_comp" in cmds
    # Sand tint propagates onto the Grade as the multiply triple.
    grade_name = result["members"]["grade"]
    grade_knobs = server.nodes[grade_name]["knobs"]
    assert grade_knobs["multiply"] == [0.78, 0.62, 0.41]


def test_setup_salt_structure_relight_happy_path(ss_tools) -> None:
    server, _script, tools = ss_tools
    result = tools["setup_salt_structure_relight"](
        "beauty",
        "normalPass",
        "positionPass",
    )
    assert result["group"] == "SaltRelight_ss_0170"
    assert result["backdrop"] == "SaltRelight_ss_0170_bd"
    relight_name = result["members"]["relight"]
    assert server.nodes[relight_name]["type"] == "Relight"


def test_setup_dehaze_copycat_ss_happy_path(ss_tools) -> None:
    _server, _script, tools = ss_tools
    result = tools["setup_dehaze_copycat_ss"](
        ["/tmp/haze1.exr", "/tmp/haze2.exr"],  # noqa: S108
        ["/tmp/clean1.exr", "/tmp/clean2.exr"],  # noqa: S108
    )
    assert result["group"] == "Dehaze_ss_0170"
    assert result["tool_version"] == "C8 v1"
    # Default model path lands under $SS/comp/models/.
    assert "dehaze_ss_0170" in result["model_path"]
    assert result["model_path"].endswith(".cat")


def test_setup_smartvector_paint_propagate_ss_happy_path(ss_tools) -> None:
    _server, _script, tools = ss_tools
    result = tools["setup_smartvector_paint_propagate_ss"]("plate", 1010, 1001, 1100)
    assert result["group"] == "PaintProp_ss_0170"
    assert result["backdrop"] == "PaintProp_ss_0170_bd"
    assert result["paint_frame"] == 1010
    assert result["range_in"] == 1001
    assert result["range_out"] == 1100
    # Cache root pinned under $SS/comp/paint_cache/<shot>/.
    assert "paint_cache" in result["cache_root"]
    assert "ss_0170" in result["cache_root"]


def test_setup_spaceship_track_patch_ss_happy_path(ss_tools) -> None:
    server, _script, tools = ss_tools
    result = tools["setup_spaceship_track_patch_ss"]("plate", 1042, "planar")
    assert result["group"] == "SpaceshipPatch_ss_0170"
    assert result["backdrop"] == "SpaceshipPatch_ss_0170_bd"
    # The C5 inner handler composes the planar branch (PlanarTracker +
    # CornerPin) -- the typed_calls log shows our orchestrator calling
    # the C5 inner handler.
    cmds = [c for c, _p in server.typed_calls]
    assert "setup_spaceship_track_patch_ss" in cmds
    assert "setup_spaceship_track_patch" in cmds


def test_setup_scream_shot_lensflare_happy_path(ss_tools) -> None:
    server, _script, tools = ss_tools
    result = tools["setup_scream_shot_lensflare"]("beauty")
    assert result["group"] == "ScreamFlare_ss_0170"
    assert result["backdrop"] == "ScreamFlare_ss_0170_bd"
    glow_name = result["members"]["glow"]
    flare_name = result["members"]["flare"]
    assert server.nodes[glow_name]["type"] == "Glow2"
    assert server.nodes[flare_name]["type"] == "Flare2"


def test_audit_comp_for_acescct_consistency_ss_happy_path(ss_tools) -> None:
    _server, _script, tools = ss_tools
    result = tools["audit_comp_for_acescct_consistency_ss"]()
    # Audit composition is READ_ONLY -- no group/backdrop fields, just
    # findings + sources.
    assert "findings" in result
    assert "sources" in result
    assert set(result["sources"]) == {"color", "render", "naming"}
    assert result["tool_version"] == "C8 v1"


def test_bake_lens_distortion_envelope_ss_happy_path(ss_tools) -> None:
    server, _script, tools = ss_tools
    result = tools["bake_lens_distortion_envelope_ss"]("plate", "lensSolve")
    assert result["box"] == "LinearComp_ss_0170"
    assert result["backdrop"] == "LinearComp_ss_0170_bd"
    assert result["tool_version"] == "C8 v1"
    # The C4 inner handler ran -- typed_calls records it.
    cmds = [c for c, _p in server.typed_calls]
    assert "bake_lens_distortion_envelope_ss" in cmds
    assert "bake_lens_distortion_envelope" in cmds
    # STMap caches resolved under the per-shot root.
    stmap_paths = result["stmap_paths"]
    assert "ss_0170_undistort" in stmap_paths["undistort"]
    assert "ss_0170_redistort" in stmap_paths["redistort"]


# ---------------------------------------------------------------------------
# Idempotency on name= -- a re-call with the same explicit name returns
# the cached payload without re-invoking the inner sub-handler.
# ---------------------------------------------------------------------------


def _count_inner_calls(server, inner_cmd: str) -> int:
    return sum(1 for cmd, _p in server.typed_calls if cmd == inner_cmd)


def test_setup_karma_aov_pipeline_ss_idempotent(ss_tools) -> None:
    server, _script, tools = ss_tools
    first = tools["setup_karma_aov_pipeline_ss"](name="aovBlock")
    second = tools["setup_karma_aov_pipeline_ss"](name="aovBlock")
    assert first["group"] == second["group"] == "aovBlock"
    # The inner C3 handler ran exactly once across the two calls.
    assert _count_inner_calls(server, "setup_karma_aov_pipeline") == 1


def test_setup_flip_blood_comp_ss_idempotent(ss_tools) -> None:
    server, _script, tools = ss_tools
    first = tools["setup_flip_blood_comp_ss"]("beauty", "deep_pass", name="bloodBlock")
    second = tools["setup_flip_blood_comp_ss"]("beauty", "deep_pass", name="bloodBlock")
    assert first["group"] == second["group"] == "bloodBlock"
    assert _count_inner_calls(server, "setup_flip_blood_comp") == 1


def test_setup_sand_dust_layer_idempotent(ss_tools) -> None:
    server, _script, tools = ss_tools
    first = tools["setup_sand_dust_layer"]("beauty", "deep_pass", name="dustBlock")
    second = tools["setup_sand_dust_layer"]("beauty", "deep_pass", name="dustBlock")
    assert first["group"] == second["group"] == "dustBlock"
    # Sand-dust composes the same C6 inner handler as FLIP blood.
    assert _count_inner_calls(server, "setup_flip_blood_comp") == 1


def test_setup_salt_structure_relight_idempotent(ss_tools) -> None:
    server, _script, tools = ss_tools
    first = tools["setup_salt_structure_relight"](
        "beauty", "normalPass", "positionPass", name="reliBlock"
    )
    second = tools["setup_salt_structure_relight"](
        "beauty", "normalPass", "positionPass", name="reliBlock"
    )
    assert first["group"] == second["group"] == "reliBlock"
    # Only one Relight in the graph after two calls -- no duplication.
    relights = [n for n, d in server.nodes.items() if d["type"] == "Relight"]
    assert len(relights) == 1


def test_setup_dehaze_copycat_ss_idempotent(ss_tools) -> None:
    server, _script, tools = ss_tools
    first = tools["setup_dehaze_copycat_ss"](["/h.exr"], ["/c.exr"], name="dehazeBlock")
    second = tools["setup_dehaze_copycat_ss"](["/h.exr"], ["/c.exr"], name="dehazeBlock")
    assert first["group"] == second["group"] == "dehazeBlock"
    groups = [n for n, d in server.nodes.items() if d["type"] == "Group" and n == "dehazeBlock"]
    assert len(groups) == 1


def test_setup_smartvector_paint_propagate_ss_idempotent(ss_tools) -> None:
    server, _script, tools = ss_tools
    first = tools["setup_smartvector_paint_propagate_ss"](
        "plate", 1010, 1001, 1100, name="paintBlock"
    )
    second = tools["setup_smartvector_paint_propagate_ss"](
        "plate", 1010, 1001, 1100, name="paintBlock"
    )
    assert first["group"] == second["group"] == "paintBlock"
    groups = [n for n, d in server.nodes.items() if d["type"] == "Group" and n == "paintBlock"]
    assert len(groups) == 1


def test_setup_spaceship_track_patch_ss_idempotent(ss_tools) -> None:
    server, _script, tools = ss_tools
    first = tools["setup_spaceship_track_patch_ss"]("plate", 1042, "planar", name="patchBlock")
    second = tools["setup_spaceship_track_patch_ss"]("plate", 1042, "planar", name="patchBlock")
    assert first["group"] == second["group"] == "patchBlock"
    # The C5 inner handler ran exactly once.
    assert _count_inner_calls(server, "setup_spaceship_track_patch") == 1


def test_setup_scream_shot_lensflare_idempotent(ss_tools) -> None:
    server, _script, tools = ss_tools
    first = tools["setup_scream_shot_lensflare"]("beauty", name="flareBlock")
    second = tools["setup_scream_shot_lensflare"]("beauty", name="flareBlock")
    assert first["group"] == second["group"] == "flareBlock"
    # No duplicate Glow2 / Flare2 / Grade after the second call.
    for cls in ("Glow2", "Flare2"):
        matching = [n for n, d in server.nodes.items() if d["type"] == cls]
        assert len(matching) == 1, f"duplicate {cls} after idempotent re-call"


def test_audit_comp_for_acescct_consistency_ss_idempotent(ss_tools) -> None:
    """The audit is read-only -- two calls return the same shape with no
    accumulating side effects.
    """
    _server, _script, tools = ss_tools
    first = tools["audit_comp_for_acescct_consistency_ss"]()
    second = tools["audit_comp_for_acescct_consistency_ss"]()
    assert first["sources"] == second["sources"]
    assert len(first["findings"]) == len(second["findings"])


def test_bake_lens_distortion_envelope_ss_idempotent(ss_tools) -> None:
    server, _script, tools = ss_tools
    first = tools["bake_lens_distortion_envelope_ss"]("plate", "lensSolve", name="lensBox")
    second = tools["bake_lens_distortion_envelope_ss"]("plate", "lensSolve", name="lensBox")
    assert first["box"] == second["box"] == "lensBox"
    # The C4 inner handler ran exactly once.
    assert _count_inner_calls(server, "bake_lens_distortion_envelope") == 1


# ---------------------------------------------------------------------------
# Backdrop annotation present on every wrapper Group
# ---------------------------------------------------------------------------


def test_every_builder_stamps_a_backdrop(ss_tools) -> None:
    """Each of the 9 builders registers a BackdropNode whose label
    embeds the shot code + tool version. ``audit_..._ss`` is read-only
    and is excluded.
    """
    server, _script, tools = ss_tools

    builders = [
        ("setup_karma_aov_pipeline_ss", lambda f: f()),
        ("setup_flip_blood_comp_ss", lambda f: f("beauty", "deep_pass")),
        ("setup_sand_dust_layer", lambda f: f("beauty", "deep_pass")),
        (
            "setup_salt_structure_relight",
            lambda f: f("beauty", "normalPass", "positionPass"),
        ),
        ("setup_dehaze_copycat_ss", lambda f: f(["/h.exr"], ["/c.exr"])),
        (
            "setup_smartvector_paint_propagate_ss",
            lambda f: f("plate", 1010, 1001, 1100),
        ),
        (
            "setup_spaceship_track_patch_ss",
            lambda f: f("plate", 1042, "planar"),
        ),
        ("setup_scream_shot_lensflare", lambda f: f("beauty")),
        ("bake_lens_distortion_envelope_ss", lambda f: f("plate", "lensSolve")),
    ]
    for tool_name, runner in builders:
        result = runner(tools[tool_name])
        bd_name = result["backdrop"]
        assert bd_name in server.nodes, f"{tool_name} did not stamp backdrop"
        bd = server.nodes[bd_name]
        assert bd["type"] == "BackdropNode"
        label = bd["knobs"]["label"]
        # ``<shot> # C8 v1`` -- both halves required.
        assert "ss_0170" in label, f"{tool_name} backdrop label missing shot: {label}"
        assert "C8 v1" in label, f"{tool_name} backdrop label missing tool version: {label}"


# ---------------------------------------------------------------------------
# SS_SHOT env-var override drives the auto-name
# ---------------------------------------------------------------------------


def test_ss_shot_env_drives_auto_group_name(mock_script, monkeypatch) -> None:
    """``SS_SHOT=ss_0042`` -> auto-names land at ``KarmaAOV_ss_0042`` etc."""
    monkeypatch.setenv("SS_SHOT", "ss_0042")
    monkeypatch.setenv("NUKE_MCP_SS_ROOT", "/tmp/fmp")  # noqa: S108
    server, _script = mock_script
    server.nodes["beauty"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["beauty"] = []
    server.nodes["deep_pass"] = {"type": "DeepRead", "knobs": {}, "x": 0, "y": 0}
    server.connections["deep_pass"] = []
    ctx = _StubCtx()
    salt_spill.register(ctx)
    tools = ctx.mcp.registered

    result = tools["setup_flip_blood_comp_ss"]("beauty", "deep_pass")
    assert result["group"] == "FLIP_Blood_ss_0042"
    assert "ss_0042" in result["write_path"]
    label = server.nodes[result["backdrop"]]["knobs"]["label"]
    assert "ss_0042" in label


# ---------------------------------------------------------------------------
# Audit composition: findings come from C2 + C9 sources
# ---------------------------------------------------------------------------


def test_audit_composition_includes_findings_from_c2_and_c9(mock_script, monkeypatch) -> None:
    """The audit composer pulls findings from ``audit_acescct_consistency``
    (C2), ``audit_render_settings`` (C9), and ``audit_naming_convention``
    (C9). Each finding gets a ``source`` field tagging its origin.
    """
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    monkeypatch.setenv("NUKE_MCP_SS_ROOT", "/tmp/fmp")  # noqa: S108
    server, _script = mock_script

    # Seed conditions that fire findings on each audit:
    # 1. Read with default colorspace + non-linear path -> C2 warning.
    server.color_management["working_space"] = "ACES - ACEScg"
    server.nodes["plate_sRGB"] = {
        "type": "Read",
        "knobs": {"colorspace": "default", "file": "/tmp/plate_sRGB.png"},  # noqa: S108
        "x": 0,
        "y": 0,
    }
    server.connections["plate_sRGB"] = []
    # 2. Wrong fps -> C9 audit_render_settings error.
    server.script_info["fps"] = 30.0
    # 3. Node name without ss_ prefix -> C9 audit_naming_convention warning.
    server.nodes["someBadName"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["someBadName"] = []

    ctx = _StubCtx()
    salt_spill.register(ctx)
    tools = ctx.mcp.registered

    result = tools["audit_comp_for_acescct_consistency_ss"]()
    sources_seen = {f["source"] for f in result["findings"]}
    # All three audit branches contributed at least one finding.
    assert "color" in sources_seen
    assert "render" in sources_seen
    assert "naming" in sources_seen


# ---------------------------------------------------------------------------
# Profile registration: every C8 tool lives in the salt_spill profile
# ---------------------------------------------------------------------------


def test_flip_blood_motion_and_holdout_kwargs_pass_through(ss_tools) -> None:
    """Optional ``motion`` and ``holdout_roto`` kwargs flow into the wire payload."""
    server, _script, tools = ss_tools
    tools["setup_flip_blood_comp_ss"](
        "beauty",
        "deep_pass",
        motion="motion",
        holdout_roto="holdoutRoto",
        name="bloodWithMotion",
    )
    cmd_params = next(p for cmd, p in server.typed_calls if cmd == "setup_flip_blood_comp_ss")
    assert cmd_params["motion"] == "motion"
    assert cmd_params["holdout_roto"] == "holdoutRoto"


def test_sand_dust_motion_kwarg_passes_through(ss_tools) -> None:
    server, _script, tools = ss_tools
    tools["setup_sand_dust_layer"](
        "beauty",
        "deep_pass",
        motion="motion",
        name="dustWithMotion",
    )
    cmd_params = next(p for cmd, p in server.typed_calls if cmd == "setup_sand_dust_layer")
    assert cmd_params["motion"] == "motion"


def test_spaceship_track_patch_source_kwarg_passes_through(ss_tools) -> None:
    """Optional ``patch_source`` kwarg flows into the wire payload."""
    server, _script, tools = ss_tools
    tools["setup_spaceship_track_patch_ss"](
        "plate", 1042, "planar", patch_source="patchSrc", name="patchWithSource"
    )
    cmd_params = next(p for cmd, p in server.typed_calls if cmd == "setup_spaceship_track_patch_ss")
    assert cmd_params["patch_source"] == "patchSrc"


def test_bake_lens_distortion_envelope_ss_explicit_write_path(ss_tools) -> None:
    """``write_path`` kwarg flows through to the inner C4 handler."""
    server, _script, tools = ss_tools
    tools["bake_lens_distortion_envelope_ss"](
        "plate",
        "lensSolve",
        write_path="/tmp/explicit.exr",  # noqa: S108
        name="lensExplicit",
    )
    cmd_params = next(
        p for cmd, p in server.typed_calls if cmd == "bake_lens_distortion_envelope_ss"
    )
    assert cmd_params["write_path"] == "/tmp/explicit.exr"  # noqa: S108


def test_resolve_ss_root_prefers_ss_env(monkeypatch) -> None:
    """``$SS`` wins over ``$NUKE_MCP_SS_ROOT`` and the home fallback."""
    monkeypatch.setenv("SS", "/tmp/saltspill")  # noqa: S108
    monkeypatch.setenv("NUKE_MCP_SS_ROOT", "/tmp/override")  # noqa: S108
    root = salt_spill._resolve_ss_root()
    assert str(root).replace("\\", "/") == "/tmp/saltspill"


def test_resolve_ss_root_falls_back_to_home_dir(monkeypatch) -> None:
    """No ``$SS`` and no ``$NUKE_MCP_SS_ROOT`` -> ``~/.nuke_mcp``."""
    monkeypatch.delenv("SS", raising=False)
    monkeypatch.delenv("NUKE_MCP_SS_ROOT", raising=False)
    root = salt_spill._resolve_ss_root()
    assert str(root).endswith(".nuke_mcp")


def test_resolve_shot_falls_back_to_unknown(monkeypatch) -> None:
    """No ``SS_SHOT`` and no ``NUKE_MCP_SS_SHOT`` -> ``"unknown"``."""
    monkeypatch.delenv("SS_SHOT", raising=False)
    monkeypatch.delenv("NUKE_MCP_SS_SHOT", raising=False)
    assert salt_spill._resolve_shot() == "unknown"


def test_resolve_shot_uses_nuke_mcp_ss_shot_override(monkeypatch) -> None:
    """``NUKE_MCP_SS_SHOT`` is honoured when ``SS_SHOT`` is unset."""
    monkeypatch.delenv("SS_SHOT", raising=False)
    monkeypatch.setenv("NUKE_MCP_SS_SHOT", "ss_override")
    assert salt_spill._resolve_shot() == "ss_override"


def test_salt_spill_profile_lists_all_ten_tools() -> None:
    from nuke_mcp.profiles import PROFILES

    assert "salt_spill" in PROFILES
    expected = {
        "setup_karma_aov_pipeline_ss",
        "setup_flip_blood_comp_ss",
        "setup_sand_dust_layer",
        "setup_salt_structure_relight",
        "setup_dehaze_copycat_ss",
        "setup_smartvector_paint_propagate_ss",
        "setup_spaceship_track_patch_ss",
        "setup_scream_shot_lensflare",
        "audit_comp_for_acescct_consistency_ss",
        "bake_lens_distortion_envelope_ss",
    }
    assert set(PROFILES["salt_spill"]) == expected

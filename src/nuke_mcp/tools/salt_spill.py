"""Salt Spill macros (Phase C8).

Ten flag-planter macros that compose the C2-C7 + C9 primitives into the
production-shaped graphs an artist would otherwise wire by hand for each
Salt Spill comp shot. Every macro emits a SubGraphRef wrapper Group
(``KarmaAOV_<shot>``, ``FLIP_Blood_<shot>``, etc.) and writes a Backdrop
labelled with the shot code plus this module's tool version (``# C8 v1``)
so the operator can spot at a glance which auto-built block is which.

Composition contract
--------------------
None of these tools reimplement the lower-level node wiring -- the
addon-side ``_ss`` orchestrator handler dispatches through the existing
typed sub-handlers (``setup_karma_aov_pipeline``, ``setup_flip_blood_comp``,
``bake_lens_distortion_envelope``, ``setup_dehaze_copycat``,
``setup_spaceship_track_patch``, ``audit_acescct_consistency``,
``audit_render_settings``, ``audit_naming_convention``, etc.). The macros
here are the *flag-planters* -- they pre-bake the Salt-Spill-specific
defaults (``$SS/renders/<shot>/``, ``$SS/comp/models/``,
``$SS/comp/stmaps/``, ``$SS/comp/paint_cache/``) so a re-call from a fresh
session lands the same graph each time.

Shot resolution
---------------
``<shot>`` falls out of (in order): the ``SS_SHOT`` environment variable,
the ``$NUKE_MCP_SS_SHOT`` override, the script's basename stem, or the
literal ``"unknown"``. The same pattern is in
:func:`_handle_setup_flip_blood_comp` etc. so every macro produces the
same shot label for a given session.

Idempotency
-----------
Every macro accepts ``name=`` and is idempotent on it -- a second call
with the same explicit name returns the existing wrapper Group's NodeRef
without rebuilding the inner sub-graph. Default (``name=None``) follows
the per-tool naming convention (``KarmaAOV_<shot>`` etc.) so a re-call
in the same shot also short-circuits when the auto-name still matches.

Annotations
-----------
* Nine builders carry ``BENIGN_NEW`` -- they create new nodes, never
  destroy. A duplicate call lands ``Foo1`` / ``Foo2`` etc. by default,
  but the ``name=`` kwarg makes them deterministic when the caller wants.
* ``audit_comp_for_acescct_consistency_ss`` is ``READ_ONLY`` -- it
  composes three audit primitives and returns a flat findings list with
  no graph mutation.

Salt Spill defaults
-------------------
``$SS`` resolves through (in order) the ``SS`` env var, the
``NUKE_MCP_SS_ROOT`` override, then ``~/.nuke_mcp/`` as a last-resort
fallback. The Salt Spill production layout puts every comp asset under
``$SS/{renders,comp}`` so the macros default the per-shot read/write
roots to ``$SS/renders/<shot>/`` and ``$SS/comp/{models,stmaps,paint_cache}/``
respectively.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Literal

from nuke_mcp.annotations import BENIGN_NEW, READ_ONLY
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


# Tool version stamped on every Backdrop label so the operator can
# identify which C-phase shipped the auto-built block. Bump when the
# C8 contract changes (e.g. a new sub-handler wired in).
C8_TOOL_VERSION = "C8 v1"


# ---------------------------------------------------------------------------
# Shot + path resolution helpers
# ---------------------------------------------------------------------------


def _resolve_ss_root() -> pathlib.Path:
    """Return the Salt Spill sandbox root.

    Search order matches the rest of the repo (``distortion.py``,
    ``ml.py``):

    1. ``$SS`` -- the canonical Salt Spill production env var.
    2. ``$NUKE_MCP_SS_ROOT`` -- explicit override for callers running
       outside the FMP tree (CI, sibling shows, dev sandboxes).
    3. ``~/.nuke_mcp/`` -- last-resort fallback that always exists.
    """
    ss = os.environ.get("SS")
    if ss:
        return pathlib.Path(ss)
    override = os.environ.get("NUKE_MCP_SS_ROOT")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".nuke_mcp"


def _resolve_shot() -> str:
    """Return the shot identifier for this session.

    Mirrors the addon-side ``_shot_id_from_env_or_script`` so the shot
    label baked into the wire payload matches what the addon would
    otherwise compute itself. Used for default ``read_path`` /
    ``write_path`` synthesis on the MCP side.
    """
    shot = os.environ.get("SS_SHOT")
    if shot:
        return shot
    override = os.environ.get("NUKE_MCP_SS_SHOT")
    if override:
        return override
    return "unknown"


def _default_render_path(shot: str, suffix: str = "") -> str:
    """Compose the canonical ``$SS/renders/<shot>[<suffix>]/<shot>.####.exr`` path.

    ``suffix`` lets the per-tool wrappers spell out variants like
    ``_blood`` or ``_dust`` so the FX layer renders land beside the main
    beauty render rather than on top of it.
    """
    root = _resolve_ss_root()
    leaf = f"{shot}{suffix}"
    return str(root / "renders" / leaf / "v001" / f"{shot}.####.exr")


def _default_model_path(shot: str) -> str:
    """``$SS/comp/models/dehaze_<shot>_v001.cat``."""
    root = _resolve_ss_root()
    return str(root / "comp" / "models" / f"dehaze_{shot}_v001.cat")


def _default_paint_cache_root(shot: str) -> str:
    """``$SS/comp/paint_cache/<shot>``."""
    root = _resolve_ss_root()
    return str(root / "comp" / "paint_cache" / shot)


def _default_stmap_root(shot: str) -> str:
    """``$SS/comp/stmaps/<shot>``."""
    root = _resolve_ss_root()
    return str(root / "comp" / "stmaps" / shot)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="salt_spill", annotations=BENIGN_NEW)
    @nuke_command("setup_karma_aov_pipeline_ss")
    def setup_karma_aov_pipeline_ss(
        read_path: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Build the Karma AOV split-and-rebuild pipeline with Salt Spill defaults.

        Composes :func:`setup_karma_aov_pipeline` (C3) under a
        ``KarmaAOV_<shot>`` Group plus a Backdrop labelled ``<shot> #
        C8 v1``. ``read_path`` defaults to
        ``$SS/renders/<shot>/v001/<shot>.####.exr``; supply an explicit
        path when the shot's beauty render is in a non-canonical
        location.

        Args:
            read_path: explicit Karma EXR path. Defaults to the canonical
                Salt Spill render layout under ``$SS/renders/<shot>/``.
            name: explicit Group name. Idempotent re-call key.

        Returns:
            Wrapper Group's ``NodeRef`` plus ``backdrop`` (Backdrop name)
            and ``layers`` / ``unknown_layers`` from the inner C3 build.
        """
        shot = _resolve_shot()
        params: dict[str, Any] = {
            "read_path": read_path or _default_render_path(shot),
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("setup_karma_aov_pipeline_ss", params, "mutate")

    @nuke_tool(ctx, profile="salt_spill", annotations=BENIGN_NEW)
    @nuke_command("setup_flip_blood_comp_ss")
    def setup_flip_blood_comp_ss(
        beauty: str,
        deep_pass: str,
        motion: str | None = None,
        holdout_roto: str | None = None,
        blood_tint: tuple[float, float, float] = (0.35, 0.02, 0.04),
        write_path: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Wrap the C6 FLIP-blood macro under a ``FLIP_Blood_<shot>`` Group.

        Composes :func:`setup_flip_blood_comp` (C6) -- DeepRecolor +
        DeepHoldout + DeepMerge + DeepToImage + Grade(``blood_tint``) +
        ZDefocus -- and stamps a Backdrop labelled ``<shot> # C8 v1``.
        Adds a Salt-Spill-defaulted Write path
        (``$SS/renders/<shot>_blood/v001/<shot>.####.exr``).

        Args:
            beauty: 2D Read fed to DeepRecolor's colour input.
            deep_pass: deep render Read.
            motion: optional motion-vector source. ``None`` -> no
                VectorBlur is added.
            holdout_roto: optional Roto holdout. ``None`` -> no-op
                holdout (DeepHoldout subject == holdout).
            blood_tint: RGB multiply on the Grade. Defaults to the
                FMP-shoot reference triple.
            write_path: explicit final Write path. Defaults to
                ``$SS/renders/<shot>_blood/...``.
            name: explicit Group name. Idempotent re-call key.
        """
        shot = _resolve_shot()
        params: dict[str, Any] = {
            "beauty": beauty,
            "deep_pass": deep_pass,
            "blood_tint": list(blood_tint),
            "write_path": write_path or _default_render_path(shot, "_blood"),
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }
        if motion is not None:
            params["motion"] = motion
        if holdout_roto is not None:
            params["holdout_roto"] = holdout_roto
        if name is not None:
            params["name"] = name
        return run_on_main("setup_flip_blood_comp_ss", params, "mutate")

    @nuke_tool(ctx, profile="salt_spill", annotations=BENIGN_NEW)
    @nuke_command("setup_sand_dust_layer")
    def setup_sand_dust_layer(
        beauty: str,
        deep_pass: str,
        motion: str | None = None,
        write_path: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Sand/dust FX layer wrapped under a ``SandDust_<shot>`` Group.

        Reuses the FLIP-blood macro's deep-comp shape -- DeepRecolor +
        DeepHoldout + flatten + tint + optional VectorBlur -- but with a
        sand-coloured tint default and a different write target
        (``$SS/renders/<shot>_dust/...``). Stamps a Backdrop labelled
        ``<shot> # C8 v1``.

        The sand tint is a desaturated warm yellow-grey
        (``(0.78, 0.62, 0.41)``) that matches the FMP-reference colour
        for desert-sand particulates illuminated by the spaceship
        landing lights.

        Args:
            beauty: 2D Read for the sand FX colour pass.
            deep_pass: deep render Read for the sand sim.
            motion: optional motion-vector source.
            write_path: explicit Write path. Defaults to
                ``$SS/renders/<shot>_dust/...``.
            name: explicit Group name. Idempotent re-call key.
        """
        shot = _resolve_shot()
        params: dict[str, Any] = {
            "beauty": beauty,
            "deep_pass": deep_pass,
            "tint": [0.78, 0.62, 0.41],
            "write_path": write_path or _default_render_path(shot, "_dust"),
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }
        if motion is not None:
            params["motion"] = motion
        if name is not None:
            params["name"] = name
        return run_on_main("setup_sand_dust_layer", params, "mutate")

    @nuke_tool(ctx, profile="salt_spill", annotations=BENIGN_NEW)
    @nuke_command("setup_salt_structure_relight")
    def setup_salt_structure_relight(
        beauty: str,
        normal_pass: str,
        position_pass: str,
        light_position: tuple[float, float, float] = (0.0, 100.0, 0.0),
        light_color: tuple[float, float, float] = (1.0, 0.92, 0.78),
        name: str | None = None,
    ) -> dict:
        """Relight the salt structures via a Relight + AOV recombine pass.

        Composes :func:`setup_karma_aov_pipeline` (C3) so the normal /
        position passes are already split out, then drops a Relight
        node fed by the AOV branch and merges the relit output back
        over the beauty. Wrapped in a ``SaltRelight_<shot>`` Group with
        a Backdrop labelled ``<shot> # C8 v1``.

        Args:
            beauty: original beauty pass.
            normal_pass: normal Read or upstream node.
            position_pass: world-position Read.
            light_position: 3-tuple of light XYZ.
            light_color: 3-tuple of light RGB.
            name: explicit Group name. Idempotent re-call key.
        """
        shot = _resolve_shot()
        params: dict[str, Any] = {
            "beauty": beauty,
            "normal_pass": normal_pass,
            "position_pass": position_pass,
            "light_position": list(light_position),
            "light_color": list(light_color),
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("setup_salt_structure_relight", params, "mutate")

    @nuke_tool(ctx, profile="salt_spill", annotations=BENIGN_NEW)
    @nuke_command("setup_dehaze_copycat_ss")
    def setup_dehaze_copycat_ss(
        haze_exemplars: list[str],
        clean_exemplars: list[str],
        epochs: int = 8000,
        model_path: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Train a Salt-Spill-defaulted dehaze CopyCat. Returns task_id.

        Composes :func:`setup_dehaze_copycat` (C7) under a
        ``Dehaze_<shot>`` Group with the model output pinned to
        ``$SS/comp/models/dehaze_<shot>_v001.cat`` by default. Stamps a
        Backdrop labelled ``<shot> # C8 v1``.

        Async: training runs as an MCP Task -- the call returns
        immediately with ``{task_id, state, ack, group, backdrop}``;
        progress flows through ``tasks_get(task_id)``.

        Args:
            haze_exemplars: hazy plate paths.
            clean_exemplars: clean plate paths (pairwise with
                ``haze_exemplars``).
            epochs: training epochs. Default 8000 matches C7's
                converged-dehaze setting.
            model_path: explicit ``.cat`` output path. Defaults to
                ``$SS/comp/models/dehaze_<shot>_v001.cat``.
            name: explicit Group name. Idempotent re-call key.
        """
        shot = _resolve_shot()
        params: dict[str, Any] = {
            "haze_exemplars": list(haze_exemplars),
            "clean_exemplars": list(clean_exemplars),
            "epochs": int(epochs),
            "model_path": model_path or _default_model_path(shot),
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("setup_dehaze_copycat_ss", params, "mutate")

    @nuke_tool(ctx, profile="salt_spill", annotations=BENIGN_NEW)
    @nuke_command("setup_smartvector_paint_propagate_ss")
    def setup_smartvector_paint_propagate_ss(
        plate: str,
        paint_frame: int,
        range_in: int,
        range_out: int,
        cache_root: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Propagate a paint via SmartVectors + bake to a paint cache.

        Composes :func:`apply_smartvector_propagate` (C4) under a
        ``PaintProp_<shot>`` Group with the cache root pinned to
        ``$SS/comp/paint_cache/<shot>/`` by default. Stamps a Backdrop
        labelled ``<shot> # C8 v1``.

        Args:
            plate: source plate (Read or upstream comp node).
            paint_frame: frame the paint reference lives on.
            range_in: first frame to propagate to.
            range_out: last frame to propagate to.
            cache_root: explicit paint-cache root. Defaults to
                ``$SS/comp/paint_cache/<shot>/``.
            name: explicit Group name. Idempotent re-call key.
        """
        shot = _resolve_shot()
        params: dict[str, Any] = {
            "plate": plate,
            "paint_frame": int(paint_frame),
            "range_in": int(range_in),
            "range_out": int(range_out),
            "cache_root": cache_root or _default_paint_cache_root(shot),
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("setup_smartvector_paint_propagate_ss", params, "mutate")

    @nuke_tool(ctx, profile="salt_spill", annotations=BENIGN_NEW)
    @nuke_command("setup_spaceship_track_patch_ss")
    def setup_spaceship_track_patch_ss(
        plate: str,
        ref_frame: int,
        surface_type: Literal["planar", "3d"] = "planar",
        patch_source: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Build the spaceship-patch graph wrapped under a ``SpaceshipPatch_<shot>`` Group.

        Composes :func:`setup_spaceship_track_patch` (C5) and stamps a
        Backdrop labelled ``<shot> # C8 v1``. The C5 macro handles the
        actual planar vs. 3D-camera branching internally.

        Args:
            plate: source plate node.
            ref_frame: reference frame for tracker / camera bake.
            surface_type: ``"planar"`` or ``"3d"``.
            patch_source: optional patch-source node.
            name: explicit Group name. Idempotent re-call key.
        """
        shot = _resolve_shot()
        params: dict[str, Any] = {
            "plate": plate,
            "ref_frame": int(ref_frame),
            "surface_type": surface_type,
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }
        if patch_source is not None:
            params["patch_source"] = patch_source
        if name is not None:
            params["name"] = name
        return run_on_main("setup_spaceship_track_patch_ss", params, "render")

    @nuke_tool(ctx, profile="salt_spill", annotations=BENIGN_NEW)
    @nuke_command("setup_scream_shot_lensflare")
    def setup_scream_shot_lensflare(
        beauty: str,
        flare_intensity: float = 1.6,
        flare_color: tuple[float, float, float] = (1.0, 0.78, 0.55),
        name: str | None = None,
    ) -> dict:
        """Build the Scream-shot lensflare envelope under a ``ScreamFlare_<shot>`` Group.

        Composes a lensflare graph: Glow on the beauty highlights ->
        Flare driven by a tracked anchor -> Merge over the beauty,
        sandwiched in an ACEScct OCIOColorSpace pair (via the C2
        :func:`convert_node_colorspace` primitive) so the flare
        colour multiply runs in the correct working space. Wrapped
        in a Group with a Backdrop labelled ``<shot> # C8 v1``.

        Args:
            beauty: source beauty stream the flare lands over.
            flare_intensity: scalar multiply applied at the flare's
                Grade. Default ``1.6`` matches the FMP reference.
            flare_color: 3-tuple flare RGB.
            name: explicit Group name. Idempotent re-call key.
        """
        shot = _resolve_shot()
        params: dict[str, Any] = {
            "beauty": beauty,
            "flare_intensity": float(flare_intensity),
            "flare_color": list(flare_color),
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("setup_scream_shot_lensflare", params, "mutate")

    @nuke_tool(ctx, profile="salt_spill", annotations=READ_ONLY)
    @nuke_command("audit_comp_for_acescct_consistency_ss")
    def audit_comp_for_acescct_consistency_ss(
        prefix: str = "ss_",
        expected_fps: float = 24.0,
        expected_format: str = "2048x1080",
        strict: bool = True,
    ) -> dict:
        """Run the Salt-Spill comp audit triple and return a unified findings list.

        READ_ONLY composition of three C-phase audits:

        * :func:`audit_acescct_consistency` (C2) -- colour-space drift.
        * :func:`audit_render_settings` (C9) -- root-level fps / format.
        * :func:`audit_naming_convention` (C9) -- ``ss_`` prefix
          enforcement.

        Each finding gets a ``source`` field (``"color"`` / ``"render"`` /
        ``"naming"``) so the operator can filter by audit origin. The
        macro never mutates the graph and never calls a builder.

        Args:
            prefix: required leading substring on every node name. Forwarded
                to ``audit_naming_convention``. Default ``"ss_"`` (Salt Spill).
            expected_fps: expected script fps. Forwarded to
                ``audit_render_settings``.
            expected_format: expected script format. Forwarded to
                ``audit_render_settings``.
            strict: forwarded to the C2 ``audit_acescct_consistency``
                heuristic gate.

        Returns:
            ``{findings: list[AuditFinding], sources: list[str]}`` --
            the findings list aggregates entries from all three audits
            with each augmented by a ``source`` key.
        """
        params: dict[str, Any] = {
            "prefix": prefix,
            "expected_fps": expected_fps,
            "expected_format": expected_format,
            "strict": strict,
            "shot": _resolve_shot(),
            "tool_version": C8_TOOL_VERSION,
        }
        return run_on_main("audit_comp_for_acescct_consistency_ss", params, "read")

    @nuke_tool(ctx, profile="salt_spill", annotations=BENIGN_NEW)
    @nuke_command("bake_lens_distortion_envelope_ss")
    def bake_lens_distortion_envelope_ss(
        plate: str,
        lens_solve: str,
        write_path: str | None = None,
        stmap_root: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Bake the lens-distortion linear-comp envelope with Salt Spill defaults.

        Composes :func:`bake_lens_distortion_envelope` (C4) and stamps a
        Backdrop labelled ``<shot> # C8 v1``. Pins the STMap cache root
        to ``$SS/comp/stmaps/<shot>/`` so the per-shot caches don't
        collide across shots in the production tree.

        Args:
            plate: source Read node name.
            lens_solve: existing LensDistortion (or LD_3DE_*) node.
            write_path: explicit final Write path. ``None`` -> the
                addon synthesises a path next to the script.
            stmap_root: explicit STMap cache directory. Defaults to
                ``$SS/comp/stmaps/<shot>/``.
            name: explicit NetworkBox name. Idempotent re-call key.
        """
        shot = _resolve_shot()
        params: dict[str, Any] = {
            "plate": plate,
            "lens_solve": lens_solve,
            "stmap_root": stmap_root or _default_stmap_root(shot),
            "shot": shot,
            "tool_version": C8_TOOL_VERSION,
        }
        if write_path is not None:
            params["write_path"] = write_path
        if name is not None:
            params["name"] = name
        return run_on_main("bake_lens_distortion_envelope_ss", params, "mutate")

"""Deep-comp workflow macros.

C6 layers a higher-level workflow tool on top of the C1 atomic deep
primitives in :mod:`nuke_mcp.tools.deep`. The macro lives in its own
module so the "primitives vs. recipe" boundary stays visible in the
file tree -- ``deep.py`` is single-node create operations,
``deep_workflow.py`` is opinionated multi-node pipelines that compose
those primitives.

Module placement decision: separate file (vs. extending ``deep.py``).
Rationale -- the C1 primitives have a tight one-tool / one-handler
shape with simple fixtures. The workflow macro orchestrates ~6-7
sub-handlers, threads ZDefocus deep-knob constraints, and wraps a
Group; mixing it into ``deep.py`` would bloat that module and blur the
"single-node atom" contract every primitive there honours.

Idempotency: ``setup_flip_blood_comp`` is BENIGN_NEW, idempotent on
``name=`` (the addon orchestrator forwards the same ``name`` to each
sub-handler so a re-call returns the existing nodes rather than
duplicating them).
"""

from __future__ import annotations

from nuke_mcp.annotations import BENIGN_NEW
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="deep", annotations=BENIGN_NEW)
    @nuke_command("setup_flip_blood_comp")
    def setup_flip_blood_comp(
        beauty: str,
        deep_pass: str,
        motion: str | None = None,
        holdout_roto: str | None = None,
        blood_tint: tuple[float, float, float] = (0.35, 0.02, 0.04),
        name: str | None = None,
    ) -> dict:
        """Build a FLIP-blood deep-comp pipeline wrapped in a Group.

        Composes the C1 deep primitives into the standard workflow:

            DeepRead(``deep_pass``) -> DeepRecolor(``beauty``) ->
            DeepHoldout(against ``holdout_roto`` when supplied) ->
            DeepMerge over BG -> DeepToImage -> Grade(``blood_tint``)
            -> [VectorBlur(``motion``) when supplied] ->
            ZDefocus(math=depth, depth=deep.front, AA-on-depth disabled).

        The ZDefocus knob trio is hardcoded -- ``math=depth`` plus
        ``depth=deep.front`` plus AA-on-depth off is the Foundry rule
        for sampling the front-most deep depth value without spatial
        anti-aliasing (which would produce false intermediate Z values
        and bleed across silhouettes). Callers don't get to override.

        The Grade is wrapped in an ACEScct OCIOColorSpace pair so the
        ``blood_tint`` multiply runs in the correct working space.

        Args:
            beauty: 2D Read / colour node feeding ``DeepRecolor``'s
                colour input (slot 1). Replaces the deep stream's RGB
                while preserving sample depth.
            deep_pass: deep render Read (DeepRead). Slot 0 of the
                ``DeepRecolor``.
            motion: optional motion-vector source node fed to a
                ``VectorBlur`` after the flatten. ``None`` -> no
                motion-blur node is created.
            holdout_roto: optional Roto/RotoPaint mask. When supplied,
                wired into the holdout side of ``DeepHoldout``. ``None``
                falls back to the upstream beauty pass as the holdout.
            blood_tint: RGB multiply applied at the Grade. Default is a
                desaturated dark-red (``(0.35, 0.02, 0.04)``) matching
                FMP-shoot reference.
            name: explicit Group node name. When supplied AND a Group
                of the same name already exists, the orchestrator is
                idempotent: every sub-handler short-circuits on its
                own ``name=`` and the Group itself is returned in
                place. ``None`` -> the Group is auto-named
                ``FLIP_Blood_<shot>`` from ``$SS_SHOT`` (or the script
                stem).

        Returns:
            ``{group, recolor, holdout, merge, flatten, grade,
            vector_blur, zdefocus}`` -- node names of every member of
            the macro. ``vector_blur`` is ``None`` when ``motion`` was
            not supplied.
        """
        params: dict = {
            "beauty": beauty,
            "deep_pass": deep_pass,
            "blood_tint": list(blood_tint),
        }
        if motion is not None:
            params["motion"] = motion
        if holdout_roto is not None:
            params["holdout_roto"] = holdout_roto
        if name is not None:
            params["name"] = name
        return run_on_main("setup_flip_blood_comp", params, "mutate")

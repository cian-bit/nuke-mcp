"""Tracking workflow macros (Phase C5).

C5 sits on top of the C1 atomic primitives in ``tracking.py``. Where
C1 exposes single-node operations (CameraTracker, PlanarTracker, bake
to CornerPin, solve, bake to Card), C5 ships *workflow macros* that
compose those primitives into the multi-node graph a comp artist
actually wires by hand for a given task.

This module currently ships one macro:

* ``setup_spaceship_track_patch(plate, ref_frame, surface_type=...)``
  -- builds either a planar-track patch (screens / labels / decals)
  or a 3D-camera projection patch (hull / panels) wrapped in a Group
  named ``SpaceshipPatch_<shot>``.

Why a separate module instead of extending ``tracking.py``?

* C1 in ``tracking.py`` is *atomic*: each tool maps 1:1 to a single
  Nuke node. The decorator-stack and idempotency contract in that
  file is uniform across every tool.
* C5 here is *macro*: one tool call produces a 5-7 node graph wrapped
  in a Group. Mixing those two shapes in the same file would force
  readers to context-switch between "thin dispatch primitive" and
  "composed workflow" on every function. Splitting keeps each
  module's responsibility crisp and gives later C5+ macros (e.g. a
  cleanup-and-retrack pass) somewhere obvious to land.

The wire payload mirrors C1: a small ``params`` dict is forwarded via
``run_on_main`` to a typed addon-side handler that does the actual
node creation. The handler composes the C1 sub-handlers internally;
this tool does NOT reimplement tracker creation.
"""

from __future__ import annotations

from typing import Literal

from nuke_mcp.annotations import BENIGN_NEW
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="tracking", annotations=BENIGN_NEW)
    @nuke_command("setup_spaceship_track_patch")
    def setup_spaceship_track_patch(
        plate: str,
        ref_frame: int,
        surface_type: Literal["planar", "3d"] = "planar",
        patch_source: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Build a tracked patch graph wrapped in a ``SpaceshipPatch_<shot>`` Group.

        Decision tree:

        * ``surface_type="planar"`` (screens / labels / decals): create
          a Roto plane, drive a PlanarTracker off it, bake to a
          CornerPin2D, then run a RotoPaint clone (or the supplied
          ``patch_source`` if provided) and a second CornerPin to
          restore perspective.
        * ``surface_type="3d"`` (hull / panels / surface curvature):
          create a CameraTracker, solve it, bake to a Card3D, then
          stack Project3D + ScanlineRender + Merge so the patch
          re-projects through the solved camera.

        The Group name is ``SpaceshipPatch_<shot>`` where ``<shot>`` is
        derived from ``$SS_SHOT`` if set, otherwise from the script
        path's stem. Idempotent on ``name=`` -- a re-call with the
        same explicit ``name`` returns the existing Group rather than
        creating a duplicate.

        Args:
            plate: source plate node (Read or upstream comp node).
            ref_frame: reference frame for the tracker / camera bake.
            surface_type: ``"planar"`` for flat surfaces, ``"3d"`` for
                curved hull patches.
            patch_source: optional node name that supplies the patch
                paint over the matched perspective. When ``None`` the
                planar branch ships a default RotoPaint clone; the 3d
                branch will scaffold a default RotoPaint upstream of
                the Project3D when no source is supplied.
            name: explicit Group name. When supplied AND a Group of
                that name already exists, the existing Group's
                ``NodeRef`` is returned (idempotent re-call).
        """
        params: dict = {
            "plate": plate,
            "ref_frame": int(ref_frame),
            "surface_type": surface_type,
        }
        if patch_source is not None:
            params["patch_source"] = patch_source
        if name is not None:
            params["name"] = name
        # ``render`` timeout class: solving the 3D camera leg in the
        # macro can run several seconds in real Nuke. Stay on the
        # higher-budget class so the MCP layer doesn't kill the call.
        return run_on_main("setup_spaceship_track_patch", params, "render")

"""Tracking workflow primitives.

C1 atomic primitives for camera-tracker, planar tracker, Tracker4, and
the bake operations that feed downstream comp / 3D workflows.

Every tool is a thin dispatch to a typed addon handler via
``run_on_main``. The wire payload is a small ``params`` dict; the
addon-side handler runs on Nuke's main thread, validates inputs,
short-circuits on idempotent re-calls (``name=`` kwarg matched against
existing class+inputs+name), and returns a flat ``NodeRef`` dict
(``{name, type, x, y, inputs}``).

Idempotency contract: when ``name=`` is supplied AND a node of the same
class with matching inputs already exists at that name, the addon
returns the existing ``NodeRef`` instead of creating a duplicate. When
``name`` is ``None``, calls ARE NOT idempotent -- Nuke names auto-uniquify
so a second call yields a fresh ``Foo2``.
"""

from __future__ import annotations

from nuke_mcp.annotations import BENIGN_NEW
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("setup_camera_tracker")
    def setup_camera_tracker(
        input_node: str,
        features: int = 300,
        solve_method: str = "Match-Move",
        mask: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Create a CameraTracker node connected to ``input_node``.

        Args:
            input_node: source plate (Read or upstream comp node).
            features: target feature count for the tracker.
            solve_method: solve mode. One of Match-Move, Tripod, etc.
            mask: optional matte node fed into the tracker mask input.
            name: explicit node name. When supplied AND a CameraTracker
                of that name already exists with the same inputs, the
                existing node is returned (idempotent re-call).
        """
        params: dict = {
            "input_node": input_node,
            "features": features,
            "solve_method": solve_method,
        }
        if mask is not None:
            params["mask"] = mask
        if name is not None:
            params["name"] = name
        return run_on_main("setup_camera_tracker", params, "mutate")

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("setup_planar_tracker")
    def setup_planar_tracker(
        input_node: str,
        plane_roto: str,
        ref_frame: int = 1,
        name: str | None = None,
    ) -> dict:
        """Create a PlanarTracker driven by a Roto plane and reference frame.

        Args:
            input_node: source plate.
            plane_roto: Roto/RotoPaint node defining the tracked plane.
            ref_frame: reference frame for the tracker.
            name: idempotent re-call key (see ``setup_camera_tracker``).
        """
        params: dict = {
            "input_node": input_node,
            "plane_roto": plane_roto,
            "ref_frame": ref_frame,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("setup_planar_tracker", params, "mutate")

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("setup_tracker4")
    def setup_tracker4(
        input_node: str,
        num_tracks: int = 4,
        name: str | None = None,
    ) -> dict:
        """Create a Tracker4 with ``num_tracks`` track slots seeded.

        Args:
            input_node: source plate.
            num_tracks: number of track slots to enable.
            name: idempotent re-call key.
        """
        params: dict = {
            "input_node": input_node,
            "num_tracks": num_tracks,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("setup_tracker4", params, "mutate")

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("bake_tracker_to_corner_pin")
    def bake_tracker_to_corner_pin(
        tracker_node: str,
        ref_frame: int = 1,
        name: str | None = None,
    ) -> dict:
        """Bake a Tracker4/PlanarTracker into a CornerPin2D node.

        Args:
            tracker_node: existing tracker node to bake.
            ref_frame: reference frame to bake against.
            name: idempotent re-call key.
        """
        params: dict = {
            "tracker_node": tracker_node,
            "ref_frame": ref_frame,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("bake_tracker_to_corner_pin", params, "mutate")

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("solve_3d_camera")
    def solve_3d_camera(
        camera_tracker_node: str,
        name: str | None = None,
    ) -> dict:
        """Solve the 3D camera on an existing CameraTracker node.

        Args:
            camera_tracker_node: CameraTracker node to solve.
            name: idempotent re-call key. Solving is expensive in Nuke
                so re-calling on the same name with no upstream changes
                is a no-op and returns the existing solve handle.
        """
        params: dict = {"camera_tracker_node": camera_tracker_node}
        if name is not None:
            params["name"] = name
        # solving can take a while in Nuke -- bump the timeout class.
        return run_on_main("solve_3d_camera", params, "render")

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("bake_camera_to_card")
    def bake_camera_to_card(
        camera_node: str,
        frame: int = 1,
        name: str | None = None,
    ) -> dict:
        """Bake a solved Camera (or CameraTracker) to a Card3D at ``frame``.

        Args:
            camera_node: solved Camera or CameraTracker node.
            frame: frame to bake the card at.
            name: idempotent re-call key.
        """
        params: dict = {
            "camera_node": camera_node,
            "frame": frame,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("bake_camera_to_card", params, "mutate")

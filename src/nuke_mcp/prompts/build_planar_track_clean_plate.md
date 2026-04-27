---
name: build_planar_track_clean_plate
description: Walks through a planar track + clean-plate procedural so a single painted patch can be projected onto a tracked surface across the shot.
arguments:
  - name: plate
    description: Read node holding the source plate.
    required: true
  - name: roi_description
    description: Plain-language description of the planar surface the user wants tracked (e.g. "the wall behind the actor").
    required: true
  - name: paint_frame
    description: Frame to paint the clean-plate patch on. Empty string defaults to the script's first frame.
    required: false
---
# Build planar track + clean plate

Track the planar surface described as `{roi_description}` on `{plate}` and project a single clean-plate patch painted on frame `{paint_frame}` onto that surface across the whole shot.

This is the C1 `setup_planar_tracker` + `bake_tracker_to_corner_pin` workflow. Planar tracking is the right tool for any flat or near-flat surface that needs paint, sign replacement, or rig removal. For non-planar surfaces use `setup_camera_tracker` instead (see `build_3d_camera_track_project`).

## Step 1 — Drop the planar tracker

Call `setup_planar_tracker` with:

- `plate="{plate}"`
- `description="{roi_description}"` (this seeds the user-visible label on the tracker; the actual ROI is set by the user in the planar-track UI).

The tool drops a PlanarTracker node downstream of `{plate}` and selects the four corner pins. Tell the user: "Step to a frame where `{roi_description}` is fully visible, draw the planar ROI, and run track forward + backward."

The track itself is a manual UI step — there is no `start_track` MCP tool because PlanarTracker's track button is a Python-internal callback that runs on the main thread. Surface this clearly to the user.

## Step 2 — Verify the track

Once the user has tracked, call `list_keyframes` on the PlanarTracker's `transform.translate` knob (or on the four corner-pin knobs if planar mode is set to corner pins). Confirm:

- Keyframes exist across the full frame range.
- No catastrophic frame-to-frame jumps (bad tracks usually pop > 10 px in a single frame).
- The track converged on every frame (the tracker's `tracked` flag is True for the full range).

If the track is bad, suggest the user re-track with a tighter ROI or a different reference frame. Do not auto-rerun the track.

## Step 3 — Bake to a CornerPin

Call `bake_tracker_to_corner_pin` with `tracker="<PlanarTracker name>"`. The tool reads the tracker's per-frame corner-pin output and bakes it to a stand-alone CornerPin2D node so the rest of the chain doesn't depend on the (slow) PlanarTracker re-evaluating on every frame.

The baked CornerPin has:

- `to1`, `to2`, `to3`, `to4` knobs animated per-frame from the tracker.
- `from1`..`from4` set to the planar ROI on the reference frame `{paint_frame}`.

## Step 4 — Author the clean-plate patch

Tell the user: "Step to frame `{paint_frame}`, paint or roto-patch the clean plate over `{roi_description}`."

Wait for the user. The painted patch should live in plate space (not pre-warped) — the CornerPin pushes it into the tracked surface space.

## Step 5 — Wire the projection

Wire:

```
{plate} ---+
           |
       Paint -- FrameHold({paint_frame}) -- CornerPin2D -- Merge(over) --> output
                                                              |
                                                              +-- Roto (alpha for the patch)
```

The FrameHold pins the painted patch to `{paint_frame}` so the CornerPin pulls from a stable source; the CornerPin then warps the patch onto the tracked surface frame by frame; the Roto provides a soft alpha so the patch blends rather than hard-edges.

## Step 6 — QC the projection

Drop `qc_viewer_pair` between `{plate}` and the projected result. Scrub the full range. The patch should sit on the tracked surface without sliding. Common drift causes:

- Track lost lock briefly — re-track with a tighter ROI.
- Surface isn't actually planar (e.g. crumpled fabric). Use `setup_camera_tracker` + project onto geo instead.
- Parallax against background (the surface is in front of something at a different depth). Add a Roto holdout for the foreground occluders.

## Tools referenced

- `setup_planar_tracker` — drop PlanarTracker on plate.
- `list_keyframes` — track verification.
- `bake_tracker_to_corner_pin` — bake tracker to standalone CornerPin2D.
- `qc_viewer_pair` — drift verification.

## When to stop

Stop after Step 5 once the projection is wired and the user has the patch in place. Step 6 is mandatory before any delivery — a tracked patch that drifts at the edge of the range is a bug, not a finish. Do not auto-render — `render_frames` is destructive and requires `confirm=True`.

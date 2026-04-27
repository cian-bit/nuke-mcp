---
name: build_3d_camera_track_project
description: Walks through a 3D camera track, scene-solve, and projection-onto-card workflow for matchmove or set-extension comp work.
arguments:
  - name: plate
    description: Read node holding the plate to camera-track.
    required: true
  - name: focal_hint
    description: Optional focal-length hint in mm (e.g. "35"). Empty string lets the solver auto-detect.
    required: false
  - name: project_node
    description: Node whose output should be projected onto cards in the solved scene (e.g. a paint patch or a CG element). Empty string skips the projection step.
    required: false
---
# Build 3D camera track + project

Camera-track `{plate}`, solve the scene, and (optionally) project `{project_node}` onto a card placed in the solved 3D space. Focal hint: `{focal_hint}` mm.

This is the C1 `setup_camera_tracker` + `solve_3d_camera` + `bake_camera_to_card` workflow. Use this for non-planar surfaces, set extension, particles in 3D, or any time a planar track isn't enough. For flat surfaces, prefer `build_planar_track_clean_plate` — it's faster and more stable.

## Step 1 — Drop the CameraTracker

Call `setup_camera_tracker` with:

- `plate="{plate}"`
- `focal_hint="{focal_hint}"` (the tool sets the camera-tracker's `focalLengthType` to "Known" when a hint is supplied, otherwise leaves it on "Unknown").

The tool drops a CameraTracker node downstream of `{plate}` and configures sensible defaults: `numberOfFeatures=300`, `previewFeatures=True`, `maskSource=Source Alpha` if `{plate}` has an alpha channel.

Tell the user: "Run `Track Features` then `Solve Camera` from the CameraTracker UI. The track step is a manual main-thread operation."

## Step 2 — Solve the scene

Once features are tracked, call `solve_3d_camera` with `tracker="<CameraTracker name>"`. The tool runs the solve and returns:

- `solve_error_avg` — the per-frame mean reprojection error (target < 1 px for a clean shot).
- `solve_error_max` — worst-frame error (target < 3 px; spikes above 5 mean the solve has a bad frame).
- `inlier_count` — number of features that survived the solve.

If `solve_error_avg > 1.0`, surface this to the user — the projection will drift visibly. Common fixes:

- Increase `numberOfFeatures` (more features -> more constraints).
- Mask out moving foreground (CameraTracker assumes a static scene).
- Trim the frame range to the part that solves cleanly and use a separate solve for the rest.

## Step 3 — Bake camera + scene

Call `bake_camera_to_card` with:

- `tracker="<CameraTracker name>"`
- `card_at="<a frame where the projection surface is well-resolved>"` (defaults to the first frame).

The tool extracts:

```
Camera (animated translate/rotate/focal)
ScanlineRender
Card (placed at the user-pointed feature in 3D)
```

The Card is positioned at the 3D point under the cursor on the chosen frame and oriented to face the camera at that frame. Tell the user: "Adjust the Card's translate / rotate / scale to match the surface you want to project onto."

## Step 4 — Wire the projection (optional)

If `{project_node}` is non-empty, wire:

```
{project_node} -- Project3D (camera = baked Camera) --> Card.img --> ScanlineRender --> output
```

Project3D pushes `{project_node}`'s pixels through the baked Camera onto the Card geometry. The ScanlineRender renders the Card from the same baked Camera back to 2D — net result: `{project_node}` sticks to the tracked surface in 3D.

If `{project_node}` is empty, skip this step — the user has a solved scene and Card and can wire their own projection.

## Step 5 — QC the projection

Drop `qc_viewer_pair` between `{plate}` and the ScanlineRender output. Scrub the full range. The projected element should sit on the tracked surface without sliding or scaling oddly. Common drift causes:

- Card position is slightly off the actual surface — adjust translate/rotate.
- Solve has a per-frame error spike — re-solve or split the range.
- `{project_node}` was painted in a different colorspace than the plate — match colorspaces before projecting.
- ScanlineRender's `samples` is at default 1 — bump to 4 or 8 for clean edges on the projected card.

## Step 6 — Optional: cache the ScanlineRender

If the projection is heavy (large card, multiple light passes), drop a Write node downstream and cache the projected pass to scene-linear EXR. Use `setup_write` with `format="exr"`, `compression="zip1"`, `colorspace="ACES - ACEScg"`. Subsequent comp passes work off the cached projection rather than re-rendering.

## Tools referenced

- `setup_camera_tracker` — drop CameraTracker on plate.
- `solve_3d_camera` — run solve and return errors.
- `bake_camera_to_card` — extract camera + ScanlineRender + Card.
- `qc_viewer_pair` — drift verification.
- `setup_write` — projection cache.

## Hard rules

- Do not auto-run `Track Features` or `Solve Camera` — both are heavy main-thread operations and may freeze Nuke for minutes. Always make the user trigger them.
- A solve with `solve_error_avg > 2.0` is unusable — say so explicitly and refuse to proceed to projection. Bad solves produce paint that swims; the user is better off with a planar track.
- ScanlineRender is destructive in the render-frames sense (high CPU, large output) — wrap large bakes in the Tasks primitive (B2) when available.

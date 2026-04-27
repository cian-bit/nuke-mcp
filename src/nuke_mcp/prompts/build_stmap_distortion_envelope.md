---
name: build_stmap_distortion_envelope
description: Walks through caching a lens-distortion envelope to STMap so downstream comp work happens in a linear, undistorted plate.
arguments:
  - name: plate
    description: Read node holding the distorted plate.
    required: true
  - name: lens_node
    description: LensDistortion node (or equivalent) that holds the solved lens model.
    required: true
  - name: stmap_path
    description: Output path for the baked STMap EXR. Empty string defaults to "<plate_dir>/stmap_<plate>.####.exr".
    required: false
---
# Build STMap distortion envelope

Cache the lens-distortion + redistortion envelope from `{lens_node}` as STMap pairs at `{stmap_path}`, then wire `{plate}` through the cached STMaps so all comp work happens in a linear undistorted space and only the final result is redistorted.

This is the C4 `bake_lens_distortion_envelope` workflow. STMap caching is the standard "linear comp" pattern — solve once, cache, comp in the middle, redistort at the end. It's massively cheaper than re-evaluating LensDistortion on every viewer scrub.

## Step 1 — Verify the lens solve

Call `read_node_detail` on `{lens_node}` and confirm:

- A solve has been run (the `lensType`, `distortion`, and `direction` knobs are set; raw lens grids exist).
- The solve covers the full frame range of `{plate}`.
- The output mode is set correctly (`undistort` for forward direction, `distort` for redistortion).

If the solve is missing, surface that to the user — STMap caching needs a working lens model upstream.

## Step 2 — Bake undistort + redistort STMaps

Call `bake_lens_distortion_envelope` with:

- `plate="{plate}"`
- `lens="{lens_node}"`
- `out_path="{stmap_path}"`

The tool wires a temporary chain:

```
{lens_node} (direction=undistort)  -> Write (stmap_undistort_####.exr)
{lens_node} (direction=distort)    -> Write (stmap_redistort_####.exr)
```

and triggers a render of both STMap streams across the plate's frame range. The tool returns when both caches exist on disk.

If the frame range is large (> 200 frames), wrap the bake call in the Tasks primitive (B2) so the user can cancel; STMap baking is I/O bound and usually fast but not instant.

## Step 3 — Wire the linear comp envelope

Replace the live LensDistortion path with the cached STMaps. The tool drops:

```
{plate} -- STMap (stmap_undistort) -- [LINEAR COMP HAPPENS HERE] -- STMap (stmap_redistort) -- output
```

The two STMaps are identity envelopes — anything that goes in undistorted comes back out distorted to match the original plate. Verify by wiring the undistort STMap output straight into the redistort STMap and comparing to the original plate via `qc_viewer_pair`. Difference should be at the floor of bilinear-resampling noise (~1e-3).

## Step 4 — Comp in the linear space

All downstream comp work — keying, paint, CG integration, deep-comp — should live between the two STMaps. The benefit:

- Edges are straight, not curved. Track points line up. Roto is sane.
- 3D camera integration projects without lens-warp distortion.
- AOV reconstruction (see `build_aov_relight_pipeline`) works against scene-linear pixels.

Tell the user: "Everything you build below `<undistort_stmap>` and above `<redistort_stmap>` is in the linear lens space. Don't add comp work outside that envelope."

## Step 5 — Verify edges before delivery

Before final render, drop a `qc_viewer_pair` between the original `{plate}` and the round-tripped (undistort -> redistort) result. Edges should match within sub-pixel tolerance. Mismatch means:

- The two STMaps were baked at different resolutions (re-bake).
- The lens solve drifts mid-shot (re-solve or split the shot).
- Anti-aliasing settings differ between the two STMap reads (set `STMap.filter` consistently — `Lanczos4` is the standard).

## Tools referenced

- `read_node_detail` — lens solve inspection.
- `bake_lens_distortion_envelope` — STMap pair render.
- `qc_viewer_pair` — round-trip identity check.
- `setup_write` — final delivery render.

## Hard rules

- Never re-run the bake on top of existing STMap files without confirming with the user — STMaps are slow to regenerate and can be referenced by multiple shots.
- Always check the lens-solve frame range before baking — a partial solve produces partial STMaps and the comp will drift on uncovered frames.
- Keep the STMap output colorspace as `linear` (raw float). Do not bake STMaps in ACEScg — they are coordinate maps, not RGB images.

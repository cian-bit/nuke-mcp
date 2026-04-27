---
name: build_smartvector_paint_propagate
description: Walks through a SmartVector + IDistort paint-propagation chain so a single painted frame can be warped to every frame in the range.
arguments:
  - name: plate
    description: Read node holding the source plate the paint will live on.
    required: true
  - name: paint_frame
    description: Frame number where the user has painted the fix (e.g. 1042).
    required: true
  - name: range_in
    description: First frame to propagate the paint to (inclusive).
    required: false
  - name: range_out
    description: Last frame to propagate the paint to (inclusive).
    required: false
---
# SmartVector paint propagate

Build a SmartVector + IDistort propagation chain so paint authored on frame `{paint_frame}` of `{plate}` warps to every frame in `[{range_in}, {range_out}]`.

This is the C4 `apply_smartvector_propagate` + `apply_idistort` workflow. SmartVector solves a forward + backward motion-vector channel pair from the plate; IDistort uses those vectors to push the paint frame to neighbouring frames.

## Step 1 — Generate motion vectors

Call `apply_smartvector_propagate` with `plate="{plate}"` and frame range `[{range_in}, {range_out}]`. The tool drops a SmartVector node, sets `vectorDetail=0.3` (NukeX default for production speed), `flicker=0.5`, and connects to `{plate}`.

SmartVector is a heavy compute — the tool returns immediately but the actual vector solve runs when the SmartVector node is rendered or its `vectors` knob is sampled. If the user wants the vectors precomputed (recommended for any range > 50 frames), call `render_frames` on the SmartVector with `confirm=True` to bake them to a `.nk_smartvector` cache.

## Step 2 — Author the paint

If the user hasn't painted yet, do not auto-paint. Instead:

1. Call `set_current_frame` to jump to `{paint_frame}`.
2. Surface a Roto / Paint node downstream of `{plate}` and tell the user "Paint your fix on this frame, then call back."

The propagation only works if the paint lives on a single frame.

## Step 3 — Propagate via IDistort

Once the paint exists, call `apply_idistort` with:

- `paint_node="<the paint or roto node>"`
- `vectors="<the SmartVector from step 1>"`
- `reference_frame={paint_frame}`
- `range=[{range_in}, {range_out}]`

The tool wires:

```
{plate} ----> SmartVector --+
                            |
   Paint -- FrameHold({paint_frame}) -- IDistort -- VectorBlur -- Premult --> output
                            |
              vectors knob -+
```

The FrameHold pins the paint to `{paint_frame}` so IDistort always pulls from the painted reference; the per-frame vectors then push that paint forward and backward in time.

## Step 4 — Verify on edge frames

Step the viewer to `{range_in}` and `{range_out}` and check the paint is still glued to the same surface feature it was painted on. Drift means SmartVector lost track somewhere — common causes:

- Motion blur in the plate (set SmartVector `flicker` higher, or pre-deblur).
- Disocclusion (the surface goes off-screen and comes back). Split the range at the disocclusion frame and run two propagations.
- Plate is interlaced (run a Deinterlace upstream of SmartVector).

If drift is local to a few frames, freeze the affected frames with a per-frame Paint patch instead of re-tuning SmartVector.

## Tools referenced

- `apply_smartvector_propagate` — SmartVector node + vector cache.
- `apply_idistort` — paint warp via vectors.
- `set_current_frame` — viewer jump to paint frame.
- `render_frames` (destructive, requires confirm) — bake vectors to cache.

## When to stop

Stop after Step 3 if the user is iterating on the paint. Stop after Step 4 once edge frames look clean. Do not propagate paint over a shot cut — the propagation only makes sense within a continuous take.

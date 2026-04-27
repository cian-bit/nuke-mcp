---
name: build_deep_holdout_chain
description: Walks through a DeepRead -> DeepRecolor -> DeepHoldout(2) -> DeepMerge -> deep_to_image deep compositing chain.
arguments:
  - name: deep_read
    description: Name of the DeepRead node (or path to a deep EXR to read in).
    required: true
  - name: holdout_a
    description: Name of the first 2D layer that should be held out by the deep front-most surface.
    required: true
  - name: holdout_b
    description: Name of the second 2D layer (use empty string to skip the second holdout).
    required: false
---
# Build deep holdout chain

Wire a deep-comp chain that uses front-most-surface depth from `{deep_read}` to hold out the 2D layers `{holdout_a}` and `{holdout_b}` and flatten the result back to image space.

The recipe below uses the C1 deep primitives (`create_deep_read`, `create_deep_recolor`, `create_deep_holdout`, `create_deep_merge`, `create_deep_to_image`) and ends with the optional `setup_flip_blood_comp` macro from C6 when the holdouts are FLIP blood splash layers.

## Step 1 — Read the deep EXR

If `{deep_read}` is already a node, skip. Otherwise call `create_deep_read` with the EXR path. Confirm the deep file has `front` and `back` samples (some Karma deep writes only have `front`, which still works for holdouts but loses depth blending).

## Step 2 — Recolor the deep samples

Call `create_deep_recolor` downstream of `{deep_read}`. This bakes the upstream beauty / colour into deep samples so the eventual `deep_to_image` flattens to a real RGB image rather than coverage-only output. Without this step the holdout works geometrically but the final image is black.

## Step 3 — First holdout

Call `create_deep_holdout` with `deep="{deep_read}_recolor"` and `image="{holdout_a}"`. The tool inserts a DeepHoldout that masks `{holdout_a}` by the deep front-most surface — pixels of `{holdout_a}` that sit behind the deep surface are killed.

## Step 4 — Second holdout (optional)

If `{holdout_b}` is non-empty, call `create_deep_holdout` again with `image="{holdout_b}"` chained off the previous holdout's output. Stack holdouts in render order — the layer closest to camera first.

If `{holdout_b}` is empty, skip this step.

## Step 5 — Merge held-out layers back into deep

Call `create_deep_merge` with the held-out 2D layers as inputs. DeepMerge composites the 2D layers as deep samples at their respective Z values, giving you a single deep stream with everything resolved correctly in depth.

## Step 6 — Flatten to image

Call `create_deep_to_image` on the merged deep stream. The output is a normal 2D image with proper depth-aware compositing — held-out layers behind the deep surface are gone, layers in front of it are visible.

## Step 7 — Optional FLIP blood macro

If `{holdout_a}` or `{holdout_b}` is a FLIP blood-splash render (Salt Spill shot 0170 territory), call `setup_flip_blood_comp` (C6, when present) on the flattened result. It adds the canonical wet-edge/grade/glow stack tuned for the Salt Spill blood look.

## Tools referenced

- `create_deep_read` — DeepRead from EXR.
- `create_deep_recolor` — bake colour into deep samples.
- `create_deep_holdout` — depth-aware 2D holdout.
- `create_deep_merge` — recombine held-out layers as deep samples.
- `create_deep_to_image` — flatten deep stream to RGB.
- `setup_flip_blood_comp` (C6) — FLIP blood-splash post-stack.

## Verification

After Step 6, sample a pixel that sits behind the deep surface and confirm `{holdout_a}`'s contribution is zero there. Sample a pixel in front and confirm it's preserved. If both samples look the same as the un-held input, the deep front samples are missing or the colour-space chain is wrong (DeepRecolor won't bake a 16-bit half-float layer correctly into linear-light deep without the Read node's colorspace set to scene-linear).

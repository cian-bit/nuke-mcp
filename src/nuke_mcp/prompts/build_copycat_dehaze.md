---
name: build_copycat_dehaze
description: Walks through training a CopyCat dehaze model from inverse-pair frames (hazy plate + clean reference).
arguments:
  - name: hazy_read
    description: Read node containing the hazy plate frames (the input the model learns from).
    required: true
  - name: clean_read
    description: Read node containing the matching clean reference frames (the target the model learns to produce).
    required: true
  - name: cache_dir
    description: Directory to write the trained .cat checkpoint to. Empty string defaults to project_directory + "/copycat".
    required: false
---
# Build CopyCat dehaze

Train a NukeX CopyCat dehaze model from inverse-pair frames `{hazy_read}` (input) and `{clean_read}` (target), saving checkpoints to `{cache_dir}`.

This is the C7 `setup_dehaze_copycat` workflow. CopyCat learns the per-pixel transform (here: hazy -> clean) from a small set of paired example frames and then runs as an Inference node on the full plate.

## Step 1 — Verify input pairs

Call `read_node_detail` on both `{hazy_read}` and `{clean_read}`. They must:

- Cover the same frame range (or at least an overlapping subset of paired frames).
- Have matching resolution and bit depth.
- Be in the same colorspace — train in scene-linear `ACES - ACEScg` whenever possible. Float-linear training generalises far better than log or display-referred.

If the pair is unaligned (e.g. the clean reference was hand-painted on a sub-set of frames), the workflow still works — just train on the paired subset, then inference on the full plate.

## Step 2 — Auto-build the training graph

Call `setup_dehaze_copycat` with:

- `hazy="{hazy_read}"`
- `clean="{clean_read}"`
- `cache_dir="{cache_dir}"`
- `epochs=2000` (the default; bump to 5000 for a final-quality pass).

The tool wires:

```
{hazy_read}  ---+
                |-- CopyCat --> Inference --> output
{clean_read} ---+
```

CopyCat's `inputData` knob points at `{hazy_read}`, `groundTruth` points at `{clean_read}`, `dataDirectory` is `{cache_dir}`, and `numEpochs` is set per the user's quality choice.

## Step 3 — Run training

CopyCat training runs inside Nuke as a long-lived task. Call the CopyCat node's `start` button via `set_knob` with `name="train"`, `value=True`, then poll the `currentEpoch` knob with `get_knob` every ~30 seconds. Surface progress to the user as `epoch X / 2000`.

If training is very long (> 5 min), prefer the Tasks primitive (B2): call `train_copycat` (when present) which wraps the train loop as a Task with progress reporting and cancellation support, instead of polling `currentEpoch` manually.

## Step 4 — Inference on the full plate

Once training finishes, the CopyCat node has a `dataDirectory` populated with `.cat` checkpoint files. The auto-build already wired the Inference node downstream — just point its `modelFile` at the latest checkpoint (the highest-numbered `.cat` in `{cache_dir}`).

Verify:

1. Run the Inference node on a paired training frame — output should match `{clean_read}` to within ~1e-3.
2. Run on a non-training frame from `{hazy_read}` — output should be visually clean. Failure modes:
   - Output looks like the hazy input -> training collapsed; restart with more epochs or more pairs.
   - Output has colour cast -> training set lacks colour variation; add more pairs.
   - Output has high-frequency noise -> over-fit; reduce epochs or increase regularisation.

## Step 5 — Bake the dehazed plate

When the user is happy with the inference, drop a Write node downstream and bake the dehazed plate to disk. Use `setup_write` with `format="exr"`, `colorspace="ACES - ACEScg"`. Inference is fast at runtime but baking once means the rest of the comp tree doesn't pay the GPU cost on every viewer scrub.

## Tools referenced

- `read_node_detail` — pair validation.
- `setup_dehaze_copycat` — full training graph.
- `set_knob` / `get_knob` — start training and poll epochs.
- `train_copycat` (B2 Task wrapper, when present) — long-running training as a task.
- `setup_write` — bake the dehazed plate.

## Hard rules

- Never call `train` on a CopyCat without explicit user confirmation — it's a long-running GPU job.
- Never overwrite an existing `{cache_dir}` without confirming with the user; CopyCat checkpoints are the user's training investment.
- If `{clean_read}` is just a hand-painted single frame, set `numEpochs <= 1000` — over-training on a single pair will not generalise.

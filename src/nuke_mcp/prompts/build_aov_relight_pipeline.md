---
name: build_aov_relight_pipeline
description: Walks through constructing a Karma EXR AOV reconstruction + relight pipeline from a Read of multi-layer EXR output.
arguments:
  - name: read_node
    description: Name of the Karma EXR Read node containing the multi-layer AOVs.
    required: true
  - name: shot
    description: Shot code used when naming downstream nodes (e.g. ss_0170). Empty string is fine if no shot code is needed.
    required: false
---
# Build AOV relight pipeline

Construct a Karma multi-channel EXR reconstruction + relight chain off `{read_node}` (shot tag: `{shot}`).

The five-step recipe below assumes the Read node already exposes the standard Karma AOV layers (`diffuse`, `specular`, `transmission`, `emission`, `direct_diffuse`, `indirect_diffuse`, etc.) and at least one light-group layer per relightable light (`lightgroup1`, `lightgroup2`, ...).

## Step 1 — Inspect the Read

Call `read_node_detail` on `{read_node}` and confirm:

- `channels` returns the expected layer set (look for `rgba`, `diffuse`, `specular`, plus per-light-group layers).
- File path resolves and frame range matches the script's global range.
- Colorspace is `ACES - ACEScg` (Karma writes scene-linear EXRs).

If any expected layer is missing, surface that to the user before continuing — relight only works if the source AOVs are intact.

## Step 2 — Auto-build the AOV merge

Call `setup_karma_aov_pipeline` with `read_node="{read_node}"` and a `name_prefix` derived from the shot tag (`{shot}` if provided, otherwise the read node name). The tool wires:

```
{read_node}
   |-- Shuffle (diffuse)      --+
   |-- Shuffle (specular)     --+-- Plus chain --> beauty_check
   |-- Shuffle (transmission) --+
   |-- Shuffle (emission)     --+
```

Verify the reconstructed `beauty_check` matches the original `rgba` to within the AOV reconstruction tolerance (~1e-4). If it doesn't, the source EXR is missing AOVs or the colorspace is wrong.

## Step 3 — Wire the relight pass

Call `setup_karma_relight` (planned C-phase tool) with `read_node="{read_node}"`. It produces one Multiply per light-group layer driven by a Constant or color knob the model can keyframe. Wire each Multiply downstream of the per-light-group Shuffle and merge with `plus`.

If `setup_karma_relight` is not yet available in the active profile, fall back to building the chain manually:

1. For each `lightgroupN` layer, add a Shuffle to extract it.
2. Add a Multiply downstream with a Constant feeding `value` so the model can knob-set per-light intensity.
3. Plus-merge all relit groups into the final relight beauty.

## Step 4 — QC viewer pair

Call `qc_viewer_pair` with `a_node="{read_node}"` and `b_node="<reconstructed_beauty>"`. This drops a pair of Viewer inputs so the user can A/B the original Karma beauty against the AOV-reconstructed one. A clean pipeline shows zero difference under `wipe`.

## Step 5 — Cache the relight result

Once the relight chain is verified, drop a Write node downstream rendering scene-linear EXR with `multiPart=true` so the relit beauty plus per-group passes survive into the next session. Use `setup_write` with `format="exr"`, `compression="zip1"`, `colorspace="ACES - ACEScg"`.

## Tools referenced

- `read_node_detail` — channel + colorspace inspection.
- `setup_karma_aov_pipeline` — Shuffle-and-plus AOV reconstruction.
- `setup_karma_relight` (C-phase) — per-light-group Multiply chain.
- `qc_viewer_pair` — A/B viewer comparison.
- `setup_write` — scene-linear EXR cache.

## When to stop

Stop after Step 4 if the user only needs AOV reconstruction (no relight). Stop after Step 5 if they need a hand-off cache for grading. Never auto-execute a render — `render_frames` requires explicit user confirmation per the destructive-tool policy.

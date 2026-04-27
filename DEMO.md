# nuke-mcp 90-second demo: Salt Spill comp loop

A walkthrough screenplay for the README-hero video. Voice prompt -> AOV merge built -> deep holdout chain wired -> ZDefocus rendered -> all visible in the Nuke DAG. End-to-end, no manual node clicks.

## Goal

Show in 90 seconds that nuke-mcp does **comp-domain depth no other DCC MCP has shipped**. The viewer should walk away thinking: this is what an AI compositor co-pilot looks like when the MCP knows AOVs, deep, and ACEScct.

## Setup (off-camera, before the recording)

- Nuke 16.x open with an empty script.
- nuke-mcp addon started (Nodes > MCP > Start Server).
- Claude Desktop wired to nuke-mcp per the [README quick start](README.md#claude-desktop-config-minimal).
- A real Karma EXR render to hand: `$SS/renders/ss_0170/v001/ss_0170.####.exr` (multi-layer beauty + diffuse_direct + diffuse_indirect + specular_direct + specular_indirect + sss + emission + lightgroup1 + lightgroup2 + cryptomatte_object + P + N + depth.z + motion).
- A FLIP blood deep render to hand: `$SS/renders/ss_0170_blood/v001/ss_0170_blood.####.exr` (deep, with `front` and `back` channels).
- A holdout roto already drawn around the hazmat suit silhouette (Roto1).
- Demo OBS scene: full-screen Nuke DAG, Claude Desktop side panel, screen-bottom caption strip, voice-over mic.

## Beats and timing

| t (s) | Beat | What's on screen | Voice-over |
|---|---|---|---|
| 0 | Cold open | Empty Nuke DAG | "This is nuke-mcp. Watch a comp build itself." |
| 4 | Voice prompt | Claude side panel: "Build me an AOV pipeline for the Salt Spill blood shot." | "I'm asking Claude to build a Karma AOV pipeline." |
| 9 | Tool call 1 | Tool chip: `setup_karma_aov_pipeline(read_path=...)` | "It detects every Karma layer. Per-layer Shuffle. Reconstruction Merge. QC viewer-pair." |
| 18 | DAG fills | Karma AOV sub-graph appears, ~14 nodes, NetworkBox `KarmaAOV_ss_0170` | (silent -- let viewer read the graph) |
| 24 | Voice prompt | Claude side panel: "Add the FLIP blood deep holdout against the hazmat plate." | "Now the deep comp." |
| 28 | Tool call 2 | Tool chip: `setup_flip_blood_comp(beauty=..., deep_pass=..., holdout_roto="Roto1")` | "DeepRecolor, DeepHoldout against the hazmat depth, DeepMerge over BG, deep-to-image, ACEScct grade, ZDefocus on `deep.front`." |
| 38 | DAG fills | `FLIP_Blood_ss_0170` NetworkBox appears, ZDefocus end-of-pipe | (silent) |
| 44 | Voice prompt | "Audit ACEScct consistency before I render." | "Always audit before rendering." |
| 48 | Tool call 3 | Tool chip: `audit_acescct_consistency(strict=True)` | "Read-only scan. Three findings, all green." |
| 54 | Audit panel | Findings printed in Claude side panel: 0 errors, 3 info | (silent) |
| 60 | Voice prompt | "Render frames 1001 to 1010 from the AOV write." | "And the render." |
| 64 | Tool call 4 | Tool chip: `render_frames(write_node="KarmaAOV_ss_0170_Write", first=1001, last=1010)` | "This is an MCP Task. It returns immediately with a task_id. Streams progress." |
| 70 | Progress stream | task_progress chips: frame 1001, 1002, 1003... | "Cancellable. Survives reconnect." |
| 80 | Final beauty | Nuke Viewer shows reconstructed beauty over BG | "Comp built, audited, rendering. Voice to DAG in 90 seconds." |
| 86 | End card | nuke-mcp logo + GitHub URL + "First DCC MCP with Tasks" | "nuke-mcp. Depth, not breadth." |
| 90 | Cut | -- | -- |

## Exact MCP tool calls in order

These are what shows up in the side-panel tool chips. Copy-paste into a dry-run script if you want to rehearse without reshooting.

### 1. AOV pipeline

```
setup_karma_aov_pipeline(
    read_path="$SS/renders/ss_0170/v001/ss_0170.####.exr",
    shot="ss_0170",
    write_path="$SS/renders/ss_0170_comp/v001/ss_0170_comp.####.exr",
)
```

Builds: Read -> per-layer Shuffles (one per detected AOV) -> reconstruction Merge -> Remove keep=rgba -> Switch (beauty vs reconstructed) + Grade gain=10 (diff QC) -> Write. Wrapped in NetworkBox `KarmaAOV_ss_0170`. Backdrop with shot code + tool version.

### 2. FLIP blood deep comp

```
setup_flip_blood_comp(
    beauty="KarmaAOV_ss_0170_Reconstructed",
    deep_pass="$SS/renders/ss_0170_blood/v001/ss_0170_blood.####.exr",
    holdout_roto="Roto1",
    blood_tint=(0.35, 0.02, 0.04),
)
```

Builds: DeepRead -> DeepRecolor (against beauty) -> DeepHoldout2 (against hazmat depth from Roto1) -> DeepMerge over BG -> deep_to_image -> Grade tinted in ACEScct context -> ZDefocus (`math=depth`, `depth=deep.front`, no AA on depth -- Foundry rule). Wrapped in NetworkBox `FLIP_Blood_ss_0170`.

### 3. ACEScct audit

```
audit_acescct_consistency(strict=True)
```

Read-only scan. Returns `[AuditFinding]` with `{severity, node, message, fix_suggestion}`. Flags Reads with default colorspace whose paths match `*_sRGB.*`, Grades downstream of ACEScg pipe with no ACEScct conversion, Writes whose output doesn't match scene-linear delivery. Never auto-fixes.

### 4. Render as MCP Task

```
render_frames(
    write_node="KarmaAOV_ss_0170_Write",
    first=1001,
    last=1010,
)
```

Returns `{task_id: ..., state: "working"}` immediately. Streams `task_progress` notifications: `{epoch_or_frame: 1001, total: 10, eta_seconds: ...}`. State persisted to `~/.nuke_mcp/tasks/<id>.json`. Survives Nuke / MCP / client restart. `tasks_cancel(task_id)` stops at next frame boundary.

## Voice-over script (90s, ~180 words)

> This is nuke-mcp. Watch a comp build itself.
>
> I'm asking Claude to build a Karma AOV pipeline. It detects every Karma layer. Per-layer Shuffle. Reconstruction Merge. QC viewer-pair.
>
> Now the deep comp. DeepRecolor, DeepHoldout against the hazmat depth, DeepMerge over BG, deep-to-image, ACEScct grade, ZDefocus on deep dot front.
>
> Always audit before rendering. Read-only scan. Three findings, all green.
>
> And the render. This is an MCP Task. It returns immediately with a task_id. Streams progress. Cancellable. Survives reconnect.
>
> Comp built, audited, rendering. Voice to DAG in 90 seconds.
>
> nuke-mcp. Depth, not breadth.

## On-screen captions

Lower-third captions, one per beat. Bold key term, sub-line for the technical claim.

| Beat | Caption (bold) | Sub-line |
|---|---|---|
| 0:09 | **`setup_karma_aov_pipeline`** | per-layer Shuffles + reconstruction Merge + QC viewer-pair |
| 0:28 | **`setup_flip_blood_comp`** | DeepRecolor -> DeepHoldout -> DeepMerge -> deep_to_image -> ZDefocus |
| 0:48 | **`audit_acescct_consistency`** | read-only OCIO/ACEScct scan, never auto-fixes |
| 1:04 | **`render_frames`** as MCP Task | task_id + progress stream + cancel + resume |
| 1:26 | **First DCC MCP with the Tasks primitive** | MCP 2025-11-25 spec |

## End-card copy

```
nuke-mcp
Production-grade MCP server for Foundry Nuke
86 tools | 9 profiles | MCP 2025-11-25 Tasks
github.com/cian-bit/nuke-mcp
```

## Recording checklist

- [ ] Nuke open at 1080p or 4K, DAG centred.
- [ ] Claude Desktop side panel pinned, font scaled up so tool chips are legible at 720p playback.
- [ ] Voice-over recorded separately (Audacity / Reaper), synced post.
- [ ] OBS scene: 70/30 split (Nuke / Claude). Lower-third caption strip overlay.
- [ ] Cut on the four tool-call boundaries -- crossfades, not hard cuts.
- [ ] Final pass: scrub the timecode against the table above. Drift > 1s on any beat = re-record that beat.
- [ ] Export 1080p H.264. Upload to YouTube unlisted first, share for review, then flip to public and update the README badge URL.

## Fallback / offline rehearsal

If you don't want to re-render the EXRs every rehearsal, the four tool calls can be dry-run against any Nuke script. The macros build the topology either way -- the renders just won't have pixels. Useful for timing the voice-over against the DAG-fill animations without burning render minutes.

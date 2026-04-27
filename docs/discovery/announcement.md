# Release announcement copy

Three ready-to-post variants for the v0.2.0 launch. Tune length per platform; keep the differentiation claims identical so the message stays consistent across surfaces.

## Twitter / X (280 chars)

```
nuke-mcp v0.2.0 is out.

First DCC MCP server with the MCP 2025-11-25 Tasks primitive: render_frames, train_copycat, bake_smartvector all run as cancellable, persistent, reconnect-safe Tasks.

86 tools, 9 profiles. AOV / deep / OCIO / tracking macros.

github.com/cian-bit/nuke-mcp
```

Variant for the visual-first audience:

```
Voice prompt -> AOV pipeline + deep holdout + ZDefocus -> rendering. 90 seconds, no clicks.

nuke-mcp v0.2.0. The Foundry Nuke MCP server with comp-domain depth.

[demo video link]
github.com/cian-bit/nuke-mcp
```

## Reddit (r/vfx, r/nuke, r/LocalLLaMA, r/mcp)

Title:

```
nuke-mcp v0.2.0 -- production-grade MCP server for Foundry Nuke (first DCC MCP with the Tasks primitive)
```

Body:

```markdown
Hi all -- I've been building [nuke-mcp](https://github.com/cian-bit/nuke-mcp), an MCP server for Foundry Nuke, for the last few weeks alongside my FMP comp work. v0.2.0 just shipped and I wanted to share what's notable about it.

**What it does**: lets Claude (or any MCP-compatible client) read and build Nuke scripts. Not just `create_node` -- it ships comp-domain macros that build working topology in one call: Karma AOV reconstruction pipelines, FLIP deep holdout chains with ZDefocus, lens-distortion envelopes, planar and 3D camera-track patches, OCIO/ACEScct audits, CopyCat ML training jobs.

**Why I built it**: the existing nuke-mcp at `kleer001/nuke-mcp` has 40+ atomic tools but no domain depth. Foundry+Griptape's roadmap doesn't address Nuke specifically. I wanted something that knew what an AOV reconstruction looks like, not just how to make a Shuffle node.

**What's actually new in v0.2.0**:

- **MCP 2025-11-25 Tasks primitive.** First DCC MCP to ship it. `render_frames`, `train_copycat`, and `bake_smartvector` return a task_id, stream `task_progress` notifications, persist state to disk. Cancellable. Survives reconnect.
- **86 tools across 9 skill profiles.** Default surface is 45 tools (core); specialised profiles load on demand via `load_profile("tracking")`.
- **Pydantic v2 structured outputs** at the tool boundary.
- **AST safety scanner** on `execute_python` -- blocks `nuke.scriptClose`, `os.remove`, write-mode `open()`, and the usual indirection paths (`getattr`, `eval`, `exec`, walrus, `__import__`, etc).
- **Audit tools** (`audit_acescct_consistency`, `audit_write_paths`, `audit_naming_convention`, `audit_render_settings`) that are strictly read-only.
- **8 first-class MCP prompts** for the most common workflows.
- **597 tests passing**, 18 skipped (live-Nuke contract tests).

**Demo (90 seconds)**: voice prompt -> AOV merge built -> deep holdout chain wired -> ZDefocus rendered -> all visible in the DAG. [video link]

License is MIT. Repo: <https://github.com/cian-bit/nuke-mcp>. Issues, ideas, and PRs welcome -- especially production-pipeline edge cases I haven't hit yet.
```

## Hacker News (Show HN)

Title:

```
Show HN: nuke-mcp -- comp-domain MCP server for Foundry Nuke (first DCC MCP with Tasks)
```

Body / first comment:

```markdown
Hi HN. I built nuke-mcp because the existing Nuke MCP servers ship atomic node primitives (create a Blur, set a knob) but no comp-domain knowledge -- so Claude ends up rebuilding an AOV reconstruction graph from scratch every time, badly.

This server ships compositing-domain macros: `setup_karma_aov_pipeline` builds a per-AOV Shuffle network plus reconstruction Merge plus QC viewer-pair from a single Read node. `setup_flip_blood_comp` wires a DeepRecolor / DeepHoldout / DeepMerge / deep-to-image / ZDefocus chain in one call. `audit_acescct_consistency` does a read-only OCIO scan and never auto-fixes.

It's also the first DCC MCP to ship the MCP 2025-11-25 Tasks primitive. Renders, CopyCat training, SmartVector bakes -- all return a task_id, stream progress notifications, persist state to disk, and survive reconnect. The state machine is `working / input_required / completed / failed / cancelled`, file-backed at `~/.nuke_mcp/tasks/<id>.json`.

86 tools across 9 skill profiles, surfaced lazily so the active tool surface stays small. AST safety scanner on `execute_python`. Pydantic v2 structured outputs. 597 tests passing.

A few things I learnt building this that might be useful to other DCC-MCP authors:

1. **Skill profiles are a free win.** Default to a small core surface; let the model expand on demand via `load_profile`. Cuts context cost ~3x without sacrificing capability.
2. **The Tasks primitive is the right shape for renders, not async/await.** A render isn't a coroutine; it's a stateful operation with progress, cancellation, and resume semantics. The 2025-11-25 spec nailed it.
3. **Per-class timeouts beat a single RECV_TIMEOUT.** A `read` is 30s; a `render` is 900s; a CopyCat training is an hour. One timeout for all of them either over-trusts fast paths or kills slow ones.
4. **Macros > primitives for domain MCPs.** The model can string primitives together, but it gets the topology wrong half the time. A macro that ships *working* topology is worth ten primitives.

Repo: <https://github.com/cian-bit/nuke-mcp>. MIT. Feedback welcome -- especially from anyone using it in a real comp pipeline.
```

## Discord / Slack VFX-channel one-liner

```
Just shipped nuke-mcp v0.2.0. MCP server for Foundry Nuke with comp-domain macros (AOV / deep / OCIO / tracking) and the MCP 2025-11-25 Tasks primitive (cancellable persistent renders). 86 tools across 9 profiles. https://github.com/cian-bit/nuke-mcp
```

## Posting cadence

Don't post all four the same hour. Sequence over 3 days so each surface has air:

- **Day 0 morning UK**: r/vfx + Discord one-liner.
- **Day 0 afternoon UK**: r/nuke + r/mcp.
- **Day 1 morning UK**: Twitter / X.
- **Day 2 morning UK**: Show HN. Don't repost on HN within 30 days.

Demo video should be live before Day 0; landing pages without a video burn first-impression goodwill.

## What not to claim

- Don't say "the only Nuke MCP" -- `kleer001/nuke-mcp` exists. Differentiate, don't erase.
- Don't say "production-tested at studios" -- it's not, yet. "Production-grade" (referring to safety scanner + Pydantic outputs + Tasks) is fine.
- Don't promise Foundry+Griptape integration. We don't know their plans.
- Don't quote tool counts in marketing if they drift between profiles. The README has the canonical mapping; link there if anyone asks.

## After-launch follow-ups

- Pin the announcement post on the GitHub repo.
- Add the demo video as the README hero (already linked, swap the URL).
- Open a discussion thread on the repo for "What macro should I add next?" -- harvests for Phase E roadmap.
- Watch the issue queue for the first 72 hours. Fast-response loop on the first three issues is the highest-leverage adoption move available.

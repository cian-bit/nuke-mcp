# mcp.so listing

Submission copy + screenshot checklist for the [mcp.so](https://mcp.so) directory listing.

## Listing form fields

mcp.so's submission form (`/submit`) takes the following fields. Copy below directly into each input.

### Name

```
nuke-mcp
```

### Tagline (one line, ~80 chars)

```
Production-grade MCP server for Foundry Nuke. AOV / deep / OCIO / Tasks.
```

### Repository URL

```
https://github.com/cian-bit/nuke-mcp
```

### Homepage URL

```
https://github.com/cian-bit/nuke-mcp
```

### Category

```
Art and Culture
```

(Fallback: `Productivity` if Art and Culture isn't an option in the dropdown. mcp.so's taxonomy drifts; pick whatever's closest at submission time.)

### Tags

```
nuke compositing vfx mcp-server tasks-primitive ocio aov deep-comp tracking copycat-ml
```

### Description (long-form, ~300 words)

```
nuke-mcp is a production-grade Model Context Protocol server for Foundry Nuke (15.x and 16.x). It gives AI assistants -- Claude Desktop, Claude Code, Cursor, and any other MCP-compatible client -- a comp-domain-deep view of a Nuke script.

Where most DCC MCP servers ship atomic node primitives, nuke-mcp ships compositing-domain macros that build working topology in a single tool call: Karma AOV reconstruction pipelines, FLIP-blood deep comp chains with ZDefocus, lens-distortion envelopes wrapped in undistorted-linear NetworkBoxes, planar and 3D camera-track patches, OCIO / ACEScct audits, and CopyCat ML training jobs that run as cancellable MCP Tasks.

It is the first DCC MCP server to ship the MCP 2025-11-25 Tasks primitive. Long-running operations (render_frames, train_copycat, bake_smartvector, solve_3d_camera) return a task_id and stream task_progress notifications. State is persisted to disk and survives MCP reconnect, Nuke restart, and client crash. Tasks are cancellable at the next frame boundary.

86 tools are organised into 9 skill profiles (core, graph_advanced, color, aov, tracking, deep, distortion, copycat, audit). Only core (45 tools) is loaded by default; the model surfaces specialised profiles via load_profile when a session needs them. This keeps the active tool surface small without sacrificing depth.

Safety: execute_python runs through an AST + regex scanner that blocks nuke.scriptClose, os.remove, write-mode open(), and indirection paths (getattr, __import__, eval, exec, walrus, globals / vars / sys.modules, unicode homoglyphs). Audit tools are read-only and never auto-fix.

Test suite: 597 passing, 18 skipped (live-Nuke contract tests, run with NUKE_BIN set). Pydantic v2 structured outputs at the tool boundary. 8 first-class MCP prompts for the most common workflows.

License: MIT. Python 3.10+.
```

### License

```
MIT
```

### Demo video URL

```
https://youtu.be/PLACEHOLDER
```

(Replace once the 90-second demo is uploaded -- see [DEMO.md](../../DEMO.md) for the screenplay.)

## Screenshot checklist

mcp.so listings render strongest with three to five screenshots. Capture in this order:

1. **Hero**: Nuke DAG mid-comp with a `KarmaAOV_ss_0170` NetworkBox visible. Claude Desktop side panel showing the `setup_karma_aov_pipeline` tool chip. 1920x1080, light theme so the screenshot survives mcp.so's compression.
2. **Tasks UX**: Claude side panel during a `render_frames` task -- task_id chip + streaming `task_progress` notifications. Catches the spec's killer feature.
3. **Audit findings**: `audit_acescct_consistency` output rendered as a list in the Claude side panel. Three findings, two green, one yellow. Demonstrates "read-only, never auto-fixes".
4. **Profile loading**: Claude calling `list_profiles` and the response panel showing all 9. Caption: "9 skill profiles, lazy-loaded."
5. **Optional**: Deep-comp DAG (FLIP_Blood_ss_0170 NetworkBox), to make the deep / FLIP / ZDefocus story visible.

Crop to the working area only -- no stray Slack or browser tabs in the OBS frame.

## Submission steps

1. Sign in at [mcp.so](https://mcp.so) with GitHub.
2. Open `/submit`. Paste each field above.
3. Upload the five screenshots in the order listed.
4. Save as draft. Walk away. Re-read tomorrow with fresh eyes -- catch typos before publishing.
5. Publish. Note the live URL and add it to the README footer alongside the awesome-mcp-servers badge.

## Indexer follow-ups

After mcp.so is live, push to the secondary indexes that scrape from it:

- [smithery.ai](https://smithery.ai) -- runs an automated GitHub crawl, but a manual submission speeds discovery by ~7 days.
- [pulse-mcp.dev](https://pulse-mcp.dev) -- has an auto-pull from awesome-mcp-servers, so the upstream PR cascades here once merged.
- [Glama AI MCP catalogue](https://glama.ai/mcp/servers) -- accepts JSON manifest submissions; reuse the long-form description above.

Set a calendar nudge to re-check each index 14 days after submission. The directory ecosystem is high-churn; entries occasionally vanish during taxonomy reshuffles and need re-submission.

## Discovery KPIs to watch

- GitHub star delta in the first 7 days after each index goes live.
- Issue / discussion volume from non-Cinematics channels.
- Referer logs from `mcp.so` and `awesome-mcp-servers` if/when GitHub Insights surfaces them.

These are sanity checks, not OKRs. The point of Phase D is making the repo *findable*; adoption is downstream.

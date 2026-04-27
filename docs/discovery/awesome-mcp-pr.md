# awesome-mcp-servers PR

Submission text for the [`punkpeye/awesome-mcp-servers`](https://github.com/punkpeye/awesome-mcp-servers) listing (the canonical community index, ~50k stars). The other repo at `appcypher/awesome-mcp-servers` mirrors the same shape; submit there too if upstream is slow to merge.

## Where to put the entry

Section: **Art and Culture** -> sub-bullet for VFX / DCC tooling.
The list is alphabetised by repo name within each section. Slot between any existing `n*` entry and the next.

## Entry (markdown)

```markdown
- [cian-bit/nuke-mcp](https://github.com/cian-bit/nuke-mcp) - Production-grade MCP server for Foundry Nuke. 86 tools across 9 skill profiles. First DCC MCP to ship the MCP 2025-11-25 Tasks primitive (cancellable, persistent, reconnect-safe long-running operations). Comp-domain depth: AOV reconstruction, deep holdout chains, lens-distortion envelopes, planar / 3D camera tracking, OCIO/ACEScct audit, CopyCat ML training as Task. AST safety scanner on `execute_python`. Pydantic structured outputs.
```

If the section already uses a fixed shorter line length, the trimmed form:

```markdown
- [cian-bit/nuke-mcp](https://github.com/cian-bit/nuke-mcp) - MCP server for Foundry Nuke. 86 tools, 9 profiles. First DCC MCP with the MCP 2025-11-25 Tasks primitive. AOV / deep / distortion / tracking / OCIO macros. AST safety scanner.
```

## PR title

```
Add cian-bit/nuke-mcp (Foundry Nuke compositing MCP server)
```

## PR body

```markdown
## Summary

Adds nuke-mcp, a production-grade MCP server for Foundry Nuke (15.x / 16.x).

## What's notable

- **First DCC MCP to ship the MCP 2025-11-25 Tasks primitive.** Long-running operations (`render_frames`, `train_copycat`, `bake_smartvector`) return a `task_id` and stream `task_progress` notifications. Disk-persisted state survives reconnect, Nuke restart, and client crash.
- **86 tools across 9 skill profiles.** Default profile is `core` (~45 tools). Specialised surfaces (`tracking`, `deep`, `aov`, `distortion`, `copycat`, `audit`) are surfaced lazily via `load_profile`.
- **Comp-domain depth.** Macros that ship working topology, not just node primitives: `setup_karma_aov_pipeline`, `setup_flip_blood_comp`, `setup_spaceship_track_patch`, `audit_acescct_consistency`, `bake_lens_distortion_envelope`.
- **AST safety scanner** on `execute_python`. Blocks `nuke.scriptClose`, `os.remove`, write-mode `open()`, indirection (`getattr`, `__import__`, `eval`, `exec`, walrus, `globals` / `vars` / `sys.modules`, unicode homoglyphs).
- **Pydantic v2 structured outputs.** `NodeDetail`, `Comp`, `RenderResult`, `KnobValue`, `DiffResult`.
- **8 first-class MCP prompts** for the most common workflows (AOV relight, deep holdout, SmartVector propagate, CopyCat dehaze, STMap envelope, planar / 3D track, ACEScct audit).

## Coverage

- Python 3.10+
- Foundry Nuke 15.x / 16.x
- 597 tests passing, 18 skipped (live-Nuke contract, run with `NUKE_BIN` set)
- License: MIT
```

## Submission steps

1. Fork [`punkpeye/awesome-mcp-servers`](https://github.com/punkpeye/awesome-mcp-servers).
2. Edit `README.md`. Find the **Art and Culture** section.
3. Add the entry alphabetically. Mirror the punctuation and dash style of neighbouring entries -- they use `[name](url) - description` with a single space hyphen.
4. Open a PR with the title and body above.
5. Mirror to [`appcypher/awesome-mcp-servers`](https://github.com/appcypher/awesome-mcp-servers) the same day. The two indexes diverge weekly; cover both.
6. Add the badge to the README once merged: `[![awesome](https://awesome.re/badge.svg)](https://github.com/punkpeye/awesome-mcp-servers#nuke-mcp)`.

## Recent neighbour PRs to match style

Before submitting, scan the last ~20 PRs to confirm the section heading hasn't been renamed and that the punctuation convention is unchanged. Common drift: section moves between "Art and Culture" -> "Creative" -> "Multimedia". Match whatever the trunk currently says.

## Failure modes / contingency

- **PR sits unreviewed for > 14 days**: ping the maintainer in the PR comments. They typically batch-merge on weekends.
- **PR rejected for "not a server, just a tool"**: this isn't a risk -- nuke-mcp is unambiguously an MCP server. But if asked, point to `src/nuke_mcp/server.py` and the FastMCP `mcp.tool` decorators.
- **PR rejected for "duplicates `kleer001/nuke-mcp`"**: reply with the differentiation table from the README. Both can coexist; the index already has multiple Blender MCPs.

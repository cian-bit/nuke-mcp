# nuke-mcp

[![tests](https://img.shields.io/badge/tests-597%20passing-brightgreen)](#)
[![tools](https://img.shields.io/badge/tools-86%20across%209%20profiles-blue)](#tool-surface)
[![mcp](https://img.shields.io/badge/MCP-2025--11--25%20Tasks-orange)](#mcp-tasks-primitive)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](#requirements)
[![nuke](https://img.shields.io/badge/Nuke-15.x%20%7C%2016.x-yellow)](#requirements)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Production-grade MCP server for Foundry Nuke.** Built for compositors who want an AI assistant that actually understands their comp -- AOVs, deep, distortion, tracking, OCIO/ACEScct, CopyCat ML -- not just `create_node`.

## 90-second demo

> Voice prompt -> AOV merge built -> deep holdout chain wired -> ZDefocus rendered -> all visible in Nuke DAG.

[![Salt Spill 90-second demo](https://img.shields.io/badge/watch-90s%20Salt%20Spill%20demo-red)](https://youtu.be/PLACEHOLDER)

Demo script + screenplay: [DEMO.md](DEMO.md).

## What it does

- **Reads your comp natively.** Claude sees every node, connection, and non-default knob value. No screenshots, no copy-paste.
- **Builds comp-domain primitives, not just nodes.** AOV reconstruction, deep holdout chains, lens-distortion envelopes, planar / 3D camera track patches, ACEScct audits, CopyCat training -- one tool call each.
- **Runs long jobs as MCP Tasks.** Render frames, train CopyCat, bake SmartVector. Cancellable. Survives reconnect. State on disk.
- **Studio-friendly destructive gates.** AST-level safety scanner blocks `nuke.scriptClose`, `os.remove`, write-mode `open()` and friends inside `execute_python`. Audit log never auto-fixes.
- **Salt Spill domain depth.** First DCC MCP with a comp-domain macro library: `setup_karma_aov_pipeline`, `setup_flip_blood_comp`, `setup_spaceship_track_patch`, `audit_acescct_consistency`.

## Why nuke-mcp

| | nuke-mcp | Foundry + Griptape | `kleer001/nuke-mcp` |
|---|---|---|---|
| Comp-domain macros (AOV / Deep / Distortion / Tracking) | yes | no published roadmap | no |
| MCP 2025-11-25 Tasks primitive | **yes (first DCC MCP)** | no | no |
| Pydantic structured outputs | yes | n/a | no |
| OCIO / ACEScct audit | yes | no | no |
| CopyCat ML training as Task | yes | no | no |
| Skill profiles (paginated tool surface) | yes (10 profiles) | n/a | no |
| AST safety scanner on `execute_python` | yes | n/a | no |
| Audit log + destructive gates | yes | n/a | no |
| Tool count | 86 | n/a | 40+ |
| Adoption posture | depth-not-breadth, comp-first | orchestration play | breadth, low adoption |

**Foundry + Griptape** (Feb 2026 acquisition) is an orchestration play. No published Nuke MCP roadmap. nuke-mcp differentiates on **depth**: comp-domain macros that ship working topology, not just node primitives.

**`kleer001/nuke-mcp`** has 40+ atomic tools but no comp-domain macros, no Tasks primitive, no OCIO audit, no Pydantic outputs. nuke-mcp is the **first DCC MCP** to ship the MCP 2025-11-25 Tasks primitive (unclaimed flag claimed here).

## Feature matrix

| Capability | What you get | Profile |
|---|---|---|
| Tools | 86 across 10 profiles, surfaced lazily via `load_profile` | (all) |
| Tasks primitive | Disk-persisted state machine: `working / input_required / completed / failed / cancelled`. Survives reconnect. | core |
| Skill profiles | Paginated tool surface. Default = `core` (~45 tools). `load_profile("tracking")` to expand. | core |
| Pydantic outputs | `NodeDetail`, `Comp`, `RenderResult`, `KnobValue`, `DiffResult` | (cross-cutting) |
| OCIO / ACEScct | `get_color_management`, `set_working_space`, `audit_acescct_consistency`, `convert_node_colorspace`, `create_ocio_colorspace` | color |
| AOV pipeline | `detect_aov_layers`, `setup_karma_aov_pipeline` (per-layer Shuffle + reconstruction Merge + QC viewer-pair), `setup_aov_merge` | aov |
| Deep workflow | `create_deep_recolor / merge / holdout / transform`, `deep_to_image`, `setup_flip_blood_comp` (FLIP -> ZDefocus chain) | deep |
| Distortion / STMap | `bake_lens_distortion_envelope`, `apply_idistort`, `apply_smartvector_propagate`, `generate_stmap` | distortion |
| Tracking | `setup_camera_tracker`, `setup_planar_tracker`, `setup_tracker4`, `bake_tracker_to_corner_pin`, `solve_3d_camera`, `bake_camera_to_card`, `setup_spaceship_track_patch` | tracking |
| CopyCat ML | `train_copycat`, `serve_copycat`, `setup_dehaze_copycat`, `list_cattery_models`, `install_cattery_model` | copycat |
| Audit | `audit_acescct_consistency`, `audit_write_paths`, `audit_naming_convention`, `audit_render_settings`, `qc_viewer_pair` | audit |
| Workflow prompts | 8 first-class MCP prompts: AOV relight, deep holdout, SmartVector paint propagate, CopyCat dehaze, STMap envelope, planar / 3D camera track, ACEScct audit | (prompts/) |

## Requirements

- Python 3.10+
- Foundry Nuke 15.x or 16.x
- An MCP-compatible client (Claude Desktop, Claude Code, Cursor, etc.)

## Quick start

```bash
git clone https://github.com/cian-bit/nuke-mcp.git
cd nuke-mcp
python -m venv .venv
.venv/Scripts/pip install -e .       # Windows
# .venv/bin/pip install -e .         # macOS/Linux
```

### Nuke addon setup

Copy the `nuke_plugin/` contents to `~/.nuke/nuke_mcp_addon/`:

```
~/.nuke/
  nuke_mcp_addon/
    __init__.py
    addon.py
    menu.py
```

Add to `~/.nuke/init.py`:

```python
nuke.pluginAddPath('./nuke_mcp_addon')
```

Add to `~/.nuke/menu.py`:

```python
import nuke_mcp_addon
toolbar = nuke.menu("Nodes")
mcp_menu = toolbar.addMenu("MCP")
mcp_menu.addCommand("Start Server", nuke_mcp_addon.start)
mcp_menu.addCommand("Stop Server", nuke_mcp_addon.stop)
```

### Claude Desktop config (minimal)

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nuke": {
      "command": "C:\\path\\to\\nuke-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "nuke_mcp"],
      "env": {
        "NUKE_HOST": "localhost",
        "NUKE_PORT": "9876"
      }
    }
  }
}
```

### Claude Code config

`~/.claude/mcp.json`:

```json
{
  "nuke": {
    "command": "nuke-mcp"
  }
}
```

## Usage

1. Open Nuke.
2. Start the MCP addon: **Nodes > MCP > Start Server**.
3. Open Claude. Start talking to your comp.

The default profile is `core` (~45 tools). Specialised surfaces are opt-in:

```
list_profiles()                    # see what's available
load_profile("tracking")           # surface camera / planar tracker tools
load_profile("deep")               # surface DeepRecolor / DeepHoldout / setup_flip_blood_comp
load_profile("aov")                # surface Karma AOV pipeline
load_profile("audit")              # surface ACEScct / write-path / naming audits
```

## Tool surface

96 tools across 10 profiles. Open [src/nuke_mcp/profiles.py](src/nuke_mcp/profiles.py) for the full mapping.

| Profile | Tools | What's in it |
|---|---|---|
| `core` | 45 | Reads, graph mutations, knobs, expressions, keyframes, render, script, roto, viewer, scene digest, profile loader, Tasks meta |
| `graph_advanced` | 4 | `create_nodes`, `set_knobs`, `execute_python`, `read_selected` |
| `color` | 7 | Keying, colour-correction, OCIO/ACEScct primitives |
| `aov` | 3 | `detect_aov_layers`, `setup_karma_aov_pipeline`, `setup_aov_merge` |
| `tracking` | 7 | 2D + 3D tracking primitives + `setup_spaceship_track_patch` macro |
| `deep` | 6 | Deep primitives + `setup_flip_blood_comp` macro |
| `distortion` | 4 | Lens distortion envelope, STMap, IDistort, SmartVector propagate |
| `copycat` | 5 | CopyCat training (Task), inference, dehaze macro, Cattery registry |
| `audit` | 5 | ACEScct, write paths, naming, render settings, QC viewer pair |

## MCP Tasks primitive

nuke-mcp is the first DCC MCP to ship the [MCP 2025-11-25 Tasks primitive](https://modelcontextprotocol.io/specification/2025-11-25). Long-running operations (`render_frames`, `train_copycat`, `bake_smartvector`, `solve_3d_camera`, etc.) return a `task_id` and stream `task_progress` notifications. State is persisted to `~/.nuke_mcp/tasks/<id>.json`. Survives MCP reconnect, Nuke restart, and client crash.

```
render_frames(write_node="Write1", first=1001, last=1240) -> {task_id: "abc123..."}
tasks_get("abc123...")  # -> {state: "working", progress: {frame: 1156, total: 240, ...}}
tasks_cancel("abc123...")  # graceful stop at next frame boundary
tasks_resume("abc123...")  # if MCP died mid-render
```

## Architecture

```
Claude / Cursor / etc
    | stdio (MCP protocol)
    v
nuke-mcp server (Python subprocess)
    | TCP socket (JSON, port 9876)
    | request_id echo, heartbeat, SO_KEEPALIVE
    v
Nuke addon (threaded server inside Nuke)
    | nuke.executeInMainThreadWithResult()
    v
Nuke Python API
```

## Safety

`execute_python` runs through an AST + regex scanner that blocks:

- `nuke.scriptClose`, `nuke.scriptClear`, `nuke.scriptExit`, `nuke.exit`, `nuke.delete`, `nuke.removeAllKnobChanged`
- `os.remove`, `os.unlink`, `shutil.rmtree`, `os.system`
- `subprocess.Popen / run / call`
- Write-mode `open()` (AST-detected, not regex-fragile)
- Indirection: `getattr`, `__import__`, `import as` aliases, `eval`, `exec`, walrus, `globals` / `vars` / `sys.modules`, unicode homoglyphs

Audit tools (`audit_acescct_consistency`, `audit_write_paths`, `audit_naming_convention`, `audit_render_settings`) are **read-only**. Never auto-fix.

## Workflow prompts

8 first-class MCP prompts under [src/nuke_mcp/prompts/](src/nuke_mcp/prompts/):

- `build_aov_relight_pipeline`
- `build_deep_holdout_chain`
- `build_smartvector_paint_propagate`
- `build_copycat_dehaze`
- `build_stmap_distortion_envelope`
- `build_planar_track_clean_plate`
- `build_3d_camera_track_project`
- `audit_acescct_consistency_guide`

## Development

```bash
pip install -e ".[dev]"
pre-commit install
pytest tests/ -v
pre-commit run --all-files
```

Tests: 597 passing, 18 skipped (live-Nuke contract tests, run with `NUKE_BIN` set).

## Tags

`nuke` `compositing` `vfx` `mcp-server` `tasks-primitive` `ocio` `aov` `deep-comp` `tracking` `copycat-ml`

## License

MIT

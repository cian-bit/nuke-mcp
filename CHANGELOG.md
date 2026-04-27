# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-04-27

The feature-complete release. Wave 1-3 phases all merged. From 38 tools / 44 tests at v0.1.0 to **86 tools / 597 tests** here.

### Headline

- **First DCC MCP server to ship the [MCP 2025-11-25 Tasks primitive](https://modelcontextprotocol.io/specification/2025-11-25).** Long-running operations (`render_frames`, `train_copycat`, `bake_smartvector`, `solve_3d_camera`) return a `task_id`, stream `task_progress` notifications, and persist state to disk. Cancellable. Survives MCP reconnect, Nuke restart, and client crash.
- **86 tools across 9 skill profiles**, surfaced lazily via `load_profile`.
- **Salt Spill comp-domain depth**: AOV reconstruction, deep holdout chains, lens-distortion envelopes, planar / 3D camera tracking, OCIO/ACEScct audit, CopyCat ML training as Task.
- **Production-grade safety**: AST + regex scanner on `execute_python` blocking destructive Nuke / OS calls and indirection paths.

### Added

#### Phase A1 -- AST + regex safety scanner

- New `src/nuke_mcp/tools/_safety.py`. AST and regex scanner blocking `nuke.scriptClose`, `nuke.scriptClear`, `nuke.scriptExit`, `nuke.exit`, `nuke.delete`, `nuke.removeAllKnobChanged`, `os.remove`, `os.unlink`, `shutil.rmtree`, `os.system`, `subprocess.Popen / run / call`, write-mode `open()`, and indirection paths (`getattr`, `__import__`, `import as` aliases, `eval`, `exec`, walrus, `globals` / `vars` / `sys.modules`, unicode homoglyphs).
- TCL pre-flight on `set_expression` (catches `python {...}` brace form alongside `python(...)`).
- Wired into `code.py:execute_python` and `expressions.py:set_expression`.
- Tests: `tests/test_safety.py` -- 30+ cases. Coverage gate: `_safety.py` >= 95%.

#### Phase A2 -- Connection-layer hardening

- `retry_with_backoff` decorator replacing inline retry at `connection.py`.
- `request_id` (uuid4 hex[:16]) on every payload; addon echoes back; sender asserts match.
- Per-command-class timeout map (`read=30s`, `mutate=60s`, `render=900s`, `copycat=3600s`, `ping=5s`).
- Heartbeat thread with `threading.Lock` socket guard; 2 consecutive misses flags `_session_lost`.
- Addon-side `SO_KEEPALIVE` + per-OS TCP keepalive tuning (Linux 1s/1s/3, Windows `SIO_KEEPALIVE_VALS`).
- Structured error envelope: `{error_class, error_code, traceback, duration_ms, request_id}`.
- New `src/nuke_mcp/main_thread.py` for typed main-thread dispatch.

#### Phase A3 -- Code-injection elimination

- Five `comp.py` tools (`setup_keying`, `setup_color_correction`, `setup_merge`, `setup_transform`, `setup_denoise`) migrated from f-string-built Python payloads to typed addon handlers.
- `render.py` Write-path traversal hardened (rejects UNC, system roots, `..`).
- `channels.py` typed-handler migration.
- `addon.py` split into `_core.py / _handlers_comp.py / _handlers_render.py`.
- `execute_python` retained as escape hatch, gated by A1 scanner.

#### Phase A4 -- MockNukeNode + test infrastructure

- `MockNukeNode` in `tests/conftest.py`. First-class node types: Read, Write, Merge2, Blur, Roto, RotoPaint, Tracker4, CameraTracker, PlanarTracker, Shuffle, ScanlineRender, DeepRecolor, DeepMerge, DeepHoldout, DeepHoldout2, DeepTransform, CopyCat, STMap, IDistort, SmartVector, VectorDistort, Grade, ColorCorrect, HueCorrect, OCIOColorSpace, Group, Backdrop, Switch, Card, Project3D, ZDefocus, Relight, Premult, FilterErode, EdgeBlur.
- `tests/contract/test_live_nuke.py` runner using `nuke -t headless_runner.py` (skipped unless `NUKE_BIN` env set).
- Per-module coverage gates in `pyproject.toml`.
- 7 untested modules filled (comp, roto, channels, render, viewer, code, plus new tracking, deep).

#### Phase A5 -- Crash recovery

- Watchdog at `nuke_plugin/_watchdog.py`. 3 consecutive handler failures writes `~/.nuke_mcp/crash_marker.json` with last tool name, last request_id, traceback.
- On reconnect, MCP includes `{"warning": "session lost ~Xm ago, last op was Y"}` in next response.
- No auto-save of corrupt state.

#### Phase B1 -- Two-threshold response truncation

- New `src/nuke_mcp/response.py`. `RESPONSE_SIZE_WARN=100_000`, `RESPONSE_SIZE_HARD=500_000`. Per-tool drop-field allowlists.
- `read_node_detail` `_SKIP_KNOBS` extended (`note_font*`, `gl_color`, `dope_sheet`, `tile_color`, `cached`, `bookmark`, `postage_stamp_*`, `*_panelDropped`, `lifetimeStart/End`).
- `list_nodes > 200` strips to `name,type,error` + `digest_fallback=True`.
- `read_comp > warn` retroactively `summary=True`; `> hard` falls through to digest.
- 2000-node fixture asserts `read_comp` < 100KB.

#### Phase B2 -- MCP 2025-11-25 Tasks primitive

- `src/nuke_mcp/tasks.py`: `class Task`, `TaskStore` writing `~/.nuke_mcp/tasks/<id>.json`.
- New core tools: `tasks_list`, `tasks_get(id)`, `tasks_cancel(id)`, `tasks_resume(id)`.
- Converted to async Tasks: `render_frames`, `train_copycat`, `setup_dehaze_copycat`, `bake_smartvector`, `apply_smartvector_propagate`, `bake_lens_distortion_envelope`, `solve_3d_camera`.
- Addon-side `_handle_render_async` worker emits `{"type":"task_progress",...}` JSON lines on the same socket.
- MCP-side `_recv_json` demuxes `task_progress` notifications to a callback queue.
- Stale `working` tasks swept to `failed` on reconnect.
- Cancellation via `nuke.executeInMainThread(lambda: nuke.cancel())`.

#### Phase B3 -- Schema-from-signature decorator

- New `src/nuke_mcp/registry.py`: `@nuke_tool(profile=..., annotations=..., output_model=...)`. Inspects `inspect.signature` and builds JSONSchema for FastMCP.
- All 56 tools migrated. Drops ~150 LOC of boilerplate.

#### Phase B4 -- Skill profiles (paginated tool exposure)

- New `src/nuke_mcp/profiles.py`. 9 profiles: `core`, `graph_advanced`, `color`, `aov`, `tracking`, `deep`, `distortion`, `copycat`, `audit`.
- Default = `core` (~45 tools). `load_profile` / `unload_profile` / `list_profiles` runtime tools.
- Server emits `tools/list_changed` notification on profile change.

#### Phase B5 -- Pydantic v2 structured outputs

- New `src/nuke_mcp/models/`: `node.py` (`NodeDetail`, `NodeSummary`, `KnobValue`), `comp.py` (`Comp`, `CompDigest`, `CompDelta`), `render.py` (`RenderResult`, `RenderProgress`).
- 5 high-leverage tools plumbed first; remaining tools migrate per phase. `extra="allow"` during stage rollout.

#### Phase B6 -- Annotation presets

- New `src/nuke_mcp/annotations.py`: `READ_ONLY`, `IDEMPOTENT`, `DESTRUCTIVE`, `OPEN_WORLD`, `BENIGN_NEW`.
- All registered tools tagged. CI static check: every tool has >= 1 hint.

#### Phase B7 -- Speed wins

- Single-pass `read_comp` knob iteration in addon (was iterating twice). 30-40% speedup on knob-heavy nodes.
- Per-request node-name -> node-pointer cache.
- Warm connection on launch.
- Scene `digest` + `delta(prev_hash)` early-exit on no-op turns.

#### Phase C1 -- Atomic primitives (`tracking.py`, `deep.py`)

- `tracking.py`: `setup_camera_tracker`, `setup_planar_tracker`, `setup_tracker4`, `bake_tracker_to_corner_pin`, `solve_3d_camera`, `bake_camera_to_card`. Aligned with Nuke 15.x API (PlanarTracker, `solveCamera` knob).
- `deep.py`: `create_deep_recolor`, `create_deep_merge`, `create_deep_holdout` (DeepHoldout2 in Nuke 15.x), `create_deep_transform`, `deep_to_image`.
- All return `NodeRef = {name, type, x, y, inputs}`. Idempotent: optional `name=` kwarg returns existing node if topology matches.
- Tracker primitive input validation. NodeRef wire-key alignment with addon convention.

#### Phase C2 -- OCIO / ACEScct (`tools/color.py`)

- `get_color_management`, `set_working_space`, `audit_acescct_consistency`, `convert_node_colorspace`, `create_ocio_colorspace`.
- Reads `nuke.root()['colorManagement']`, `['OCIO_config']`, `['workingSpaceLUT']`, `['defaultViewerLUT']`.
- Audit flags: Read with default colorspace whose path matches `*_sRGB.*` / `*.png|.jpg`; Grade downstream of ACEScg pipe with no ACEScct conversion; Write whose output doesn't match scene-linear delivery.

#### Phase C3 -- AOV / Karma EXR pipeline (`tools/aov.py`)

- `detect_aov_layers(read_node)` parses `Read.metadata()` exr/* keys + `r.channels()`.
- `setup_karma_aov_pipeline(read_path)` builds full Shuffle-per-layer sub-graph + reconstruction Merge + `Remove keep=rgba` + QC viewer-pair.
- `setup_aov_merge` migrated from naive additive Merge2 to AOV-aware reconstruction.

#### Phase C4 -- Distortion (`tools/distortion.py`)

- `bake_lens_distortion_envelope(plate, lens_solve, write_path=None)`. Wraps comp body in NetworkBox `LinearComp_undistorted`.
- `apply_idistort(plate, vector_node, uv_channels)`.
- `apply_smartvector_propagate(plate, paint_frame, range_in, range_out)`.
- `generate_stmap(lens_distortion_node, mode="undistort"|"redistort")`.
- STMaps cached to `$SS/comp/stmaps/`. SmartVector + STMap renders are Tasks.

#### Phase C5 -- Tracking workflow (`tools/track_workflow.py`)

- `setup_spaceship_track_patch(plate, ref_frame, surface_type, patch_source)`. Decision tree: planar -> planar tracker + RotoPaint clone + corner-pin restore. 3D -> CameraTracker + solve + bake_camera_to_card + Project3D + ScanlineRender + Merge.
- Wraps in Group `SpaceshipPatch_<shot>`.

#### Phase C6 -- Deep comp workflow (`tools/deep_workflow.py`)

- `setup_flip_blood_comp(beauty, deep_pass, motion=None, holdout_roto, blood_tint)`.
- DeepRead -> DeepRecolor -> DeepHoldout (against hazmat depth) -> DeepMerge over BG -> deep_to_image -> Grade tinted in ACEScct context -> ZDefocus (`math=depth`, `depth=deep.front`, no AA on depth).

#### Phase C7 -- CopyCat / Cattery ML (`tools/ml.py`)

- `train_copycat(model_path, dataset_dir, epochs, in_layer, out_layer, inverse)` -- Task.
- `serve_copycat(model_path, plate)`.
- `setup_dehaze_copycat(haze_exemplars, clean_exemplars, epochs)` -- Task. Inverse training (clean->hazy, run forward).
- `list_cattery_models(category)`, `install_cattery_model(model_id)`.
- Progress shape: `{epoch, total_epochs, loss, eta_seconds, sample_thumbnail_path}`.

#### Phase C9 -- Audit + QC (`tools/audit.py`)

- `audit_acescct_consistency` (delegating wrapper to C2 owner; degraded payload if C2 absent).
- `audit_write_paths(allow_roots=["$SS"])`, `audit_naming_convention(prefix="ss_")`, `audit_render_settings(expected_fps, expected_format, expected_range)`.
- `qc_viewer_pair(beauty, recombined)` -- Switch + Grade(gain=10) visual diff builder.
- All findings: `{severity, node, message, fix_suggestion}`. Read-only, never auto-fixes.

#### Phase C10 -- Workflow prompts (8 first-class MCP prompts)

- `src/nuke_mcp/prompts/build_aov_relight_pipeline.md`
- `src/nuke_mcp/prompts/build_deep_holdout_chain.md`
- `src/nuke_mcp/prompts/build_smartvector_paint_propagate.md`
- `src/nuke_mcp/prompts/build_copycat_dehaze.md`
- `src/nuke_mcp/prompts/build_stmap_distortion_envelope.md`
- `src/nuke_mcp/prompts/build_planar_track_clean_plate.md`
- `src/nuke_mcp/prompts/build_3d_camera_track_project.md`
- `src/nuke_mcp/prompts/audit_acescct_consistency_guide.md` (renamed from `audit_acescct_consistency` to avoid tool-name collision with the C9 audit tool).
- Wired through `register_prompts` in `build_server`.

### Phase totals (delta vs v0.1.0)

| Phase | Tools added | Tests added (cumulative pass count) | Notes |
|---|---|---|---|
| v0.1.0 baseline | 38 | 44 | 11 modules, no safety, no Tasks, no profiles |
| A1 safety | 0 | ~30 | scanner + tests |
| A2 connection | 0 | ~12 | retry, request_id, heartbeat, error envelope |
| A3 typed handlers | 0 | ~20 | 5 comp.py + render + channels migrated |
| A4 mocks + tests | 0 | ~150 (cumulative ~256) | MockNukeNode + 7-module fill |
| A5 crash recovery | 0 | ~11 | watchdog tests |
| B1 truncation | 0 | ~8 | two-threshold + 2000-node fixture |
| B2 Tasks primitive | 4 (`tasks_*` meta) | ~25 | Task store, async render |
| B3 schema-from-signature | 0 | ~15 | registry decorator + migration |
| B4 profiles | 3 (`list_profiles`, `load_profile`, `unload_profile`) | ~10 | 9 profiles |
| B5 Pydantic outputs | 0 | ~12 | 5 models, 5 plumbed tools |
| B6 annotations | 0 | ~5 | preset library + audit |
| B7 speed wins | 0 | ~5 | single-pass + cache + digest delta |
| C1 atomic primitives | 11 (6 tracking + 5 deep) | ~30 | Nuke 15.x alignment |
| C2 OCIO / ACEScct | 5 | ~15 | colour module + handlers |
| C3 AOV / Karma | 2 net (`detect_aov_layers`, `setup_karma_aov_pipeline`; `setup_aov_merge` migrated) | ~12 | per-layer Shuffle + reconstruction Merge |
| C4 distortion | 4 | ~14 | envelope + STMap + IDistort + SmartVector |
| C5 tracking workflow | 1 (`setup_spaceship_track_patch`) | ~8 | macro on top of C1 |
| C6 deep workflow | 1 (`setup_flip_blood_comp`) | ~8 | FLIP -> ZDefocus chain |
| C7 CopyCat ML | 5 | ~15 | training as Task |
| C9 audit + QC | 5 | ~18 | read-only scans + viewer pair |
| C10 workflow prompts | 0 (8 prompts) | ~10 | first-class MCP prompts |
| **v0.2.0 total** | **86** | **597 passing, 18 skipped** | 9 profiles, Tasks, Pydantic, audit |

### Changed

- `setup_aov_merge` migrated from naive additive Merge2 to AOV-aware reconstruction (Phase C3). Existing call sites continue to work; output topology is richer.
- `set_expression` now does a TCL pre-flight before `setExpression` (Phase A1). Invalid expressions surface a structured error rather than a Nuke runtime fault.
- `addon.py` split into `_core.py / _handlers_comp.py / _handlers_render.py` (Phase A3). Wire-protocol unchanged.
- `RECV_TIMEOUT` / `RECV_TIMEOUT_RENDER` constants replaced by `TIMEOUT_CLASSES` map (Phase A2). `send(command, _class="render", ...)` is the new shape.

### Fixed

- Auto-reconnect no longer replays non-idempotent `setup_*` mutations on timeout (skip replay when `_class not in ("read","ping")`).
- `send_raw()` now applies request_id validation + structured envelope.
- `render_frames` uses `_class="render"` (900s) rather than ad-hoc `timeout=300.0`.
- `_stop_heartbeat()` no longer joins itself when called from heartbeat-thread failure path.
- `_handle_setup_write` rejects UNC paths, system roots, `/etc/passwd` (was only rejecting `..`).
- `setup_*` family annotations changed `IDEMPOTENT` -> `BENIGN_NEW` to reflect that they create new nodes every call.

### Phase D -- this release

Documentation + discovery. No tool changes.

- README rewrite: feature matrix, differentiation vs Foundry+Griptape and `kleer001/nuke-mcp`, demo hero, profile breakdown.
- `DEMO.md`: 90-second Salt Spill demo loop screenplay.
- `docs/discovery/awesome-mcp-pr.md`: ready-to-submit PR text for `awesome-mcp-servers`.
- `docs/discovery/mcpso-listing.md`: mcp.so submission copy + screenshot checklist.
- `docs/discovery/announcement.md`: Twitter / Reddit / HN / Discord launch copy.
- Version bumped from `0.1.0` to `0.2.0`.

## [0.1.0] - 2026-04 (pre-Wave-1 baseline)

The pre-hardening v2 baseline that Phase A inherited.

### Added

- 38 tools across 11 modules: `read.py`, `graph.py`, `knobs.py`, `expressions.py`, `comp.py`, `render.py`, `script.py`, `roto.py`, `channels.py`, `viewer.py`, `code.py`.
- Threaded Nuke addon at `nuke_plugin/addon.py` over TCP socket on port 9876.
- `nuke.executeInMainThreadWithResult` dispatch.
- 44 tests passing.

[0.2.0]: https://github.com/cian-bit/nuke-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/cian-bit/nuke-mcp/releases/tag/v0.1.0

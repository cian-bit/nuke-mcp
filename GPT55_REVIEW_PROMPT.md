# GPT-5.5 Review Prompt — Nuke MCP `feature/foundation-hardening`

Paste everything below the line into GPT-5.5.

---

## Authorization and context

I am the sole author and maintainer of the open-source repository at `C:\Users\Josh\Documents\nuke-mcp\` (public on GitHub at `cian-bit/nuke-mcp`, MIT licensed). I am asking you to perform a defensive code review of my own code, on my own machine, before I merge a feature branch.

The repository ships an input-validation layer (`_safety.py`) whose explicit purpose is to refuse dangerous Python payloads before they are forwarded to a local Foundry Nuke instance running on my own workstation. My goal is the standard one for a security-conscious open-source maintainer: confirm the validator does what it claims, and find any gaps in test coverage that would let a buggy or malicious MCP client cause data loss on the developer's own machine. There is no third party, no production system, and no remote target involved — the threat model is "an LLM acting as MCP client emits unsafe Python that wipes my Nuke script or `~/.nuke/` directory."

This is the same kind of review that any reasonable maintainer of a CPython sandbox, a Jupyter kernel restriction, or an MCP server would request before merging. Please treat it as a normal defensive review, not as an offensive engagement.

## Role

You are a senior Python infrastructure engineer doing a defensive code review of an MCP (Model Context Protocol) server that bridges Anthropic's Claude (and other MCP clients) to Foundry Nuke for compositing automation. I want a real second pair of eyes — find bugs, missing input validation, race conditions, API misuse, test gaps, and design smells. Do not flatter. Do not hedge. If something is fine, say "fine" and move on; if something is broken, say what's broken and propose the fix as a unified diff.

You are reviewing **branch `feature/foundation-hardening`** in the local repo at `C:\Users\Josh\Documents\nuke-mcp\`. The branch sits 5 commits ahead of `master`. Do not assume any commit beyond what's listed.

## Context — read this carefully before you open any file

The codebase is a TCP-socket bridge: a FastMCP-based Python server (in `src/nuke_mcp/`) on the host side speaks JSON over `localhost:9876` to a daemon thread loaded inside Nuke's Python interpreter (in `nuke_plugin/addon.py`). All Nuke API calls must go through `nuke.executeInMainThreadWithResult()` because Nuke's Python is single-threaded on the UI side.

Five commits just landed on this branch (oldest → newest):

1. **`008e086` — AST+regex safety scanner.** Blocks `nuke.scriptClose`/`scriptClear`/`scriptExit`/`exit`/`delete`, `os.remove`/`unlink`/`system`, `subprocess.*`, write-mode `open(...)`, `getattr(nuke, "scriptClose")` bypass, `__import__("os").remove(...)`, alias chains. Wired into `code.py:execute_python` and `expressions.py:set_expression`. 67 new tests; 99% coverage of `_safety.py`.

2. **`649da34` — Connection hardening.** Replaced ad-hoc retry loop with `retry_with_backoff(jitter=True)` decorator; added per-command-class timeout map (`{"read":30,"mutate":60,"render":900,"copycat":3600,"ping":5}`); added `request_id` (uuid4 hex[:8]) round-trip, addon echoes back; added 5s heartbeat thread that flags `_session_lost=True` after 2 misses; added `probe_existing_connection()` 0.5s wall-clock liveness check; added structured error envelope (`error_class`, `duration_ms`, `request_id`, `traceback`); added `main_thread.run_on_main` helper; added per-OS SO_KEEPALIVE on the addon socket (Linux TCP_KEEPIDLE/INTVL/CNT, Windows SIO_KEEPALIVE_VALS). New module `src/nuke_mcp/main_thread.py`.

3. **`d8cd027` — Response truncation + annotation presets.** New `src/nuke_mcp/response.py` with two-threshold truncation (100KB warn / 500KB hard), per-tool drop allowlists (`_UI_KNOBS_EXTENDED` glob-matched via `fnmatch`), `_estimate_response_size`, `_truncate_response`, `_add_response_metadata`, `apply_response_shape(obj, operation)` entry point. New `src/nuke_mcp/annotations.py` with `READ_ONLY`/`IDEMPOTENT`/`DESTRUCTIVE`/`OPEN_WORLD` dicts. Audited and applied annotations across all 43 registered tools. `_helpers.py:nuke_command` decorator now pipes returns through `apply_response_shape`.

4. **`8143cd6` — MockNukeNode + tests for 7 modules.** New `MockNukeNode` class in `tests/conftest.py` with knobs/inputs/setInput/xpos/ypos/Class/name/dependent/metadata API; factories for 30+ node types (Read, Write, Merge2, Tracker4, CameraTracker, DeepRecolor, DeepMerge, CopyCat, STMap, SmartVector, ScanlineRender, ZDefocus, etc.). New test files: `test_comp.py`, `test_render.py`, `test_channels.py`, `test_roto.py`, `test_viewer.py`. Placeholder `test_tracking.py` + `test_deep.py` with xfailed tests waiting for C1 to fill the empty modules. New `tests/contract/test_live_nuke.py` runner skipped unless `NUKE_BIN` env set. Per-file coverage gates in `tests/test_coverage_gates.py`.

5. **`69ddc00` — Typed handlers + read_comp single-pass + scene digest.** Six addon-side typed handlers (`_handle_setup_keying`, `_handle_setup_color_correction`, `_handle_setup_merge`, `_handle_setup_transform`, `_handle_setup_denoise`, `_handle_setup_write`) replace f-string code injection. MCP-side `comp.py` and `setup_write` are now thin dispatchers via `main_thread.run_on_main`. Allowlists on `operation`/`file_type`; path-traversal reject on `..`. `_handle_read_comp` now single-pass over `n.knobs()`. Added per-request `threading.local` node cache. `server.py` now warm-connects on launch with graceful fallback. New `src/nuke_mcp/tools/digest.py` with `scene_digest()` (md5 hex[:8] over node graph) and `scene_delta(prev_hash)`.

Final test state: **267 passed, 1 skipped (live-Nuke contract — `NUKE_BIN` unset), 11 xfailed (C1 placeholders).** Total coverage 89%. All per-file gates green.

## Your job

Pick at every layer. Specifically pursue these eight angles, in this order, and budget your reasoning as you go (you are encouraged to think hard — the harder, the better):

### 1. Input validation completeness — the safety scanner

Open `src/nuke_mcp/tools/_safety.py` and `tests/test_safety.py`. The validator's job is to refuse Python payloads that would call destructive Nuke or filesystem APIs. Evaluate whether it correctly identifies the following patterns. For each pattern that the validator does NOT currently detect, classify it as a coverage gap and propose a unified-diff patch that closes it.

Patterns to evaluate (these are common Python indirection forms that any sandbox-style validator must handle correctly — not novel exploits):

- Indirect execution through `eval` or `exec` of a string containing a forbidden call.
- Walrus-operator alias: `walrus := nuke.scriptClose; walrus()`.
- Module-dictionary lookup: `globals()["nuke"].scriptClose()`, `vars(nuke)["scriptClose"]()`.
- Decorator-flavored indirection: `@functools.lru_cache` wrapping a function returning a forbidden callable.
- Regex backstop coverage: cases where the AST pass is bypassed (e.g. by syntax errors or string concatenation) and the regex must catch them.
- TCL pre-flight in `expressions.py`: TCL has multiple quoting forms, including `[python …]`, `[python {…}]`, and brace nesting. Does the validator handle each?
- Unicode normalization: identifiers that look like ASCII forbidden names but contain confusable code points (e.g. Greek omicron in place of Latin "o"). The standard mitigation is NFKC normalization or codepoint-class restriction; check whether the validator applies one.

For each pattern, state whether the current validator detects it, and where (file:line). For each gap, propose a fix as a unified diff.

### 2. Connection layer — race conditions, lost messages, deadlocks

Open `src/nuke_mcp/connection.py`, `src/nuke_mcp/main_thread.py`, `src/nuke_mcp/tools/_helpers.py`, `nuke_plugin/addon.py` (`_dispatch`, `_handle_client`, the SO_KEEPALIVE setup, the request-id echo). Specifically:

- The `_io_lock` wraps `_send_json` + `_recv_json` together. Is there any code path where a different thread's response gets read by another caller? Walk the heartbeat thread interleaving with a tool call — can the heartbeat's `pong` be consumed by a tool's `recv`, causing the tool to assert `request_id` mismatch and the heartbeat to time out?
- The auto-reconnect path on `OSError`/`ConnectionError`: if the addon crashes mid-render, the host-side `send()` re-sends the same payload after reconnect — does the addon's HANDLERS dict have any non-idempotent handler that misbehaves on duplicate? Specifically `setup_*` family.
- After A2's structured error envelope: is `CommandError.envelope` accessed defensively by `_helpers.py`? What if `envelope` is never populated (e.g. on a generic Exception)?
- Heartbeat thread: does it cleanly die when `disconnect()` is called from the foreground? Is there a join? Is there a daemon=True flag? Does `_session_lost` get reset on reconnect?
- request_id collision probability: uuid4 hex[:8] = 32 bits. At ~1000 req/s sustained, what's the birthday probability of collision in a session?
- Per-OS SO_KEEPALIVE: the Windows path uses `SIO_KEEPALIVE_VALS = (1, 1000, 1000)` — is that the right tuple shape for that ioctl? Check `socket.SIO_KEEPALIVE_VALS` semantics.

### 3. Truncation correctness

Open `src/nuke_mcp/response.py` and `tests/test_truncation.py`. Look for:

- `_estimate_response_size` calls `json.dumps`. What if the dict contains a non-JSON-serializable value (a numpy array, a datetime, a node object)? Does the truncation path swallow the exception and pass through, or raise?
- `fnmatch` glob match for knob skips: does `note_font*` accidentally match `note_font` (no suffix) — desired? — but does `postage_stamp_*` accidentally match `postage_stamp` itself?
- The recursive truncation drops "deepest children first." How is "deepest" determined? Is the algorithm stable across Python dict ordering changes?
- After truncation, the `_meta` block reports `drop_fields_applied`. Is it accurate when the same field is dropped at multiple depths?
- The 200-char string truncation appends `<N chars>` — is N the original length or the truncated length?
- Does `apply_response_shape` get called when the result is itself a list, not a dict? `_helpers.py` checks `isinstance(result, dict)` — but what if a tool returns `{"items": [<huge list>]}`?

### 4. Annotation correctness vs MCP spec

Open `src/nuke_mcp/annotations.py` and grep all `@ctx.mcp.tool(annotations=...)` sites. The MCP spec 2025-11-25 defines:
- `readOnlyHint: bool` — tool only reads.
- `destructiveHint: bool` — tool may delete or overwrite.
- `idempotentHint: bool` — repeated calls have same effect as one.
- `openWorldHint: bool` — tool interacts with external systems beyond the local sandbox.

Check:
- Does `auto_layout` deserve `idempotentHint=True`? Calling it twice on the same DAG produces the same layout *only if* the DAG is unchanged between calls — is that the spec intent?
- `setup_keying` has `idempotentHint=True` but the implementation has `TODO(A3-followup)` for actual idempotent detection. The annotation lies. Is that acceptable while the TODO sits? Document or change it.
- `create_node` has `destructiveHint=False` (benign-new) — but it does change script state. Does the spec intend "destructive" to mean "destroys existing data" or "mutates state"? The Anthropic docs are ambiguous; check what `MaxStudio` or `blender-mcp` do.
- `execute_python` is `DESTRUCTIVE | OPEN_WORLD` — but with `allow_dangerous=False` it's actually quite constrained. Does annotation cover the worst case correctly? Yes/no, justify.

### 5. Test rigor

Open `tests/conftest.py` (MockNukeServer + MockNukeNode), then survey the test files. Look for:

- Tests that mock the MCP boundary but never touch the addon mock: are those tests proving anything useful, or just the round-trip plumbing?
- Tests that assert on string content of an error message (brittle to copy-edit). Find them.
- Coverage gates in `tests/test_coverage_gates.py`: are they reading `.coverage` correctly? Does the test work in isolation (without `--cov`)?
- The xfailed tests in `test_tracking.py` / `test_deep.py`: do they assert on the eventual function signatures correctly? Will they actually flip green when C1 lands, or will they need rewrite?
- Live-contract runner (`tests/contract/test_live_nuke.py`): does it actually start Nuke headless and verify a handshake, or does it just import the addon module?
- Heartbeat tests in `test_connection.py`: are they flake-prone? Do they monkey-patch time, or sleep for real? If real sleep, on a slow CI runner do they fail?
- Mock leakage: does `MockNukeServer` cleanly tear down between tests? Does the autouse `NUKE_MCP_HEARTBEAT=0` fixture from A2 actually disable the thread, or does it just suppress logging?

### 6. Houdini-MCP parity gaps

The Houdini MCP at `C:\Users\Josh\Documents\houdini-mcp-beta\` is the maturity benchmark. Diff the two:

- Does Nuke's safety scanner have feature parity with Houdini's? Specifically `_classify_alias_source` and `_collect_dangerous_aliases` — was every nuance ported?
- Does Nuke's connection layer have feature parity? Houdini ships `probe_existing_connection` with a `concurrent.futures.ThreadPoolExecutor(max_workers=4)`; does Nuke?
- Does Nuke have a preset save/apply system equivalent to Houdini's `tools/presets.py`? (Plan says: deferred. Confirm.)
- Does Nuke have a cook-cost estimator equivalent to Houdini's `tools/cost.py`? (Plan says: out of scope for Phase A. Confirm.)

### 7. Plan adherence

Open `C:\Users\Josh\.claude\plans\i-am-now-using-cuddly-cray.md`. Cross-check the five landed commits against the plan's Phase A and partial Phase B. Specifically:

- A5 (crash recovery watchdog with `crash_marker.json`) was deferred. Is the SO_KEEPALIVE + heartbeat alone sufficient, or does the missing watchdog mean a Nuke segfault still leaves the MCP holding a stale `_session_lost=False`?
- B7's "30–40% read_comp speedup" is unverifiable without live Nuke. Is there a way to verify from the addon code alone (loop count analysis, cyclomatic) that it was halved?
- The plan's "Wave 4" (Salt Spill domain depth, C1–C10) hasn't started. Is the foundation actually ready to support those macros, or did A1–A4+B1+B6+B7 leave a sharp edge?

### 8. Hunt for a real bug

Spend at least 10 minutes looking for a behavioral defect (not a style issue). Targets:
- `nuke_plugin/addon.py:_handle_read_comp` after the single-pass refactor — read both the old and new code paths (use `git show 8143cd6:nuke_plugin/addon.py` vs `HEAD`).
- The threading.local node cache — does it clear between requests, or does it leak across `_dispatch` calls in the daemon thread?
- `digest.py:scene_delta(prev_hash)` — what if the addon-side `_handle_scene_digest` returns the same hash because the underlying data is genuinely unchanged, but the *previous call* never actually committed its hash? (Stale snapshot.)
- Path traversal in `_handle_setup_write`: only rejects `..`. What about absolute Windows paths like `C:\Windows\System32\drivers\etc\hosts`? Does the addon write there?
- `_safety.py` regex backstop: is it triggered ONLY on `SyntaxError`, or on every call? If only on SyntaxError, is the AST scan provably stronger than the regex (i.e. nothing the regex catches escapes the AST)?

## Operating instructions

- **Always cite file:line**. Don't say "in connection.py" — say "in `src/nuke_mcp/connection.py:142-160`".
- **Always show the bug before the fix**. Quote the exact lines that are wrong.
- **Always offer a fix as a unified diff**. Format as triple-backtick `diff` blocks.
- **Distinguish "real bug" from "design smell"**. Tag findings as `[BUG]` (will misbehave in production), `[RACE]` (concurrency hazard, may not always trigger), `[SECURITY]` (exploitable), `[SMELL]` (works but ugly/fragile), `[GAP]` (test or doc missing), `[NIT]` (style/minor).
- **Don't propose features that aren't already partially built**. The user does not want scope creep — they want this branch hardened.
- **If you're unsure about Nuke API behavior**, say so explicitly. Don't fabricate. Examples: "I believe `nuke.executeInMainThreadWithResult` raises `RuntimeError` if called from the main thread, but verify in Foundry docs."
- **Reasoning budget**: this is worth at least 60–90 minutes of your time. Use it. Show your reasoning trace.

## Deliverable shape

Return a single Markdown document with this structure exactly:

```
# Nuke MCP foundation-hardening review — GPT-5.5

## Verdict (1 paragraph)
<honest summary; ship/don't-ship/needs-X>

## Critical findings ([BUG]/[SECURITY]/[RACE], must fix before merge)
### finding-N: <one-line title>
- **Where:** path:line
- **What's wrong:** ...
- **Why it matters:** ...
- **Fix:**
```diff
- old
+ new
```

## Important findings ([SMELL]/[GAP], should fix soon)
<same shape>

## Minor findings ([NIT])
<terse list>

## What's solid (be specific — don't generalize)
<what they got right; bullet list of file:line citations>

## What I couldn't verify without live Nuke
<list>

## Recommended next move
<one paragraph: ship phase A as-is? add A5 first? rewrite something?>
```

Begin. Take your time. Be ruthless.

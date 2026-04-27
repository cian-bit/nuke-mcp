---
name: audit_acescct_consistency_guide
description: Guides the model through running the ACEScct consistency audit and interpreting findings (Read colorspaces, Write colorspaces, working space, OCIO config).
arguments:
  - name: scope
    description: Audit scope - "all" for the whole script, "selected" for currently selected nodes only, or a specific node name.
    required: false
---
# Audit ACEScct consistency

Run the C2 `audit_acescct_consistency` tool over scope `{scope}` and walk the user through the findings.

This audit catches the four classes of colour-pipeline mistake that bite Salt Spill (and any ACES-cct working-space comp):

1. Reads tagged with the wrong colorspace (e.g. a linear EXR tagged as `sRGB`).
2. Writes that double-transform (`ACES - ACEScct` tagged but written through a `Convert from ACEScct` upstream).
3. Working-space drift (a colour grade authored in `ACES - ACEScct` then accidentally Mergeed into a `linear` chain).
4. OCIO config mismatch (script uses `aces_1.0.3` but the studio shipped a `cg-config-v2.0.0` config).

## Step 1 — Run the audit

Call `audit_acescct_consistency` with `scope="{scope}"`. The tool returns a structured findings list:

```
{
  "ocio_config": "aces_1.3",
  "working_space": "ACES - ACEScct",
  "findings": [
    {"severity": "error",   "node": "Read1", "message": "...", "fix_suggestion": "..."},
    {"severity": "warning", "node": "Write1", "message": "...", "fix_suggestion": "..."},
    ...
  ],
  "summary": {"errors": 1, "warnings": 3, "ok": 14}
}
```

If `findings` is empty, surface "Audit clean — `{scope}` is ACEScct-consistent" and stop.

## Step 2 — Triage by severity

Errors come first. Each `error` finding represents a colour mistake that will produce visibly wrong pixels at delivery — fix before anything else. Walk the user through them one by one:

1. Quote `node` and `message`.
2. Suggest the fix from `fix_suggestion` (the audit always proposes one).
3. Ask the user to confirm before flipping the knob — colorspace changes are destructive in the sense that they re-interpret existing pixels.

Then `warning` findings — these are inconsistencies that may or may not be intentional (e.g. a `linear` Read of an STMap, which is correct because STMaps are coordinate maps, not RGB). Ask the user "is this intentional?" before changing.

`info` findings (if any) are observations only — never auto-fix.

## Step 3 — Apply fixes (with confirmation)

For each finding the user wants to fix, call `set_knob` on the relevant node's `colorspace` knob. Examples:

- "Read1 is linear EXR tagged sRGB" -> `set_knob` `name="colorspace"`, `value="ACES - ACEScg"` on Read1.
- "Write1 double-transforms ACEScct" -> remove the upstream `OCIOColorSpace` and set Write1's colorspace to `ACES - ACEScct`.
- "Working space drift on Grade1" -> wrap the grade in `OCIOColorSpace` (in: `ACEScg`, out: `ACEScct`) and back (in: `ACEScct`, out: `ACEScg`) around the grade.

Never apply more than one fix at a time without re-running the audit — fixes can cascade and uncover or hide downstream findings.

## Step 4 — Re-audit after fixes

After applying any fix, re-run `audit_acescct_consistency` with the same `scope` and confirm:

- The fixed finding is gone.
- No new errors appeared (fixes can occasionally surface a previously-masked issue).
- The `summary.errors` count strictly decreased.

If errors increased, revert the last fix — the user's chain depends on the original "wrong" colorspace tag in a way that breaks when corrected. Surface that to the user.

## Step 5 — Verify against a known-good frame

Once the audit returns clean, drop a `qc_viewer_pair` between the script's primary delivery write and a known-good reference (the editorial cut, a previous-version delivery, etc.). Differences should be at the floor of OCIO interpolation noise (~1e-4). Larger differences mean the audit missed something — surface this and ask for a manual look.

## Tools referenced

- `audit_acescct_consistency` (C9) — the audit itself.
- `set_knob` — apply per-node colorspace fixes.
- `qc_viewer_pair` — final verification against reference.

## Hard rules

- Never auto-fix without confirming with the user. Colorspace changes re-interpret pixels and can destroy hours of grade work if applied wrongly.
- Always re-audit after each fix — never batch-apply without verifying.
- If the OCIO config itself is wrong (script says `aces_1.0.3`, studio shipped `cg-config-v2.0.0`), do not change it — surface the mismatch and let the user / supervisor decide. Switching configs mid-shot is a project-level decision, not a per-script fix.
- The audit is read-only by design (`fix_suggestion` is advisory text, not an auto-apply hint). Stay aligned with that — never auto-apply.

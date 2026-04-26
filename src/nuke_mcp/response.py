"""Two-threshold response shaping with per-tool drop allowlists.

Ported from ``houdini-mcp-beta/houdini_mcp/tools/_common.py:948-1255``.
The shape of a tool response goes through three passes:

  1. ``_estimate_response_size`` -- a fast recursive byte-count, no real
     ``json.dumps``. Cheaper than full serialization since the MCP layer
     will re-serialize anyway.
  2. ``_truncate_response`` -- only fires when size > ``RESPONSE_SIZE_HARD``.
     Strategies, in order:
       a. Drop fields per the per-tool allowlist (knob-name globs included).
       b. Truncate long string values to ``MAX_STR_LEN`` chars with a
          ``"...<N chars>"`` suffix.
       c. Truncate ``menu_items`` arrays to first ``MAX_MENU_ITEMS`` with
          a ``"+N more"`` indicator.
       d. Recursive deepest-children drop.
       e. Digest fallback -- last resort, replace nested dicts with counts.
  3. ``_add_response_metadata`` -- always runs. Adds a ``_meta`` key with
     ``size_bytes`` (and, when truncated, ``truncated`` / ``digest_fallback``
     / ``drop_fields_applied``). Merges with any existing ``_meta`` so the
     A2 ``duration_ms`` / ``request_id`` survive.

``apply_response_shape`` is the single public entry point used by the
``nuke_command`` decorator.
"""

from __future__ import annotations

import fnmatch
import json
from typing import Any

# Two thresholds: a "warn" level (we just stamp the size) and a "hard"
# level (we truncate). Both in bytes of estimated JSON output.
RESPONSE_SIZE_WARN = 100_000  # 100 KB
RESPONSE_SIZE_HARD = 500_000  # 500 KB

MAX_STR_LEN = 200  # truncate string values to this many chars
MAX_MENU_ITEMS = 10  # truncate enum menu_items arrays to this length
_RECURSION_LIMIT = 50

# Per-tool drop / truncate config. Looked up by ``operation`` name in
# ``apply_response_shape``. Keys:
#   * ``knobs_skip``   -- glob-set of knob names to drop from any ``knobs`` dict.
#   * ``summary_fallback`` -- if size > warn after passes, retroactively
#     enable ``summary=True`` semantics by stripping ``knobs`` from every
#     node entry.
#   * ``strip_to``     -- when ``len(nodes) >= threshold_count``, keep only
#     these keys per node.
#   * ``threshold_count`` -- companion to ``strip_to``.
#   * ``truncate_str`` -- hard char limit for string values (override).
#   * ``menu_items``   -- hard length limit for menu_items arrays.
#   * ``max_count``    -- hard cap on a top-level list.
_UI_KNOBS_EXTENDED: set[str] = {
    "note_font*",
    "gl_color",
    "dope_sheet",
    "tile_color",
    "cached",
    "bookmark",
    "postage_stamp_*",
    "*_panelDropped",
    "lifetimeStart",
    "lifetimeEnd",
    "useLifetime",
    "indicators",
    "process_mask",
    "panel",
    "icon",
    "hide_input",
}

DROPS: dict[str, dict[str, Any]] = {
    "read_node_detail": {"knobs_skip": _UI_KNOBS_EXTENDED},
    "read_comp": {"knobs_skip": _UI_KNOBS_EXTENDED, "summary_fallback": True},
    "list_nodes": {
        "strip_to": ("name", "type", "error"),
        "threshold_count": 200,
    },
    "find_nodes": {"truncate_str": MAX_STR_LEN, "menu_items": MAX_MENU_ITEMS},
    "list_keyframes": {"truncate_str": MAX_STR_LEN, "max_count": 1000},
    "list_channels": {
        "truncate_str": MAX_STR_LEN,
        "menu_items": MAX_MENU_ITEMS,
    },
    "list_roto_shapes": {"max_count": 100},
    "diff_comp": {"truncate_str": 500},
    "snapshot_comp": {"truncate_str": 500},
}


# ---------------------------------------------------------------------------
# Size estimation
# ---------------------------------------------------------------------------


def _estimate_response_size(obj: Any) -> int:
    """Return the byte-count of ``json.dumps(obj, separators=(",", ":"))``.

    A real ``json.dumps`` is fine here: payloads are bounded (we don't
    estimate megabyte responses, we truncate them), and matching the MCP
    layer's own serialization eliminates the off-by-X% drift that a
    cheap structural estimator gives.
    """
    try:
        return len(json.dumps(obj, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return len(str(obj))


# ---------------------------------------------------------------------------
# Truncation passes
# ---------------------------------------------------------------------------


def _matches_glob(name: str, patterns: set[str]) -> bool:
    """True iff ``name`` matches any glob in ``patterns``."""
    for pat in patterns:
        if "*" in pat or "?" in pat or "[" in pat:
            if fnmatch.fnmatchcase(name, pat):
                return True
        elif name == pat:
            return True
    return False


def _drop_knobs_globbed(obj: Any, patterns: set[str], depth: int = 0) -> int:
    """Walk ``obj``; in every ``knobs`` dict, drop keys matching any pattern.

    Returns the count of dropped keys.
    """
    if depth >= _RECURSION_LIMIT:
        return 0
    dropped = 0
    if isinstance(obj, dict):
        knobs = obj.get("knobs")
        if isinstance(knobs, dict):
            for key in list(knobs.keys()):
                if _matches_glob(key, patterns):
                    knobs.pop(key, None)
                    dropped += 1
        for v in obj.values():
            dropped += _drop_knobs_globbed(v, patterns, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            dropped += _drop_knobs_globbed(item, patterns, depth + 1)
    return dropped


def _truncate_long_strings(obj: Any, limit: int, depth: int = 0) -> int:
    """Replace any string > ``limit`` with ``s[:limit] + '...<N chars>'``.

    Mutates dict / list values in place. Returns the count of truncations.
    """
    if depth >= _RECURSION_LIMIT:
        return 0
    truncated = 0
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str) and len(v) > limit:
                obj[k] = v[:limit] + f"...<{len(v)} chars>"
                truncated += 1
            else:
                truncated += _truncate_long_strings(v, limit, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str) and len(v) > limit:
                obj[i] = v[:limit] + f"...<{len(v)} chars>"
                truncated += 1
            else:
                truncated += _truncate_long_strings(v, limit, depth + 1)
    return truncated


def _truncate_menu_items(obj: Any, limit: int, depth: int = 0) -> int:
    """Trim every ``menu_items`` list (top-level or nested) to ``limit``.

    Adds a ``"+N more"`` sentinel when items are dropped. Returns the count
    of trimmed lists.
    """
    if depth >= _RECURSION_LIMIT:
        return 0
    trimmed = 0
    if isinstance(obj, dict):
        items = obj.get("menu_items")
        if isinstance(items, list) and len(items) > limit:
            extra = len(items) - limit
            obj["menu_items"] = [*items[:limit], f"+{extra} more"]
            trimmed += 1
        for v in obj.values():
            trimmed += _truncate_menu_items(v, limit, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            trimmed += _truncate_menu_items(item, limit, depth + 1)
    return trimmed


def _cap_list(obj: dict[str, Any], list_keys: tuple[str, ...], limit: int) -> bool:
    """Hard-cap one of the top-level list values. Returns True iff trimmed.

    Tries each key in ``list_keys`` in order; the first one whose value is
    a list longer than ``limit`` gets truncated and the function returns.
    """
    for key in list_keys:
        v = obj.get(key)
        if isinstance(v, list) and len(v) > limit:
            extra = len(v) - limit
            obj[key] = v[:limit]
            obj[f"_{key}_truncated"] = f"+{extra} more dropped"
            return True
    return False


def _strip_node_entries(obj: dict[str, Any], keep: tuple[str, ...]) -> int:
    """In a ``read_comp`` / ``list_nodes`` result, slim every node entry to ``keep``.

    Returns the count of nodes touched.
    """
    nodes = obj.get("nodes")
    if not isinstance(nodes, list):
        return 0
    touched = 0
    for i, n in enumerate(nodes):
        if isinstance(n, dict):
            stripped = {k: n[k] for k in keep if k in n}
            nodes[i] = stripped
            touched += 1
    return touched


def _strip_summary(obj: dict[str, Any]) -> int:
    """Strip ``knobs`` (and optionally ``inputs``) from every node entry.

    Two passes: first drop just ``knobs``. If the payload is still over the
    warn line, also drop ``inputs`` so a 2000-node fixture fits under
    100KB at name+type only.
    """
    nodes = obj.get("nodes")
    if not isinstance(nodes, list):
        return 0
    touched = 0
    for n in nodes:
        if isinstance(n, dict):
            n.pop("knobs", None)
            touched += 1
    if touched:
        obj["summary"] = True
    # Second pass: if still over warn, drop inputs too. This is the budget
    # threshold for the 2000-node fixture in the test suite.
    if _estimate_response_size(obj) > RESPONSE_SIZE_WARN:
        for n in nodes:
            if isinstance(n, dict):
                n.pop("inputs", None)
    return touched


def _digest_fallback(obj: dict[str, Any]) -> dict[str, Any]:
    """Last-resort summary: replace nested containers with counts.

    Keeps every primitive value at the top level. For dict / list values,
    write ``{"_count": N, "_type": "list" | "dict"}`` instead. Always fits
    under any reasonable budget.
    """
    digest: dict[str, Any] = {}
    for k, v in obj.items():
        if k == "_meta":
            continue
        if isinstance(v, list):
            digest[k] = {"_count": len(v), "_type": "list"}
        elif isinstance(v, dict):
            digest[k] = {"_count": len(v), "_type": "dict"}
        else:
            digest[k] = v
    return digest


def _truncate_response(
    obj: dict[str, Any],
    drops: dict[str, Any],
    *,
    hard: int = RESPONSE_SIZE_HARD,
    warn: int = RESPONSE_SIZE_WARN,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply truncation passes until ``obj`` fits under ``hard`` bytes.

    Returns ``(obj, meta)`` where ``meta`` carries:
      ``truncated``: bool
      ``digest_fallback``: bool
      ``drop_fields_applied``: list[str]
    """
    meta: dict[str, Any] = {
        "truncated": False,
        "digest_fallback": False,
        "drop_fields_applied": [],
    }

    if not isinstance(obj, dict):
        return obj, meta

    applied: list[str] = []

    # Pass 1 (always-on for the operation): drop globbed knobs. UI-only knobs
    # are noise no matter the payload size, so we run this unconditionally
    # whenever the operation has a ``knobs_skip`` allowlist configured.
    knobs_skip = drops.get("knobs_skip")
    if knobs_skip:
        dropped = _drop_knobs_globbed(obj, knobs_skip)
        if dropped:
            applied.append(f"knobs_skip:{dropped}")

    # Pass 2 (always-on): hard-cap a top-level list -- for list_keyframes,
    # list_roto_shapes -- to ``max_count``.
    max_count = drops.get("max_count")
    if max_count and _cap_list(obj, ("keyframes", "shapes", "items", "results"), max_count):
        applied.append(f"max_count:{max_count}")

    # Pass 3 (always-on if count is high): strip node entries to a tiny
    # subset for list_nodes >= 200.
    strip_to = drops.get("strip_to")
    threshold_count = drops.get("threshold_count")
    if strip_to and threshold_count:
        nodes = obj.get("nodes")
        if isinstance(nodes, list) and len(nodes) >= threshold_count:
            n = _strip_node_entries(obj, strip_to)
            if n:
                applied.append(f"strip_to:{n}")

    # The size-gated passes only fire if the payload is over the warn line
    # AFTER the always-on passes have done what they can.
    str_limit = drops.get("truncate_str", MAX_STR_LEN)
    if _estimate_response_size(obj) > warn:
        n = _truncate_long_strings(obj, str_limit)
        if n:
            applied.append(f"truncate_str:{n}")

    # menu_items pass: always-on when the operation explicitly configures
    # ``menu_items`` (find_nodes, list_channels). Big enum lists are noise
    # at any payload size for those tools.
    menu_limit_explicit = drops.get("menu_items")
    if menu_limit_explicit is not None:
        n = _truncate_menu_items(obj, menu_limit_explicit)
        if n:
            applied.append(f"menu_items:{n}")
    elif _estimate_response_size(obj) > warn:
        n = _truncate_menu_items(obj, MAX_MENU_ITEMS)
        if n:
            applied.append(f"menu_items:{n}")

    # Summary fallback: drop ``knobs`` from every node entry once we're
    # still over warn after the cheaper passes. read_comp opt-in.
    if drops.get("summary_fallback") and _estimate_response_size(obj) > warn:
        n = _strip_summary(obj)
        if n:
            applied.append(f"summary_fallback:{n}")

    final_size = _estimate_response_size(obj)

    # Last-resort: digest. Replace nested containers with counts.
    if final_size > hard:
        obj = _digest_fallback(obj)
        meta["digest_fallback"] = True
        applied.append("digest_fallback")

    if applied:
        meta["truncated"] = True
        meta["drop_fields_applied"] = applied

    return obj, meta


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def _add_response_metadata(
    obj: dict[str, Any],
    *,
    truncated: bool,
    size: int,
    digest_fallback: bool = False,
    drop_fields_applied: list[str] | None = None,
) -> dict[str, Any]:
    """Stamp ``_meta`` onto ``obj``. Merges into any pre-existing ``_meta``.

    The decorator in ``_helpers.py`` already adds ``duration_ms`` and
    ``request_id``; this MUST not clobber them.
    """
    existing = obj.get("_meta")
    meta: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    meta["size_bytes"] = size
    if truncated:
        meta["truncated"] = True
    if digest_fallback:
        meta["digest_fallback"] = True
    if drop_fields_applied:
        meta["drop_fields_applied"] = drop_fields_applied
    obj["_meta"] = meta
    return obj


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def apply_response_shape(obj: Any, operation: str) -> Any:
    """Estimate, truncate (if needed), stamp ``_meta``. Single funnel.

    Non-dict inputs pass through untouched. Unknown operations get the
    default truncation behavior (any payload is still capped at the hard
    threshold).
    """
    if not isinstance(obj, dict):
        return obj

    drops = DROPS.get(operation, {})
    size_before = _estimate_response_size(obj)

    truncated = False
    digest_fallback = False
    drop_fields_applied: list[str] = []

    # Run truncation if EITHER the size crossed the warn line (size-gated
    # passes will fire) OR the operation has any always-on rules (knob
    # globs, max_count caps). The inner passes already early-out on no-op.
    needs_pass = size_before > RESPONSE_SIZE_WARN or bool(drops)
    if needs_pass:
        obj, meta = _truncate_response(obj, drops)
        truncated = bool(meta.get("truncated"))
        digest_fallback = bool(meta.get("digest_fallback"))
        drop_fields_applied = list(meta.get("drop_fields_applied", []))

    final_size = _estimate_response_size(obj)
    return _add_response_metadata(
        obj,
        truncated=truncated,
        size=final_size,
        digest_fallback=digest_fallback,
        drop_fields_applied=drop_fields_applied,
    )

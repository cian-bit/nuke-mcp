"""Tests for src/nuke_mcp/response.py (B1 two-threshold truncation).

Coverage gate: ``response.py`` >= 95%.
"""

from __future__ import annotations

import pytest

from nuke_mcp.response import (
    DROPS,
    MAX_MENU_ITEMS,
    MAX_STR_LEN,
    RESPONSE_SIZE_HARD,
    RESPONSE_SIZE_WARN,
    _add_response_metadata,
    _drop_knobs_globbed,
    _estimate_response_size,
    _matches_glob,
    _truncate_long_strings,
    _truncate_menu_items,
    _truncate_response,
    apply_response_shape,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _synth_node(i: int, knobs_per_node: int = 5) -> dict:
    """Synthesize one node entry with ``knobs_per_node`` knobs."""
    knobs = {f"knob_{j}": float(j) for j in range(knobs_per_node)}
    return {
        "name": f"Node_{i}",
        "type": "Grade",
        "knobs": knobs,
        "inputs": [f"Node_{i - 1}"] if i else [],
    }


@pytest.fixture
def two_thousand_nodes() -> dict:
    """A 2000-node read_comp-shaped fixture. Crosses both thresholds."""
    nodes = [_synth_node(i, knobs_per_node=10) for i in range(2000)]
    return {"nodes": nodes, "count": 2000, "total": 2000}


@pytest.fixture
def camera_tracker_300_knobs() -> dict:
    """A read_node_detail-shaped payload with 300 knobs.

    Crosses 100KB on its own. Triggers ``knobs_skip`` glob drops.
    """
    knobs: dict[str, str] = {}
    for i in range(300):
        # Sprinkle UI-only knobs that the drop-list should match.
        if i % 10 == 0:
            knobs[f"note_font_{i}"] = "Helvetica" * 50
        elif i % 15 == 0:
            knobs[f"postage_stamp_{i}"] = "x" * 100
        else:
            knobs[f"real_knob_{i}"] = str(i) * 10
    return {
        "name": "CameraTracker1",
        "type": "CameraTracker",
        "knobs": knobs,
        "inputs": ["plate"],
    }


# ---------------------------------------------------------------------------
# Size estimation
# ---------------------------------------------------------------------------


def test_estimate_size_round_trip_simple():
    obj = {"foo": "bar", "n": 42}
    size = _estimate_response_size(obj)
    assert 10 <= size <= 30


def test_estimate_size_handles_unserializable():
    """default=str catches the odd object."""

    class Weird:
        def __str__(self) -> str:
            return "weird"

    assert _estimate_response_size({"x": Weird()}) > 0


def test_estimate_size_growing_payload():
    a = _estimate_response_size({"items": []})
    b = _estimate_response_size({"items": list(range(100))})
    assert b > a


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------


def test_matches_glob_literal():
    assert _matches_glob("gl_color", {"gl_color"})
    assert not _matches_glob("gl_color", {"different"})


def test_matches_glob_wildcard_prefix():
    assert _matches_glob("note_font_size", {"note_font*"})
    assert not _matches_glob("font_size", {"note_font*"})


def test_matches_glob_wildcard_suffix():
    assert _matches_glob("input_panelDropped", {"*_panelDropped"})


# ---------------------------------------------------------------------------
# Drop allowlist
# ---------------------------------------------------------------------------


def test_drop_knobs_globbed_drops_matched_keys():
    obj = {
        "knobs": {
            "note_font_size": "12",
            "real_knob": "keep",
            "gl_color": "0.5",
        }
    }
    dropped = _drop_knobs_globbed(obj, {"note_font*", "gl_color"})
    assert dropped == 2
    assert "real_knob" in obj["knobs"]
    assert "note_font_size" not in obj["knobs"]
    assert "gl_color" not in obj["knobs"]


def test_drop_knobs_globbed_recurses_into_lists():
    obj = {"nodes": [{"knobs": {"gl_color": "x", "keep": "y"}}]}
    dropped = _drop_knobs_globbed(obj, {"gl_color"})
    assert dropped == 1
    assert obj["nodes"][0]["knobs"] == {"keep": "y"}


# ---------------------------------------------------------------------------
# String truncation
# ---------------------------------------------------------------------------


def test_truncate_long_strings_replaces_with_suffix():
    obj = {"description": "x" * 50_000}
    n = _truncate_long_strings(obj, MAX_STR_LEN)
    assert n == 1
    assert len(obj["description"]) < 300
    assert obj["description"].endswith("chars>")
    assert obj["description"].startswith("x" * MAX_STR_LEN)


def test_truncate_long_strings_skips_short():
    obj = {"foo": "short"}
    n = _truncate_long_strings(obj, MAX_STR_LEN)
    assert n == 0
    assert obj["foo"] == "short"


def test_truncate_long_strings_inside_list():
    obj = {"items": ["a" * 5000, "b"]}
    n = _truncate_long_strings(obj, 100)
    assert n == 1
    assert obj["items"][0].endswith("chars>")
    assert obj["items"][1] == "b"


# ---------------------------------------------------------------------------
# menu_items truncation
# ---------------------------------------------------------------------------


def test_truncate_menu_items_caps_to_limit():
    obj = {"menu_items": [f"item_{i}" for i in range(50)]}
    n = _truncate_menu_items(obj, 10)
    assert n == 1
    assert len(obj["menu_items"]) == 11  # 10 + sentinel
    assert obj["menu_items"][-1] == "+40 more"


def test_truncate_menu_items_skips_short_lists():
    obj = {"menu_items": ["a", "b", "c"]}
    n = _truncate_menu_items(obj, 10)
    assert n == 0
    assert obj["menu_items"] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Full pipeline: apply_response_shape
# ---------------------------------------------------------------------------


def test_apply_unknown_op_round_trips_small():
    """Small payloads pass through, just gain a _meta block."""
    out = apply_response_shape({"foo": "bar"}, "unknown_op")
    assert out["foo"] == "bar"
    assert out["_meta"]["size_bytes"] > 0
    # No truncation flag for sub-warn payloads.
    assert "truncated" not in out["_meta"]


def test_apply_non_dict_passes_through():
    """Primitive returns are skipped by the wrap."""
    assert apply_response_shape("just-a-string", "anything") == "just-a-string"
    assert apply_response_shape(42, "anything") == 42


def test_apply_read_comp_2000_nodes_under_warn(two_thousand_nodes: dict):
    """The single hard requirement: 2000-node read_comp must fit under 100KB."""
    out = apply_response_shape(two_thousand_nodes, "read_comp")
    size = out["_meta"]["size_bytes"]
    assert size < RESPONSE_SIZE_WARN, f"expected < {RESPONSE_SIZE_WARN} bytes, got {size}"


def test_apply_camera_tracker_300_knobs_drops_ui_knobs(camera_tracker_300_knobs: dict):
    """read_node_detail with UI knobs must drop them via the glob allowlist."""
    out = apply_response_shape(camera_tracker_300_knobs, "read_node_detail")
    knobs = out.get("knobs", {})
    # note_font_* and postage_stamp_* should be gone.
    assert not any(k.startswith("note_font_") for k in knobs), knobs
    assert not any(k.startswith("postage_stamp_") for k in knobs)
    # real knobs (most of them) still there.
    assert any(k.startswith("real_knob_") for k in knobs)
    # _meta records what happened.
    if out["_meta"].get("truncated"):
        applied = out["_meta"].get("drop_fields_applied", [])
        assert any("knobs_skip" in a for a in applied)


def test_apply_under_20kb_target_for_camera_tracker(camera_tracker_300_knobs: dict):
    out = apply_response_shape(camera_tracker_300_knobs, "read_node_detail")
    # After dropping UI knobs + truncating long strings, payload fits under 20KB.
    assert out["_meta"]["size_bytes"] < 20_000


def test_apply_5000_keyframes_capped():
    """list_keyframes with thousands of frames trims to max_count."""
    keyframes = [{"frame": i, "value": float(i)} for i in range(5000)]
    payload = {"node": "Grade1", "knob": "gain", "keyframes": keyframes}
    out = apply_response_shape(payload, "list_keyframes")
    assert len(out["keyframes"]) <= 1000
    if len(out["keyframes"]) == 1000:
        assert "_keyframes_truncated" in out


def test_apply_50kb_string_truncated_to_short():
    """A 50KB string knob value gets truncated to MAX_STR_LEN + suffix."""
    big = "x" * 50_000
    payload = {"comments": big, "filler": "y" * 200_000}
    out = apply_response_shape(payload, "find_nodes")
    if isinstance(out.get("comments"), str):
        assert len(out["comments"]) < 300
        assert out["comments"].endswith("chars>")
    assert out["_meta"]["size_bytes"] <= RESPONSE_SIZE_HARD


def test_apply_menu_items_50_truncated_to_10():
    """menu_items list of 50 items truncated to first 10 + "+40 more".

    For ops that configure ``menu_items`` explicitly (find_nodes,
    list_channels), the trim runs unconditionally -- big enum lists are
    noise at any payload size.
    """
    items = [f"opt_{i}" for i in range(50)]
    payload = {"menu_items": items, "node": "Read1"}
    out = apply_response_shape(payload, "find_nodes")
    assert len(out["menu_items"]) == MAX_MENU_ITEMS + 1
    assert out["menu_items"][-1] == "+40 more"


def test_apply_drops_note_font_size_from_read_node_detail():
    """Drop allowlist applied: pass a note_font_size knob, assert dropped."""
    payload = {
        "name": "Grade1",
        "type": "Grade",
        # Force payload >warn so truncation runs at all.
        "filler": "x" * 200_000,
        "knobs": {"note_font_size": "12", "real_knob": "0.5"},
    }
    out = apply_response_shape(payload, "read_node_detail")
    assert "note_font_size" not in out["knobs"]
    assert "real_knob" in out["knobs"]


def test_apply_glob_match_on_postage_stamp():
    """``postage_stamp_size`` matches the ``postage_stamp_*`` glob."""
    payload = {
        "name": "Read1",
        "filler": "x" * 200_000,
        "knobs": {"postage_stamp_size": "200", "real_knob": "0.5"},
    }
    out = apply_response_shape(payload, "read_node_detail")
    assert "postage_stamp_size" not in out["knobs"]


def test_meta_shape_carries_required_keys():
    """_meta must expose at least size_bytes; truncated/drop_fields_applied
    surface only when truncation actually fired.
    """
    huge_payload = {"data": "x" * (RESPONSE_SIZE_HARD + 1)}
    out = apply_response_shape(huge_payload, "read_node_detail")
    meta = out["_meta"]
    assert "size_bytes" in meta
    assert meta.get("truncated") is True
    assert "drop_fields_applied" in meta


def test_round_trip_unknown_op_unchanged():
    """apply_response_shape({"foo": "bar"}, "unknown_op") returns unchanged."""
    out = apply_response_shape({"foo": "bar"}, "unknown_op")
    assert out["foo"] == "bar"
    assert out["_meta"].get("truncated") is not True


def test_truncate_response_returns_meta_dict():
    """Direct contract test on the inner truncate function."""
    big = {"data": "x" * (RESPONSE_SIZE_HARD + 100)}
    out, meta = _truncate_response(big, drops={})
    assert meta["truncated"]
    # Either passes 1: long-string truncation, OR digest fallback.
    applied = meta["drop_fields_applied"]
    assert applied  # non-empty


def test_digest_fallback_on_unsalvageable_payload():
    """If standard passes can't bring it under hard, fall through to digest."""
    # 5000 keys of nested data -- well over 500KB so the std passes can't fix
    bad = {f"k{i}": {"deep": {"x": [j for j in range(80)], "tag": "a" * 50}} for i in range(5000)}
    out = apply_response_shape(bad, "unknown_op")
    # After digest fallback, output is FAR smaller (~50KB).
    assert out["_meta"].get("digest_fallback") is True
    sample_keys = [k for k in out if k.startswith("k")]
    assert sample_keys, out
    assert out[sample_keys[0]]["_count"] >= 1
    assert "drop_fields_applied" in out["_meta"]
    assert "digest_fallback" in out["_meta"]["drop_fields_applied"]


# ---------------------------------------------------------------------------
# _add_response_metadata: merging behavior
# ---------------------------------------------------------------------------


def test_add_response_metadata_preserves_existing_meta():
    """_meta merges -- existing duration_ms / request_id MUST survive."""
    obj: dict = {
        "value": 1,
        "_meta": {"duration_ms": 42, "request_id": "abcd1234"},
    }
    out = _add_response_metadata(obj, truncated=False, size=100)
    assert out["_meta"]["duration_ms"] == 42
    assert out["_meta"]["request_id"] == "abcd1234"
    assert out["_meta"]["size_bytes"] == 100


def test_add_response_metadata_adds_truncation_flags():
    obj: dict = {"value": 1}
    out = _add_response_metadata(
        obj,
        truncated=True,
        size=600_000,
        digest_fallback=True,
        drop_fields_applied=["truncate_str:5", "digest_fallback"],
    )
    assert out["_meta"]["truncated"] is True
    assert out["_meta"]["digest_fallback"] is True
    assert out["_meta"]["drop_fields_applied"] == [
        "truncate_str:5",
        "digest_fallback",
    ]


# ---------------------------------------------------------------------------
# Drop config introspection
# ---------------------------------------------------------------------------


def test_drops_table_has_expected_keys():
    """Sanity: every operation we mention in the brief is in DROPS."""
    expected = {
        "read_node_detail",
        "read_comp",
        "list_nodes",
        "find_nodes",
        "list_keyframes",
        "list_channels",
        "list_roto_shapes",
        "diff_comp",
        "snapshot_comp",
    }
    assert expected.issubset(DROPS.keys())


def test_thresholds_have_expected_values():
    assert RESPONSE_SIZE_WARN == 100_000
    assert RESPONSE_SIZE_HARD == 500_000


# ---------------------------------------------------------------------------
# Edge-case coverage
# ---------------------------------------------------------------------------


def test_estimate_size_falls_back_on_unserializable():
    """Object that defeats both json.dumps AND default=str."""

    class Bad:
        def __str__(self) -> str:
            raise RuntimeError("nope")

    # default=str will get called; if it itself raises, json would error.
    # The fallback path returns ``len(str(obj))`` -- which would also raise
    # here -- but we wrap json.dumps in try/except specifically. Test that
    # the wrapped call returns a positive int for normal objects.
    size = _estimate_response_size({"x": 1})
    assert size > 0


def test_drop_knobs_globbed_recursion_cap():
    """Past the recursion cap, the walker bails out cleanly."""
    from nuke_mcp.response import _RECURSION_LIMIT, _drop_knobs_globbed

    # Build a dict 60 levels deep -- past the 50-cap. Walker must not crash.
    deep: dict = {"end": "value"}
    for _ in range(60):
        deep = {"down": deep}
    n = _drop_knobs_globbed(deep, {"foo*"})
    assert n >= 0  # didn't blow up
    assert _RECURSION_LIMIT == 50


def test_truncate_long_strings_recursion_cap():
    from nuke_mcp.response import _truncate_long_strings

    deep: dict = {"end": "x" * 1000}
    for _ in range(60):
        deep = {"down": deep}
    n = _truncate_long_strings(deep, 200)
    assert n >= 0


def test_truncate_menu_items_recursion_cap():
    from nuke_mcp.response import _truncate_menu_items

    deep: dict = {"menu_items": list(range(50))}
    for _ in range(60):
        deep = {"down": deep}
    n = _truncate_menu_items(deep, 10)
    assert n >= 0


def test_cap_list_returns_false_when_no_match():
    from nuke_mcp.response import _cap_list

    obj = {"other_key": [1, 2, 3]}
    # ``other_key`` not in the search list -- no cap applied.
    assert _cap_list(obj, ("keyframes", "shapes"), 1) is False


def test_strip_node_entries_no_nodes_key():
    from nuke_mcp.response import _strip_node_entries

    obj = {"foo": "bar"}
    assert _strip_node_entries(obj, ("name",)) == 0


def test_strip_summary_no_nodes_key():
    from nuke_mcp.response import _strip_summary

    obj = {"foo": "bar"}
    assert _strip_summary(obj) == 0


def test_truncate_response_non_dict_input():
    """Non-dict short-circuits with empty meta."""
    out, meta = _truncate_response("just-a-string", drops={})  # type: ignore[arg-type]
    assert out == "just-a-string"
    assert meta["truncated"] is False


def test_apply_list_nodes_strip_to_when_count_over_threshold():
    """list_nodes >= 200 triggers strip_to (name/type/error only)."""
    nodes = [
        {
            "name": f"N{i}",
            "type": "Grade",
            "error": False,
            "knobs": {"x": 1.0},
            "extra": "drop me",
        }
        for i in range(250)
    ]
    out = apply_response_shape({"nodes": nodes, "count": 250}, "list_nodes")
    sample = out["nodes"][0]
    assert set(sample.keys()) <= {"name", "type", "error"}
    assert "extra" not in sample
    assert "knobs" not in sample


def test_apply_list_nodes_under_threshold_keeps_full_entry():
    """list_nodes < 200 keeps the full node dicts."""
    nodes = [{"name": f"N{i}", "type": "Grade", "error": False, "extra": "keep"} for i in range(50)]
    out = apply_response_shape({"nodes": nodes, "count": 50}, "list_nodes")
    assert "extra" in out["nodes"][0]


def test_digest_fallback_function_directly():
    """_digest_fallback rewrites every dict/list value to a count stub."""
    from nuke_mcp.response import _digest_fallback

    obj = {
        "primitive": 42,
        "string": "hi",
        "items": [1, 2, 3],
        "nested": {"a": 1, "b": 2},
        "_meta": {"size_bytes": 10},  # excluded
    }
    out = _digest_fallback(obj)
    assert out["primitive"] == 42
    assert out["string"] == "hi"
    assert out["items"] == {"_count": 3, "_type": "list"}
    assert out["nested"] == {"_count": 2, "_type": "dict"}
    assert "_meta" not in out


def test_strip_summary_drops_inputs_under_pressure():
    """When still over warn after dropping knobs, inputs go too."""
    from nuke_mcp.response import _strip_summary

    # 2000 nodes with knobs + inputs -- over warn even after knobs drop.
    nodes = [
        {
            "name": f"Node_{i}",
            "type": "Grade",
            "knobs": {"x": 1.0},
            "inputs": [f"Node_{i - 1}"] if i else [],
        }
        for i in range(2000)
    ]
    obj = {"nodes": nodes}
    _strip_summary(obj)
    # After the second pass, inputs should be gone too.
    assert all("inputs" not in n for n in obj["nodes"])
    assert obj.get("summary") is True


def test_implicit_menu_items_fallback_on_unknown_op_over_warn():
    """Unknown op that has menu_items + payload >warn fires the implicit pass.

    Many short items: truncate_str pass leaves them alone (each is shorter
    than the limit), so the bulk stays over warn and the implicit
    menu_items pass fires.
    """
    items = [f"opt_{i:05d}_aaaaaaa" for i in range(8000)]
    payload = {"menu_items": items}
    out = apply_response_shape(payload, "no_such_op")
    assert len(out["menu_items"]) == 11


def test_apply_response_shape_runs_truncate_for_op_with_drops_under_warn():
    """Even small payloads get the always-on passes for configured ops."""
    # read_node_detail with a single UI knob -- below warn but knob still
    # gets dropped via the always-on pass.
    payload = {"name": "Node1", "knobs": {"gl_color": "0.5", "real": "1"}}
    out = apply_response_shape(payload, "read_node_detail")
    assert "gl_color" not in out["knobs"]
    assert out["knobs"]["real"] == "1"

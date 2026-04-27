"""Tests for the C9 audit + QC tools.

Exercises every audit on synthetic bad/good input, verifies the
``$SS`` env-expansion path (set + missing), case-sensitive vs
insensitive naming, render-settings drift, and the ``qc_viewer_pair``
Switch + Merge(diff) + Grade(gain=10) chain construction.

The ``audit_acescct_consistency`` graceful-degradation path is also
asserted -- when ``tools.color`` is absent (the C2 parallel branch
hasn't merged) the wrapper returns the documented degraded shape
without ever hitting the wire.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from nuke_mcp.tools import audit


class _StubMCP:
    """Captures registered tool callables (mirror of test_tracking.py)."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.registered[func.__name__] = func
            return func

        return decorator


class _StubCtx:
    def __init__(self) -> None:
        self.mcp = _StubMCP()
        self.version = None
        self.mock = True


@pytest.fixture
def audit_tools(mock_script):
    """Register audit tools against a connected mock server.

    Seeds a Read node so qc_viewer_pair has a reference point.
    """
    server, script = mock_script
    server.nodes["ss_plate"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["ss_plate"] = []
    ctx = _StubCtx()
    audit.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# audit_write_paths
# ---------------------------------------------------------------------------


def test_audit_write_paths_flags_offending_node(audit_tools, monkeypatch, tmp_path):
    """Write nodes outside ``$SS`` produce an error finding."""
    server, _script, tools = audit_tools
    monkeypatch.setenv("SS", str(tmp_path))
    server.nodes["bad_write"] = {
        "type": "Write",
        "knobs": {"file": "C:/randomdir/foo.exr"},
        "x": 0,
        "y": 0,
    }
    server.connections["bad_write"] = []
    result = tools["audit_write_paths"]()
    assert result.get("status") != "error", result
    findings = result["findings"]
    bad = [f for f in findings if f["node"] == "bad_write"]
    assert len(bad) == 1
    assert bad[0]["severity"] == "error"
    assert bad[0]["path"] == "C:/randomdir/foo.exr"
    assert bad[0]["fix_suggestion"] is not None


def test_audit_write_paths_clean_on_allow_listed_path(audit_tools, monkeypatch, tmp_path):
    """A Write under ``$SS`` produces no error finding for that node."""
    server, _script, tools = audit_tools
    monkeypatch.setenv("SS", str(tmp_path))
    target = str(tmp_path / "renders" / "shot.####.exr")
    server.nodes["good_write"] = {
        "type": "Write",
        "knobs": {"file": target},
        "x": 0,
        "y": 0,
    }
    server.connections["good_write"] = []
    result = tools["audit_write_paths"]()
    bad = [f for f in result["findings"] if f["node"] == "good_write"]
    assert bad == []


def test_audit_write_paths_missing_env_var_emits_info(audit_tools, monkeypatch):
    """Unset ``$SS`` produces an info finding (and skips that root)."""
    server, _script, tools = audit_tools
    monkeypatch.delenv("SS", raising=False)
    server.nodes["w1"] = {
        "type": "Write",
        "knobs": {"file": "C:/elsewhere/foo.exr"},
        "x": 0,
        "y": 0,
    }
    server.connections["w1"] = []
    result = tools["audit_write_paths"]()
    info = [f for f in result["findings"] if f["severity"] == "info"]
    assert any("SS" in f["message"] for f in info)


def test_audit_write_paths_explicit_allow_roots(audit_tools, tmp_path):
    """Explicit ``allow_roots`` list overrides the default ``["$SS"]``."""
    server, _script, tools = audit_tools
    target = str(tmp_path / "out.exr")
    server.nodes["w_explicit"] = {
        "type": "Write",
        "knobs": {"file": target},
        "x": 0,
        "y": 0,
    }
    server.connections["w_explicit"] = []
    result = tools["audit_write_paths"](allow_roots=[str(tmp_path)])
    bad = [f for f in result["findings"] if f["node"] == "w_explicit"]
    assert bad == []


def test_audit_write_paths_empty_path_is_warning(audit_tools, monkeypatch, tmp_path):
    """A Write with an empty file knob is flagged as a warning."""
    server, _script, tools = audit_tools
    monkeypatch.setenv("SS", str(tmp_path))
    server.nodes["w_empty"] = {
        "type": "Write",
        "knobs": {"file": ""},
        "x": 0,
        "y": 0,
    }
    server.connections["w_empty"] = []
    result = tools["audit_write_paths"]()
    bad = [f for f in result["findings"] if f["node"] == "w_empty"]
    assert len(bad) == 1
    assert bad[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# audit_naming_convention
# ---------------------------------------------------------------------------


def test_audit_naming_convention_flags_misnamed(audit_tools):
    """Nodes without the prefix get a warning finding."""
    server, _script, tools = audit_tools
    server.nodes["BadName"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["BadName"] = []
    result = tools["audit_naming_convention"]()
    flagged = [f for f in result["findings"] if f["node"] == "BadName"]
    assert len(flagged) == 1
    assert flagged[0]["severity"] == "warning"
    assert "ss_" in flagged[0]["fix_suggestion"]


def test_audit_naming_convention_case_sensitive(audit_tools):
    """Default case-sensitive comparison flags an upper-case prefix."""
    server, _script, tools = audit_tools
    server.nodes["SS_Wrong"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["SS_Wrong"] = []
    result = tools["audit_naming_convention"]()
    flagged = [f for f in result["findings"] if f["node"] == "SS_Wrong"]
    assert len(flagged) == 1


def test_audit_naming_convention_case_insensitive(audit_tools):
    """``case_sensitive=False`` accepts ``SS_Wrong`` against prefix ``ss_``."""
    server, _script, tools = audit_tools
    server.nodes["SS_OK"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["SS_OK"] = []
    result = tools["audit_naming_convention"](case_sensitive=False)
    flagged = [f for f in result["findings"] if f["node"] == "SS_OK"]
    assert flagged == []


def test_audit_naming_convention_clean_when_prefixed(audit_tools):
    """A correctly-named node produces no finding."""
    server, _script, tools = audit_tools
    server.nodes["ss_grade1"] = {"type": "Grade", "knobs": {}, "x": 0, "y": 0}
    server.connections["ss_grade1"] = []
    result = tools["audit_naming_convention"]()
    flagged = [f for f in result["findings"] if f["node"] == "ss_grade1"]
    assert flagged == []


# ---------------------------------------------------------------------------
# audit_render_settings
# ---------------------------------------------------------------------------


def test_audit_render_settings_fps_mismatch(audit_tools):
    """fps drift produces one error finding on ``__root__``."""
    server, _script, tools = audit_tools
    server.script_info["fps"] = 25.0
    result = tools["audit_render_settings"](expected_fps=24.0)
    fps_findings = [f for f in result["findings"] if "fps" in f["message"].lower()]
    assert len(fps_findings) == 1
    assert fps_findings[0]["severity"] == "error"
    assert fps_findings[0]["node"] == "__root__"


def test_audit_render_settings_format_mismatch(audit_tools):
    """Format mismatch produces an error finding."""
    server, _script, tools = audit_tools
    server.script_info["format"] = "HD 1920x1080"
    result = tools["audit_render_settings"](expected_format="2048x1080")
    fmt = [f for f in result["findings"] if "format" in f["message"].lower()]
    assert len(fmt) == 1
    assert fmt[0]["severity"] == "error"


def test_audit_render_settings_range_none_skips(audit_tools):
    """``expected_range=None`` (default) skips the frame-range check entirely."""
    server, _script, tools = audit_tools
    server.script_info["fps"] = 24.0
    server.script_info["format"] = "2K_DCP 2048x1080"
    server.script_info["first_frame"] = 999
    server.script_info["last_frame"] = 9999
    result = tools["audit_render_settings"](expected_fps=24.0, expected_format="2048x1080")
    range_findings = [f for f in result["findings"] if "range" in f["message"].lower()]
    assert range_findings == []


def test_audit_render_settings_range_mismatch(audit_tools):
    """When ``expected_range`` is given, drift produces an error finding."""
    server, _script, tools = audit_tools
    server.script_info["fps"] = 24.0
    server.script_info["format"] = "2K_DCP 2048x1080"
    server.script_info["first_frame"] = 1001
    server.script_info["last_frame"] = 1100
    result = tools["audit_render_settings"](
        expected_fps=24.0,
        expected_format="2048x1080",
        expected_range=(1001, 1200),
    )
    range_findings = [f for f in result["findings"] if "range" in f["message"].lower()]
    assert len(range_findings) == 1
    assert range_findings[0]["severity"] == "error"


def test_audit_render_settings_clean_when_aligned(audit_tools):
    """No findings when fps + format + range all match."""
    server, _script, tools = audit_tools
    server.script_info["fps"] = 24.0
    server.script_info["format"] = "2K_DCP 2048x1080 1.0"
    server.script_info["first_frame"] = 1001
    server.script_info["last_frame"] = 1100
    result = tools["audit_render_settings"](
        expected_fps=24.0,
        expected_format="2048x1080",
        expected_range=(1001, 1100),
    )
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# qc_viewer_pair
# ---------------------------------------------------------------------------


def test_qc_viewer_pair_creates_switch_and_diff_chain(audit_tools):
    """qc_viewer_pair creates Switch + Merge(diff) + Grade and returns NodeRef."""
    server, _script, tools = audit_tools
    server.nodes["ss_recombined"] = {
        "type": "Read",
        "knobs": {},
        "x": 0,
        "y": 0,
    }
    server.connections["ss_recombined"] = []
    result = tools["qc_viewer_pair"]("ss_plate", "ss_recombined")
    assert result.get("status") != "error", result
    assert result["type"] == "Switch"

    # The diff chain landed in the mock node graph.
    types = {n: d["type"] for n, d in server.nodes.items()}
    assert "Merge2" in types.values()
    assert "Grade" in types.values()
    assert "Switch" in types.values()

    # Switch wires both inputs + the diff branch.
    switch_inputs = result["inputs"]
    assert "ss_plate" in switch_inputs
    assert "ss_recombined" in switch_inputs
    # Third input is the Grade tail of the diff branch.
    grade_names = [n for n, t in types.items() if t == "Grade"]
    assert grade_names
    assert grade_names[0] in switch_inputs


def test_qc_viewer_pair_unknown_beauty(audit_tools):
    """Unknown beauty node returns an error result."""
    _server, _script, tools = audit_tools
    result = tools["qc_viewer_pair"]("does_not_exist", "ss_plate")
    assert result.get("status") == "error"


# ---------------------------------------------------------------------------
# audit_acescct_consistency -- graceful degradation
# ---------------------------------------------------------------------------


def test_audit_acescct_consistency_graceful_when_c2_absent(audit_tools, monkeypatch):
    """Without ``tools.color``, the wrapper returns the documented degraded shape."""
    # Belt-and-braces: ensure the colour module isn't importable for this test.
    monkeypatch.setitem(sys.modules, "nuke_mcp.tools.color", None)
    _server, _script, tools = audit_tools
    result = tools["audit_acescct_consistency"]()
    assert result.get("status") != "error", result
    assert result["findings"] == []
    assert "C2" in result["note"] or "color" in result["note"].lower()


def test_audit_acescct_consistency_delegates_when_c2_present(audit_tools, monkeypatch):
    """When ``tools.color.audit_acescct_consistency`` exists, the wrapper delegates."""
    import types

    fake_color = types.ModuleType("nuke_mcp.tools.color")
    sentinel = {
        "findings": [{"severity": "info", "node": "Read1", "message": "ok", "fix_suggestion": None}]
    }

    def _fake_audit() -> dict:
        return sentinel

    fake_color.audit_acescct_consistency = _fake_audit  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nuke_mcp.tools.color", fake_color)

    _server, _script, tools = audit_tools
    result = tools["audit_acescct_consistency"]()
    # The wire round-trip stamps _meta -- compare on findings/note only.
    assert result.get("findings") == sentinel["findings"]
    assert "note" not in result

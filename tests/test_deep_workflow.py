"""Tests for deep_workflow.py.

C6 layered the ``setup_flip_blood_comp`` macro on top of the C1 atomic
deep primitives. The tests pin:

* the pipeline shape -- DeepRecolor + DeepHoldout(2) + DeepMerge +
  DeepToImage + Grade + ZDefocus end up under a Group;
* ``blood_tint`` propagates onto the Grade's ``multiply`` knob;
* ``motion`` toggles a VectorBlur in/out of the chain;
* ``holdout_roto`` toggles between the supplied roto and a default
  fallback for the holdout slot;
* the ZDefocus knob trio (``math=depth``, ``depth_channel=deep.front``,
  AA-on-depth disabled) is hardcoded -- callers can't override;
* the macro is idempotent on ``name=``;
* the Group name is derived from ``$SS_SHOT`` (or the script stem).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from nuke_mcp.tools import deep_workflow


def test_module_importable() -> None:
    """Module is importable and exposes register()."""
    assert deep_workflow is not None
    assert hasattr(deep_workflow, "register")


# ---------------------------------------------------------------------------
# Stub MCP infrastructure (mirrors test_deep.py)
# ---------------------------------------------------------------------------


class _StubMCP:
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
def workflow_tools(mock_script):
    """Register deep_workflow tools against a connected mock server.

    Seeds a beauty Read, a deep render, a holdout roto, and a motion
    vector source so the macro has every plausible input wired.
    """
    server, script = mock_script
    server.nodes["beauty"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["beauty"] = []
    server.nodes["deep_pass"] = {"type": "DeepRead", "knobs": {}, "x": 0, "y": 0}
    server.connections["deep_pass"] = []
    server.nodes["holdoutRoto"] = {"type": "Roto", "knobs": {}, "x": 0, "y": 0}
    server.connections["holdoutRoto"] = []
    server.nodes["motionVecs"] = {"type": "Read", "knobs": {}, "x": 0, "y": 0}
    server.connections["motionVecs"] = []
    ctx = _StubCtx()
    deep_workflow.register(ctx)
    return server, script, ctx.mcp.registered


# ---------------------------------------------------------------------------
# Signature pin
# ---------------------------------------------------------------------------


def test_setup_flip_blood_comp_signature(workflow_tools) -> None:
    """The macro's leading params match the C6 contract."""
    _server, _script, tools = workflow_tools
    fn = tools.get("setup_flip_blood_comp")
    assert fn is not None, "setup_flip_blood_comp not registered"
    sig = inspect.signature(fn)
    params = tuple(p.name for p in sig.parameters.values())
    assert params[:2] == ("beauty", "deep_pass")
    # All other contracted params are present.
    for required in ("motion", "holdout_roto", "blood_tint", "name"):
        assert required in params, f"missing param: {required}"


# ---------------------------------------------------------------------------
# Default-args happy path
# ---------------------------------------------------------------------------


def test_default_args_produce_full_pipeline(workflow_tools, monkeypatch):
    """Default args -> Recolor + Holdout(2) + Merge + Flatten + Grade + ZDefocus
    inside a Group; no VectorBlur because motion is None."""
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    server, _script, tools = workflow_tools
    result = tools["setup_flip_blood_comp"]("beauty", "deep_pass")
    assert isinstance(result, dict)
    assert result.get("status") != "error"
    assert result["group"] == "FLIP_Blood_ss_0170"
    assert server.nodes[result["group"]]["type"] == "Group"
    # All the structural members exist on the mock.
    assert server.nodes[result["recolor"]]["type"] == "DeepRecolor"
    assert server.nodes[result["holdout"]]["type"] == "DeepHoldout2"
    assert server.nodes[result["merge"]]["type"] == "DeepMerge"
    assert server.nodes[result["flatten"]]["type"] == "DeepToImage"
    assert server.nodes[result["grade"]]["type"] == "Grade"
    assert server.nodes[result["zdefocus"]]["type"] == "ZDefocus2"
    # No VectorBlur on the default path.
    assert result["vector_blur"] is None
    # Each member node carries the group as its parent.
    for member in ("recolor", "holdout", "merge", "flatten", "grade", "zdefocus"):
        assert server.nodes[result[member]]["knobs"].get("group") == result["group"]
    # The orchestrator composes the C1 sub-handlers -- check the wire log.
    cmds = [cmd for cmd, _params in server.typed_calls]
    for sub in (
        "setup_flip_blood_comp",
        "create_deep_recolor",
        "create_deep_holdout",
        "create_deep_merge",
        "deep_to_image",
    ):
        assert sub in cmds, f"sub-handler not invoked: {sub}"


# ---------------------------------------------------------------------------
# blood_tint -> Grade.multiply
# ---------------------------------------------------------------------------


def test_blood_tint_propagates_to_grade(workflow_tools, monkeypatch):
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    server, _script, tools = workflow_tools
    tint = (0.42, 0.05, 0.07)
    result = tools["setup_flip_blood_comp"]("beauty", "deep_pass", blood_tint=tint)
    grade_knobs = server.nodes[result["grade"]]["knobs"]
    assert grade_knobs["multiply"] == [0.42, 0.05, 0.07]


# ---------------------------------------------------------------------------
# motion toggles VectorBlur
# ---------------------------------------------------------------------------


def test_motion_none_skips_vector_blur(workflow_tools, monkeypatch):
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    server, _script, tools = workflow_tools
    result = tools["setup_flip_blood_comp"]("beauty", "deep_pass", motion=None)
    assert result["vector_blur"] is None
    vblur_nodes = [n for n, d in server.nodes.items() if d["type"] == "VectorBlur"]
    assert vblur_nodes == []


def test_motion_provided_wires_vector_blur(workflow_tools, monkeypatch):
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    server, _script, tools = workflow_tools
    result = tools["setup_flip_blood_comp"]("beauty", "deep_pass", motion="motionVecs")
    assert result["vector_blur"] is not None
    vblur = server.nodes[result["vector_blur"]]
    assert vblur["type"] == "VectorBlur"
    # VectorBlur sits between the OCIO-out and the ZDefocus, fed by an
    # internal motion Input on slot 1.
    inputs = server.connections.get(result["vector_blur"], [])
    assert len(inputs) >= 2
    # Slot 1 is the motion source -- mock wires the internal Input
    # placeholder, whose knob points back at the original motion node.
    motion_input_name = inputs[1]
    assert server.nodes[motion_input_name]["type"] == "Input"
    # Group's external input slot picks up the motion node directly.
    group_inputs = server.connections.get(result["group"], [])
    assert "motionVecs" in group_inputs


# ---------------------------------------------------------------------------
# holdout_roto -> default vs supplied
# ---------------------------------------------------------------------------


def test_holdout_roto_none_uses_default(workflow_tools, monkeypatch):
    """``holdout_roto=None`` -> DeepHoldout's holdout side is the recolor
    itself (a no-op holdout). No external Input added for it."""
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    server, _script, tools = workflow_tools
    result = tools["setup_flip_blood_comp"]("beauty", "deep_pass", holdout_roto=None)
    holdout_inputs = server.connections.get(result["holdout"], [])
    # Subject and holdout both point at the recolor when no roto supplied.
    assert holdout_inputs[0] == result["recolor"]
    assert holdout_inputs[1] == result["recolor"]
    # Group's external slot 2 isn't bound when holdout_roto is None.
    group_inputs = server.connections.get(result["group"], [])
    assert "holdoutRoto" not in group_inputs


def test_holdout_roto_provided_wires_roto(workflow_tools, monkeypatch):
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    server, _script, tools = workflow_tools
    result = tools["setup_flip_blood_comp"]("beauty", "deep_pass", holdout_roto="holdoutRoto")
    # Group external input slot 2 is the holdout roto.
    group_inputs = server.connections.get(result["group"], [])
    assert group_inputs[0] == "deep_pass"
    assert group_inputs[1] == "beauty"
    assert "holdoutRoto" in group_inputs
    # Internally the holdout's slot-1 input is an Input node, not the
    # recolor (which would be the no-op fallback).
    holdout_inputs = server.connections.get(result["holdout"], [])
    assert holdout_inputs[0] == result["recolor"]
    holdout_input_node = server.nodes[holdout_inputs[1]]
    assert holdout_input_node["type"] == "Input"


# ---------------------------------------------------------------------------
# ZDefocus knob trio (Foundry rule)
# ---------------------------------------------------------------------------


def test_zdefocus_knob_constraints(workflow_tools, monkeypatch):
    """ZDefocus must carry math=depth, depth_channel=deep.front, AA-off."""
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    server, _script, tools = workflow_tools
    result = tools["setup_flip_blood_comp"]("beauty", "deep_pass")
    knobs = server.nodes[result["zdefocus"]]["knobs"]
    assert knobs["math"] == "depth"
    assert knobs["depth_channel"] == "deep.front"
    # Both the legacy and ZDefocus2 spelling of the depth-AA toggle
    # must be off; the addon flips whichever knob the live build
    # exposes.
    assert knobs["aa"] is False
    assert knobs["depth_aa"] is False


# ---------------------------------------------------------------------------
# Idempotency on name=
# ---------------------------------------------------------------------------


def test_idempotent_on_name(workflow_tools, monkeypatch):
    """Two calls with the same ``name`` -> second returns the first
    Group's payload without creating duplicate members."""
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    server, _script, tools = workflow_tools
    first = tools["setup_flip_blood_comp"]("beauty", "deep_pass", name="bloodFX")
    second = tools["setup_flip_blood_comp"]("beauty", "deep_pass", name="bloodFX")
    assert first["group"] == second["group"] == "bloodFX"
    # Exactly one Group, one DeepRecolor, etc.
    for node_class in ("Group", "DeepRecolor", "DeepHoldout2", "DeepMerge", "Grade", "ZDefocus2"):
        matching = [n for n, d in server.nodes.items() if d["type"] == node_class]
        assert (
            len(matching) == 1
        ), f"idempotency drift: {len(matching)} {node_class}(s) after re-call"


# ---------------------------------------------------------------------------
# Group naming: SS_SHOT vs script stem
# ---------------------------------------------------------------------------


def test_group_name_uses_ss_shot_env(workflow_tools, monkeypatch):
    monkeypatch.setenv("SS_SHOT", "ss_0042")
    _server, _script, tools = workflow_tools
    result = tools["setup_flip_blood_comp"]("beauty", "deep_pass")
    assert result["group"] == "FLIP_Blood_ss_0042"


def test_group_name_falls_back_to_script_stem(workflow_tools, monkeypatch):
    """No ``SS_SHOT`` -> derive shot from the script's basename stem."""
    monkeypatch.delenv("SS_SHOT", raising=False)
    server, _script, tools = workflow_tools
    server.script_info["script"] = "/jobs/fmp/comp/ss_0170_blood_v003.nk"
    result = tools["setup_flip_blood_comp"]("beauty", "deep_pass")
    assert result["group"] == "FLIP_Blood_ss_0170_blood_v003"


def test_group_name_explicit_overrides_shot(workflow_tools, monkeypatch):
    """Explicit ``name=`` wins over both env-var and script stem."""
    monkeypatch.setenv("SS_SHOT", "ss_0170")
    _server, _script, tools = workflow_tools
    result = tools["setup_flip_blood_comp"]("beauty", "deep_pass", name="myMacro")
    assert result["group"] == "myMacro"

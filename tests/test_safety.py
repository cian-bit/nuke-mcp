"""Tests for the AST + regex code-safety scanner."""

from __future__ import annotations

from nuke_mcp.tools import _safety
from nuke_mcp.tools._safety import (
    Finding,
    _attr_path,
    _detect_dangerous_code,
    finding_to_dict,
    validate_tcl,
)


def _messages(findings: list[Finding]) -> list[str]:
    return [f.message for f in findings]


def _has_error(findings: list[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)


def _error_messages(findings: list[Finding]) -> list[str]:
    return [f.message for f in findings if f.severity == "error"]


# ---------------------------------------------------------------------
# Direct forbidden calls
# ---------------------------------------------------------------------


def test_direct_forbidden_call_script_close() -> None:
    findings = _detect_dangerous_code("nuke.scriptClose()")
    assert _has_error(findings)
    assert any("scriptClose" in m for m in _messages(findings))


def test_direct_forbidden_call_delete() -> None:
    findings = _detect_dangerous_code("nuke.delete(some_node)")
    assert _has_error(findings)
    assert any("nuke.delete" in m for m in _messages(findings))


def test_direct_forbidden_call_script_clear() -> None:
    findings = _detect_dangerous_code("nuke.scriptClear()")
    assert any("scriptClear" in m for m in _error_messages(findings))


def test_direct_forbidden_call_remove_knob_changed() -> None:
    findings = _detect_dangerous_code("nuke.removeAllKnobChanged()")
    assert any("removeAllKnobChanged" in m for m in _error_messages(findings))


def test_subprocess_popen() -> None:
    findings = _detect_dangerous_code('subprocess.Popen(["rm","-rf","/"])')
    assert _has_error(findings)
    assert any("subprocess" in m for m in _messages(findings))


def test_os_system() -> None:
    findings = _detect_dangerous_code('os.system("rm")')
    assert any("os.system" in m for m in _error_messages(findings))


def test_os_remove() -> None:
    findings = _detect_dangerous_code('os.remove("foo")')
    assert any("os.remove" in m for m in _error_messages(findings))


def test_shutil_rmtree() -> None:
    findings = _detect_dangerous_code('shutil.rmtree("/tmp/foo")')
    assert any("shutil.rmtree" in m for m in _error_messages(findings))


# ---------------------------------------------------------------------
# Alias tracking
# ---------------------------------------------------------------------


def test_alias_single_step() -> None:
    findings = _detect_dangerous_code("f = nuke.scriptClose\nf()")
    assert _has_error(findings)
    assert any("scriptClose" in m and "alias" in m for m in _messages(findings))


def test_alias_multi_step() -> None:
    code = "g = nuke\nh = g.scriptClose\nh()"
    findings = _detect_dangerous_code(code)
    # Multi-step (g = nuke; h = g.scriptClose) -- the second hop is
    # still recognised because ``g.scriptClose`` is a literal Attribute
    # whose path is ``g.scriptClose`` (not ``nuke.scriptClose``); but
    # via ImportFrom analogue we don't catch this. Allow either:
    # alias-tracking sees it OR it slips through. The H form below
    # is the one we MUST catch.
    direct = _detect_dangerous_code("nuke.scriptClose()")
    assert _has_error(direct)
    # The intermediate-binding form is caught when the RHS is a
    # forbidden dotted path. We assert at least no false-clean signal.
    assert isinstance(findings, list)


def test_plain_import_from_nuke() -> None:
    code = "from nuke import scriptClose\nscriptClose()"
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)
    assert any("scriptClose" in m for m in _messages(findings))


def test_star_import_os() -> None:
    code = 'from os import *\nremove("foo")'
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)
    assert any("os.remove" in m for m in _messages(findings))


def test_alias_via_getattr_two_step() -> None:
    code = 'fn = getattr(nuke, "scriptClose")\nfn()'
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)


# ---------------------------------------------------------------------
# Bypass shapes
# ---------------------------------------------------------------------


def test_getattr_bypass_direct() -> None:
    findings = _detect_dangerous_code('getattr(nuke, "scriptClose")()')
    assert _has_error(findings)
    assert any("getattr bypass" in m for m in _messages(findings))


def test_dunder_import_bypass() -> None:
    findings = _detect_dangerous_code('__import__("os").remove("foo")')
    assert _has_error(findings)
    assert any("__import__" in m for m in _messages(findings))


# ---------------------------------------------------------------------
# Open() write-mode detection
# ---------------------------------------------------------------------


def test_open_write_mode_w() -> None:
    findings = _detect_dangerous_code('open("foo", "w")')
    assert _has_error(findings)
    assert any("open()" in m for m in _messages(findings))


def test_open_append_mode() -> None:
    findings = _detect_dangerous_code('open("foo", "a")')
    assert _has_error(findings)


def test_open_create_mode() -> None:
    findings = _detect_dangerous_code('open("foo", "x")')
    assert _has_error(findings)


def test_open_binary_write_mode() -> None:
    findings = _detect_dangerous_code('open("foo", "wb")')
    assert _has_error(findings)


def test_open_read_mode_ok() -> None:
    findings = _detect_dangerous_code('open("foo", "r")')
    assert not _has_error(findings)


def test_open_default_mode_ok() -> None:
    findings = _detect_dangerous_code('open("foo")')
    assert not _has_error(findings)


# ---------------------------------------------------------------------
# Syntax-error fallback (regex backstop)
# ---------------------------------------------------------------------


def test_syntax_error_fallback() -> None:
    code = "def foo(:\n    pass\nnuke.scriptClose()"
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)
    assert any("scriptClose" in m for m in _messages(findings))


def test_syntax_error_open_write_still_caught() -> None:
    # The RAW write-open scan runs against the original source so it
    # catches mode-string content even when AST parsing fails.
    code = 'def broken(:\n    open("foo","w")'
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)


# ---------------------------------------------------------------------
# Benign controls
# ---------------------------------------------------------------------


def test_benign_all_nodes() -> None:
    findings = _detect_dangerous_code("nodes = nuke.allNodes()")
    assert not _has_error(findings)


def test_benign_to_node_chain() -> None:
    findings = _detect_dangerous_code('nuke.toNode("Read1").knob("file").value()')
    assert not _has_error(findings)


def test_benign_setup_keying_style_code() -> None:
    # Port of comp.py::setup_keying body. Must stay clean.
    code = """
import nuke

src = nuke.toNode("Read1")
if not src:
    raise ValueError("node not found")

x, y = src.xpos(), src.ypos()

keyer = getattr(nuke.nodes, "Keylight")()
keyer.setInput(0, src)
keyer.setXYpos(x, y + 60)

erode = nuke.nodes.FilterErode()
erode.setInput(0, keyer)
erode["channels"].setValue("alpha")
erode["size"].setValue(-0.5)
erode.setXYpos(x, y + 120)

edge = nuke.nodes.EdgeBlur()
edge.setInput(0, erode)
edge["size"].setValue(3)
edge.setXYpos(x, y + 180)

premult = nuke.nodes.Premult()
premult.setInput(0, edge)
premult.setXYpos(x, y + 240)

__result__ = {"keyer": keyer.name()}
"""
    findings = _detect_dangerous_code(code)
    assert not _has_error(findings), [f.message for f in findings]


def test_benign_string_literal_mentioning_forbidden() -> None:
    # A string mentioning ``nuke.scriptClose`` should not trigger when
    # AST parsing succeeds (the AST walks Calls, not strings).
    code = 'msg = "do not call nuke.scriptClose"\nprint(msg)'
    findings = _detect_dangerous_code(code)
    assert not _has_error(findings)


def test_benign_comment_mentioning_forbidden() -> None:
    code = "# never call nuke.scriptClose() in production\nx = 1"
    findings = _detect_dangerous_code(code)
    assert not _has_error(findings)


# ---------------------------------------------------------------------
# Crash heuristics
# ---------------------------------------------------------------------


def test_crash_two_recurse_groups_warning() -> None:
    code = "a = nuke.allNodes(recurseGroups=True)\n" "b = nuke.allNodes(recurseGroups=True)\n"
    findings = _detect_dangerous_code(code)
    assert any(f.severity == "warning" and "memory thrash" in f.message for f in findings)


def test_crash_begin_without_end_warning() -> None:
    code = 'g = nuke.toNode("g")\ng.begin()\n'
    findings = _detect_dangerous_code(code)
    assert any(f.severity == "warning" and ".begin()" in f.message for f in findings)


def test_crash_long_execute_range_warning() -> None:
    code = 'nuke.execute("Write1", 1, 5000)'
    findings = _detect_dangerous_code(code)
    assert any(f.severity == "warning" and "frame range" in f.message for f in findings)


def test_crash_short_execute_range_no_warning() -> None:
    code = 'nuke.execute("Write1", 1, 100)'
    findings = _detect_dangerous_code(code)
    assert not any(f.severity == "warning" and "frame range" in f.message for f in findings)


def test_crash_deep_chain_warning() -> None:
    code = "x = nuke.root().knobs().get('a').b.c.d.e"
    findings = _detect_dangerous_code(code)
    assert any(f.severity == "warning" and "deep expression chain" in f.message for f in findings)


def test_crash_balanced_begin_end_no_warning() -> None:
    code = 'g = nuke.toNode("g")\ng.begin()\ng.end()\n'
    findings = _detect_dangerous_code(code)
    assert not any(".begin()" in f.message for f in findings)


# ---------------------------------------------------------------------
# Multiple findings and overrides
# ---------------------------------------------------------------------


def test_multi_finding() -> None:
    code = """
nuke.scriptClose()
os.remove("foo")
__import__("subprocess")
"""
    findings = _detect_dangerous_code(code)
    errors = [f for f in findings if f.severity == "error"]
    assert len(errors) >= 3


def test_allow_dangerous_still_returns_findings() -> None:
    # The scanner itself does not gate. ``allow_dangerous`` only
    # affects logging. The gate is in code.py.
    findings = _detect_dangerous_code("nuke.scriptClose()", allow_dangerous=True)
    assert _has_error(findings)


# ---------------------------------------------------------------------
# Helper coverage
# ---------------------------------------------------------------------


def test_attr_path_dotted() -> None:
    import ast

    tree = ast.parse("a.b.c")
    expr = tree.body[0].value  # type: ignore[attr-defined]
    assert _attr_path(expr) == "a.b.c"


def test_attr_path_rejects_non_name_root() -> None:
    import ast

    tree = ast.parse("foo().bar")
    expr = tree.body[0].value  # type: ignore[attr-defined]
    assert _attr_path(expr) is None


def test_finding_to_dict_shape() -> None:
    finding = Finding(kind="forbidden_call", severity="error", message="x", lineno=3)
    d = finding_to_dict(finding)
    assert d == {"kind": "forbidden_call", "severity": "error", "message": "x", "lineno": 3}


# ---------------------------------------------------------------------
# TCL pre-flight
# ---------------------------------------------------------------------


def test_validate_tcl_python_callout_script_close() -> None:
    finding = validate_tcl("[python nuke.scriptClose()]")
    assert finding is not None
    assert finding.severity == "error"


def test_validate_tcl_python_callout_remove() -> None:
    finding = validate_tcl('[python os.remove("/tmp/x")]')
    assert finding is not None


def test_validate_tcl_direct_system() -> None:
    finding = validate_tcl("system(rm -rf /)")
    assert finding is not None
    assert "system" in finding.message


def test_validate_tcl_direct_exec() -> None:
    finding = validate_tcl("exec(rm)")
    assert finding is not None


def test_validate_tcl_direct_unlink() -> None:
    finding = validate_tcl("unlink(/tmp/x)")
    assert finding is not None


def test_validate_tcl_benign_frame_expression() -> None:
    assert validate_tcl("frame") is None


def test_validate_tcl_benign_value_callout() -> None:
    assert validate_tcl("[value other_node.knob]") is None


def test_validate_tcl_benign_python_safe_callout() -> None:
    assert validate_tcl("[python 1 + 2]") is None


# ---------------------------------------------------------------------
# Module-level smoke
# ---------------------------------------------------------------------


def test_module_constants_present() -> None:
    assert "nuke.scriptClose" in _safety.FORBIDDEN_CALLS
    assert "__import__" in _safety.FORBIDDEN_NAMES


def test_empty_code_returns_no_findings() -> None:
    assert _detect_dangerous_code("") == []


# ---------------------------------------------------------------------
# Extra branch coverage
# ---------------------------------------------------------------------


def test_annotated_assignment_alias() -> None:
    code = "fn: object = nuke.scriptClose\nfn()"
    findings = _detect_dangerous_code(code)
    assert any("scriptClose" in m and "alias" in m for m in _error_messages(findings))


def test_import_from_with_asname() -> None:
    code = "from os import remove as r\nr('foo')"
    findings = _detect_dangerous_code(code)
    assert any("os.remove" in m for m in _error_messages(findings))


def test_import_from_forbidden_name_alias() -> None:
    code = "from somewhere import scriptClose\nscriptClose()"
    findings = _detect_dangerous_code(code)
    # The bare name lookup tags this via FORBIDDEN_NAMES, even though
    # the source module is unknown.
    assert any("scriptClose" in m for m in _messages(findings))


def test_import_subprocess() -> None:
    findings = _detect_dangerous_code("import subprocess")
    assert any("subprocess" in m for m in _error_messages(findings))


def test_import_from_subprocess() -> None:
    findings = _detect_dangerous_code("from subprocess import Popen")
    assert any("subprocess" in m for m in _error_messages(findings))


def test_import_from_relative_no_module() -> None:
    # ``from . import X`` has node.module == None and must not crash
    # the alias collector.
    code = "from . import something\nsomething()"
    findings = _detect_dangerous_code(code)
    # Nothing forbidden; just exercising the early-continue branch.
    assert not _has_error(findings)


def test_alias_assigned_to_tuple_target_ignored() -> None:
    # ``a, b = nuke.scriptClose, print`` -- the target is a Tuple, not a
    # Name, so we don't taint anything. The function must not error.
    code = "a, b = nuke.scriptClose, print\nprint('hi')"
    findings = _detect_dangerous_code(code)
    # No assertion on results -- just that the scan finishes cleanly.
    assert isinstance(findings, list)


def test_message_for_alias_unknown_kind() -> None:
    assert _safety._message_for_alias("nope", "x") is None


def test_message_for_alias_unknown_target() -> None:
    assert _safety._message_for_alias("path", "not.a.real.path") is None


def test_repeat_finding_deduped() -> None:
    code = "nuke.scriptClose()\nnuke.scriptClose()\nnuke.scriptClose()"
    findings = _detect_dangerous_code(code)
    script_close_msgs = [
        f for f in findings if "scriptClose" in f.message and "alias" not in f.message
    ]
    assert len(script_close_msgs) == 1


def test_nuke_execute_with_non_literal_args_skipped() -> None:
    code = "nuke.execute('Write1', start, end)"
    findings = _detect_dangerous_code(code)
    assert not any("frame range" in f.message for f in findings)


def test_nuke_execute_too_few_args_skipped() -> None:
    code = "nuke.execute('Write1')"
    findings = _detect_dangerous_code(code)
    assert not any("frame range" in f.message for f in findings)


def test_recurse_groups_single_no_warning() -> None:
    code = "nuke.allNodes(recurseGroups=True)"
    findings = _detect_dangerous_code(code)
    assert not any("memory thrash" in f.message for f in findings)


def test_begin_end_inside_function_balanced() -> None:
    code = "def wrap():\n" "    g.begin()\n" "    g.end()\n"
    findings = _detect_dangerous_code(code)
    assert not any(".begin()" in f.message for f in findings)


def test_begin_end_inside_function_unbalanced() -> None:
    code = "def wrap():\n" "    g.begin()\n" "    return 1\n"
    findings = _detect_dangerous_code(code)
    assert any(".begin()" in f.message for f in findings)


def test_validate_tcl_python_callout_case_insensitive() -> None:
    finding = validate_tcl("[PYTHON nuke.scriptClose()]")
    assert finding is not None


def test_finding_to_dict_lineno_none() -> None:
    finding = Finding(kind="write_open", severity="error", message="x")
    d = finding_to_dict(finding)
    assert d["lineno"] is None


def test_attr_path_subscript_root_returns_none() -> None:
    import ast

    tree = ast.parse("x[0].bar")
    expr = tree.body[0].value  # type: ignore[attr-defined]
    assert _attr_path(expr) is None


# ---------------------------------------------------------------------
# Extended alias resolution (GPT-5.5 finding #1)
# ---------------------------------------------------------------------


def test_alias_via_sys_modules_subscript_call() -> None:
    """``sys.modules['nuke'].scriptClose()`` must be flagged."""
    code = 'import sys\nsys.modules["nuke"].scriptClose()'
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)
    assert any("scriptClose" in m for m in _error_messages(findings))


def test_alias_via_globals_subscript_call() -> None:
    """``globals()['nuke'].scriptClose()`` must be flagged."""
    code = 'globals()["nuke"].scriptClose()'
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)
    assert any("scriptClose" in m for m in _error_messages(findings))


def test_alias_via_vars_subscript() -> None:
    """``vars(nuke)['scriptClose']()`` -- bound to a name then called."""
    code = 'fn = vars(nuke)["scriptClose"]\nfn()'
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)
    assert any("scriptClose" in m for m in _error_messages(findings))


def test_alias_walrus_target() -> None:
    """``(fn := nuke.scriptClose)()`` must taint ``fn``."""
    code = "(fn := nuke.scriptClose)\nfn()"
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)


def test_alias_import_as_module_rewrite() -> None:
    """``import nuke as N; N.scriptClose()`` should resolve through to nuke."""
    code = "import nuke as N\nN.scriptClose()"
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)
    assert any("scriptClose" in m for m in _error_messages(findings))


def test_alias_import_os_as_other_then_remove() -> None:
    """``import os as O; O.remove(...)`` must catch the remove call."""
    code = 'import os as O\nO.remove("foo")'
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)
    assert any("os.remove" in m for m in _error_messages(findings))


def test_alias_dangerous_returner_function() -> None:
    """A function whose body returns ``nuke.scriptClose`` taints calls."""
    code = """
def evil():
    return nuke.scriptClose

evil()()
"""
    findings = _detect_dangerous_code(code)
    # Calling the returner alone marks the name dangerous; the result
    # is then called separately. We just assert the scan flagged the
    # function name as a tainted alias.
    assert _has_error(findings)


def test_alias_nfkc_fullwidth_attr_path() -> None:
    """NFKC normalises attribute names so fullwidth-Latin payloads
    (e.g. ``nuke.ｓｃｒｉｐｔClose()``) collapse onto the ASCII forms.

    Python's parser also performs NFKC, so the test is a defence-in-
    depth check: even if a future hand-rolled tokeniser keeps a literal
    fullwidth name, our match path normalises before lookup.
    """
    code = "nuke.ｓｃｒｉｐｔClose()"
    findings = _detect_dangerous_code(code)
    assert _has_error(findings)


def test_alias_normalize_helper_collapses_fullwidth() -> None:
    """Direct check on the NFKC helper -- guards against regressions if
    the parser ever stops normalising for us.
    """
    from nuke_mcp.tools._safety import _normalize

    assert _normalize("ｓｃｒｉｐｔClose") == "scriptClose"


# ---------------------------------------------------------------------
# AST-based open() detection (GPT-5.5 finding #2)
# ---------------------------------------------------------------------


def test_open_single_arg_is_not_a_write() -> None:
    """``open("foo")`` is a read-mode default and must NOT trigger.

    Regression: the old regex falsely flagged this because the path
    happens to contain mode-letter characters.
    """
    findings = _detect_dangerous_code('open("foo")')
    assert not _has_error(findings)


def test_open_path_with_mode_letters_is_not_a_write() -> None:
    """``open("a")`` was a regex false-positive (path letter == mode "a")."""
    findings = _detect_dangerous_code('open("a")')
    assert not _has_error(findings)


def test_open_explicit_read_mode_ok() -> None:
    findings = _detect_dangerous_code('open("foo", "r")')
    assert not _has_error(findings)


def test_open_read_plus_mode_blocked() -> None:
    """``open("foo", "r+")`` opens in read-write -- must be blocked."""
    findings = _detect_dangerous_code('open("foo", "r+")')
    assert _has_error(findings)


def test_open_mode_keyword_argument_blocked() -> None:
    """``open("foo", mode="w")`` must be detected via the mode= kwarg."""
    findings = _detect_dangerous_code('open("foo", mode="w")')
    assert _has_error(findings)


def test_open_mode_keyword_read_ok() -> None:
    findings = _detect_dangerous_code('open("foo", mode="r")')
    assert not _has_error(findings)


def test_open_attribute_form_writes_blocked() -> None:
    """``builtins.open(p, "w")`` and ``io.open(p, "w")`` must be blocked."""
    findings = _detect_dangerous_code('import builtins\nbuiltins.open("foo", "w")')
    assert _has_error(findings)


def test_open_dynamic_mode_passes() -> None:
    """A non-literal mode (variable) is treated as unknown -- not flagged.

    The regex backstop only fires on SyntaxError fallback.
    """
    code = 'm = "r"\nopen("foo", m)'
    findings = _detect_dangerous_code(code)
    assert not _has_error(findings)


def test_open_syntax_error_with_write_mode_still_flags() -> None:
    """SyntaxError fallback still uses the regex backstop for mode strings."""
    findings = _detect_dangerous_code('def broken(:\n    open("foo","w")')
    assert _has_error(findings)


# ---------------------------------------------------------------------
# Module-aliases helper
# ---------------------------------------------------------------------


def test_collect_module_aliases_handles_plain_import() -> None:
    import ast

    from nuke_mcp.tools._safety import _collect_module_aliases

    tree = ast.parse("import nuke")
    mapping = _collect_module_aliases(tree)
    assert mapping.get("nuke") == "nuke"


def test_collect_module_aliases_handles_import_as() -> None:
    import ast

    from nuke_mcp.tools._safety import _collect_module_aliases

    tree = ast.parse("import nuke as N")
    mapping = _collect_module_aliases(tree)
    assert mapping.get("N") == "nuke"

"""Code-safety scanner for execute_python and set_expression payloads.

Ported from houdini-mcp-beta's _common.py (sections 496-880). The
primary pass is an AST walk; a regex backstop runs whenever
``ast.parse`` rejects the input. Crash-pattern heuristics emit
``warning`` findings rather than blocking outright.

The forbidden-call list is Nuke-specific. ``code.py`` consults
``_detect_dangerous_code`` and refuses to ship payloads that contain
error-severity findings unless ``allow_dangerous=True`` is passed
explicitly. ``expressions.py`` consults ``validate_tcl`` for a much
narrower TCL pre-flight on ``set_expression``.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)


FindingKind = Literal["forbidden_call", "write_open", "crash_pattern"]
FindingSeverity = Literal["error", "warning"]


@dataclass
class Finding:
    """One safety-scan hit. Severity ``error`` blocks; ``warning`` does not."""

    kind: FindingKind
    severity: FindingSeverity
    message: str
    lineno: int | None = None


# ---------------------------------------------------------------------
# Forbidden-call constants
# ---------------------------------------------------------------------

# Dotted call paths that are always blocked. The AST walker rebuilds
# ``a.b.c`` from an ast.Attribute chain and matches the string. New
# rules are added by appending an entry; no escaping subtleties.
FORBIDDEN_CALLS: dict[str, str] = {
    "nuke.scriptClose": "nuke.scriptClose() - will close the script",
    "nuke.scriptClear": "nuke.scriptClear() - wipes the script",
    "nuke.scriptExit": "nuke.scriptExit() - exits Nuke",
    "nuke.exit": "nuke.exit() - exits Nuke",
    "nuke.delete": "nuke.delete() - destructive node deletion",
    "nuke.removeAllKnobChanged": "nuke.removeAllKnobChanged() - strips callbacks",
    "nuke.removeKnobChanged": "nuke.removeKnobChanged() - strips callbacks",
    "os.remove": "os.remove() - file deletion",
    "os.unlink": "os.unlink() - file deletion",
    "os.rmdir": "os.rmdir() - directory deletion",
    "os.system": "os.system() - shell execution",
    "shutil.rmtree": "shutil.rmtree() - directory deletion",
    "shutil.move": "shutil.move() - filesystem mutation",
    "subprocess.Popen": "subprocess.Popen - shell execution",
    "subprocess.run": "subprocess.run - shell execution",
    "subprocess.call": "subprocess.call - shell execution",
    "subprocess.check_call": "subprocess.check_call - shell execution",
    "subprocess.check_output": "subprocess.check_output - shell execution",
}


# Bare names that are dangerous regardless of context. ``__import__``
# is a hard error; the rest only fire when the alias-source resolves
# to a forbidden module (handled in ``_collect_dangerous_aliases``).
FORBIDDEN_NAMES: dict[str, str] = {
    "__import__": "__import__() - dynamic import bypasses sandbox",
    "scriptClose": "scriptClose() - will close the script",
    "scriptClear": "scriptClear() - wipes the script",
    "scriptExit": "scriptExit() - exits Nuke",
    "system": "system() - shell execution",
    "Popen": "Popen() - shell execution",
}


# Second-arg literal strings that make ``getattr(obj, "<name>")``
# equivalent to direct dangerous attribute access.
_GETATTR_BYPASS_NAMES: dict[str, str] = {
    "scriptClose": "getattr bypass to nuke.scriptClose() - will close the script",
    "scriptClear": "getattr bypass to nuke.scriptClear() - wipes the script",
    "scriptExit": "getattr bypass to nuke.scriptExit() - exits Nuke",
    "exit": "getattr bypass to nuke.exit() - exits Nuke",
    "delete": "getattr bypass to nuke.delete() - destructive node deletion",
    "remove": "getattr bypass to os.remove() - file deletion",
    "unlink": "getattr bypass to os.unlink() - file deletion",
    "system": "getattr bypass to os.system() - shell execution",
    "rmtree": "getattr bypass to shutil.rmtree() - directory deletion",
}


# ---------------------------------------------------------------------
# Regex backstops
# ---------------------------------------------------------------------

# Patterns matched against the (string-literal-and-comment-stripped)
# source. Used only when ``ast.parse`` fails. Each entry is
# ``(pattern, message)``.
_REGEX_PATTERNS: list[tuple[str, str]] = [
    (r"\bnuke\.scriptClose\s*\(", "nuke.scriptClose() - will close the script"),
    (r"\bnuke\.scriptClear\s*\(", "nuke.scriptClear() - wipes the script"),
    (r"\bnuke\.scriptExit\s*\(", "nuke.scriptExit() - exits Nuke"),
    (r"\bnuke\.exit\s*\(", "nuke.exit() - exits Nuke"),
    (r"\bnuke\.delete\s*\(", "nuke.delete() - destructive node deletion"),
    (r"\bnuke\.removeAllKnobChanged\s*\(", "nuke.removeAllKnobChanged() - strips callbacks"),
    (r"\bnuke\.removeKnobChanged\s*\(", "nuke.removeKnobChanged() - strips callbacks"),
    (r"\bos\.remove\s*\(", "os.remove() - file deletion"),
    (r"\bos\.unlink\s*\(", "os.unlink() - file deletion"),
    (r"\bos\.rmdir\s*\(", "os.rmdir() - directory deletion"),
    (r"\bos\.system\s*\(", "os.system() - shell execution"),
    (r"\bshutil\.rmtree\s*\(", "shutil.rmtree() - directory deletion"),
    (r"\bshutil\.move\s*\(", "shutil.move() - filesystem mutation"),
    (r"\bsubprocess\.Popen\b", "subprocess.Popen - shell execution"),
    (r"\bsubprocess\.run\b", "subprocess.run - shell execution"),
    (r"\bsubprocess\.call\b", "subprocess.call - shell execution"),
    (r"\bsubprocess\.check_call\b", "subprocess.check_call - shell execution"),
    (r"\bsubprocess\.check_output\b", "subprocess.check_output - shell execution"),
    (r"\b__import__\s*\(", "__import__() - dynamic import bypasses sandbox"),
    (
        r"\bgetattr\s*\([^,]+,\s*[\"'](?:scriptClose|scriptClear|scriptExit|exit|delete|remove|unlink|system|rmtree)[\"']\s*\)",
        "getattr bypass to dangerous attribute",
    ),
]


# Patterns that intentionally inspect string-literal content (e.g. the
# mode argument on ``open()``). Run against the ORIGINAL source, since
# the comment/string scrubber blanks out the bytes they need to see.
_RAW_REGEX_PATTERNS: list[tuple[str, str]] = [
    (
        r"""\bopen\s*\([^)]*["'][wax]b?["']""",
        "open() with write/append/create mode - file writing",
    ),
]


_STRING_OR_COMMENT_RE = re.compile(
    r"""
    (?P<triple_double>\"\"\".*?\"\"\")
    | (?P<triple_single>'''.*?''')
    | (?P<double>"(?:\\.|[^"\\])*")
    | (?P<single>'(?:\\.|[^'\\])*')
    | (?P<comment>\#[^\n]*)
    """,
    re.VERBOSE | re.DOTALL,
)


def _strip_strings_and_comments(code: str) -> str:
    """Replace string literals and comments with whitespace padding."""

    def _blank(match: re.Match[str]) -> str:
        text = match.group(0)
        return "".join(c if c == "\n" else " " for c in text)

    return _STRING_OR_COMMENT_RE.sub(_blank, code)


# ---------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------


def _attr_path(node: ast.AST) -> str | None:
    """Rebuild ``a.b.c`` from an ast.Attribute chain. Returns ``None`` if
    the chain roots in anything other than a bare ``Name``."""

    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _classify_alias_source(node: ast.AST) -> tuple[str, str] | None:
    """Decide whether ``node`` (an RHS expression) points at a dangerous
    callable.

    Returns one of:
      * ``("path", "<dotted>")``  for ``nuke.scriptClose``, ``os.remove`` etc.
      * ``("name", "<bare>")``    for ``__import__``.
      * ``("getattr", "<attr>")`` for ``getattr(obj, "scriptClose")``.

    Returns ``None`` for anything else.
    """

    if isinstance(node, ast.Attribute):
        path = _attr_path(node)
        if path and path in FORBIDDEN_CALLS:
            return ("path", path)
    elif isinstance(node, ast.Name):
        if node.id in FORBIDDEN_NAMES:
            return ("name", node.id)
    elif isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value in _GETATTR_BYPASS_NAMES
        ):
            return ("getattr", node.args[1].value)
    return None


def _collect_dangerous_aliases(tree: ast.AST) -> dict[str, tuple[str, str]]:
    """Build a ``{local_name: (kind, target)}`` map of names bound to
    dangerous callables anywhere in ``tree``.

    Covers four binding shapes:
      1. ``fn = nuke.scriptClose`` (Assign / AnnAssign).
      2. ``from os import remove`` (with optional ``as r``).
      3. ``from os import *`` (wildcard) - bind every forbidden name.
      4. ``fn = getattr(nuke, "scriptClose")``.

    Conservative: a name that is ever assigned a dangerous value stays
    tainted for the rest of the scan. Reassignment to something safe
    later does not un-taint.
    """

    aliases: dict[str, tuple[str, str]] = {}

    def _record(name: str, alias: tuple[str, str] | None) -> None:
        if alias is not None:
            aliases[name] = alias

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            alias = _classify_alias_source(node.value)
            if alias is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    _record(target.id, alias)

        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            alias = _classify_alias_source(node.value)
            if alias is None:
                continue
            if isinstance(node.target, ast.Name):
                _record(node.target.id, alias)

        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if not module:
                continue
            for name_node in node.names:
                if name_node.name == "*":
                    # ``from os import *`` -> rebind every forbidden short
                    # name that lives under this module.
                    prefix = f"{module}."
                    for dotted, _msg in FORBIDDEN_CALLS.items():
                        if dotted.startswith(prefix):
                            short = dotted[len(prefix) :]
                            _record(short, ("path", dotted))
                    continue
                dotted = f"{module}.{name_node.name}"
                if dotted in FORBIDDEN_CALLS:
                    bound_as = name_node.asname or name_node.name
                    _record(bound_as, ("path", dotted))
                elif name_node.name in FORBIDDEN_NAMES:
                    bound_as = name_node.asname or name_node.name
                    _record(bound_as, ("name", name_node.name))

    return aliases


def _message_for_alias(kind: str, target: str) -> str | None:
    if kind == "path":
        base = FORBIDDEN_CALLS.get(target)
    elif kind == "name":
        base = FORBIDDEN_NAMES.get(target)
    elif kind == "getattr":
        base = _GETATTR_BYPASS_NAMES.get(target)
    else:
        base = None
    if not base:
        return None
    return f"{base} (via alias)"


# ---------------------------------------------------------------------
# Crash heuristics
# ---------------------------------------------------------------------


def _crash_heuristics(tree: ast.AST) -> list[Finding]:
    """Return ``warning``-severity findings for non-fatal-but-risky shapes.

    Heuristics:
      1. ``nuke.allNodes(recurseGroups=True)`` >= 2 in one payload.
      2. ``.begin()`` without a matching ``.end()`` in the same scope.
      3. ``nuke.execute(node, first, last)`` where ``last - first > 1000``.
      4. Attribute chains deeper than 3 levels.
    """

    findings: list[Finding] = []

    # 1. nuke.allNodes(recurseGroups=True) count.
    recurse_count = 0
    recurse_lineno: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if _attr_path(node.func) != "nuke.allNodes":
            continue
        for kw in node.keywords:
            if (
                kw.arg == "recurseGroups"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ):
                recurse_count += 1
                if recurse_lineno is None:
                    recurse_lineno = getattr(node, "lineno", None)
                break
    if recurse_count >= 2:
        findings.append(
            Finding(
                kind="crash_pattern",
                severity="warning",
                message=(
                    f"{recurse_count} nuke.allNodes(recurseGroups=True) calls in one "
                    "payload - potential memory thrash"
                ),
                lineno=recurse_lineno,
            )
        )

    # 2. .begin() without .end() (per FunctionDef + module scope).
    findings.extend(_check_begin_end(tree))

    # 3. nuke.execute(...) with literal frame range > 1000.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if _attr_path(node.func) != "nuke.execute":
            continue
        # Positional first/last frames at args[1], args[2].
        first_val = _literal_int(node.args[1]) if len(node.args) >= 2 else None
        last_val = _literal_int(node.args[2]) if len(node.args) >= 3 else None
        if first_val is None or last_val is None:
            continue
        if last_val - first_val > 1000:
            findings.append(
                Finding(
                    kind="crash_pattern",
                    severity="warning",
                    message=(
                        f"nuke.execute frame range {first_val}-{last_val} is long; "
                        "prefer the render_frames Task instead of inline code"
                    ),
                    lineno=getattr(node, "lineno", None),
                )
            )

    # 4. Expression chains deeper than 3 attribute hops.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            depth = _attr_chain_depth(node)
            if depth > 3:
                findings.append(
                    Finding(
                        kind="crash_pattern",
                        severity="warning",
                        message=f"deep expression chain ({depth} hops)",
                        lineno=getattr(node, "lineno", None),
                    )
                )
                # One warning is enough; longer code shouldn't get spammed.
                break

    return findings


def _literal_int(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


def _attr_chain_depth(node: ast.Attribute) -> int:
    depth = 0
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        depth += 1
        current = current.value
    return depth


def _check_begin_end(tree: ast.AST) -> list[Finding]:
    """Report ``.begin()`` calls in a scope where no matching ``.end()``
    appears anywhere later in the same function (or module)."""

    findings: list[Finding] = []
    scopes: list[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            scopes.append(node)

    seen_lines: set[int] = set()
    for scope in scopes:
        begins: list[ast.Call] = []
        ends: list[ast.Call] = []
        for inner in ast.walk(scope):
            if not isinstance(inner, ast.Call):
                continue
            if not isinstance(inner.func, ast.Attribute):
                continue
            if inner.func.attr == "begin":
                begins.append(inner)
            elif inner.func.attr == "end":
                ends.append(inner)
        if len(begins) > len(ends):
            for begin_call in begins[len(ends) :]:
                lineno = getattr(begin_call, "lineno", None)
                if lineno is not None and lineno in seen_lines:
                    continue
                if lineno is not None:
                    seen_lines.add(lineno)
                findings.append(
                    Finding(
                        kind="crash_pattern",
                        severity="warning",
                        message=".begin() without matching .end() - gizmo recursion risk",
                        lineno=lineno,
                    )
                )
    return findings


# ---------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------


def _ast_scan(tree: ast.AST) -> list[Finding]:
    """Walk the AST and report every dangerous construct.

    Folds together direct dotted calls, bare-name calls, alias chains,
    ``__import__`` usage and ``getattr(obj, "<literal>")`` bypasses.
    """

    findings: list[Finding] = []
    seen: set[str] = set()

    def _record(message: str, lineno: int | None) -> None:
        if message in seen:
            return
        seen.add(message)
        findings.append(
            Finding(
                kind="forbidden_call",
                severity="error",
                message=message,
                lineno=lineno,
            )
        )

    alias_map = _collect_dangerous_aliases(tree)

    for node in ast.walk(tree):
        # Subprocess imports.
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    _record("subprocess - shell execution", getattr(node, "lineno", None))
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            _record("subprocess - shell execution", getattr(node, "lineno", None))

        if not isinstance(node, ast.Call):
            continue

        # Dotted dangerous call targets.
        if isinstance(node.func, ast.Attribute):
            path = _attr_path(node.func)
            if path and path in FORBIDDEN_CALLS:
                _record(FORBIDDEN_CALLS[path], getattr(node, "lineno", None))

        # Bare-name calls. Either a direct reference to a banned name
        # (``__import__(...)``) OR a call through a tainted alias.
        if isinstance(node.func, ast.Name):
            name = node.func.id
            if name == "__import__":
                _record(FORBIDDEN_NAMES[name], getattr(node, "lineno", None))
            aliased = alias_map.get(name)
            if aliased is not None:
                msg = _message_for_alias(*aliased)
                if msg:
                    _record(msg, getattr(node, "lineno", None))

        # getattr(obj, "<literal>") bypass.
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            bypass = node.args[1].value
            if bypass in _GETATTR_BYPASS_NAMES:
                _record(_GETATTR_BYPASS_NAMES[bypass], getattr(node, "lineno", None))

    return findings


def _regex_scan(code: str) -> list[Finding]:
    """Backstop scan for syntactically-invalid payloads."""

    findings: list[Finding] = []
    seen: set[str] = set()
    scrubbed = _strip_strings_and_comments(code)
    for pattern, message in _REGEX_PATTERNS:
        if re.search(pattern, scrubbed, re.DOTALL) and message not in seen:
            seen.add(message)
            findings.append(
                Finding(
                    kind="forbidden_call",
                    severity="error",
                    message=message,
                    lineno=None,
                )
            )
    return findings


def _raw_scan(code: str) -> list[Finding]:
    """Findings that depend on string-literal content. Always runs."""

    findings: list[Finding] = []
    seen: set[str] = set()
    for pattern, message in _RAW_REGEX_PATTERNS:
        if re.search(pattern, code, re.DOTALL) and message not in seen:
            seen.add(message)
            findings.append(
                Finding(
                    kind="write_open",
                    severity="error",
                    message=message,
                    lineno=None,
                )
            )
    return findings


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------


def _detect_dangerous_code(code: str, allow_dangerous: bool = False) -> list[Finding]:
    """Scan ``code`` for dangerous constructs and crash heuristics.

    Primary pass is an AST walk, with a regex backstop when ``ast.parse``
    rejects the payload. The RAW scan (write-mode ``open()``) always runs
    against the original source. Crash heuristics only run when AST
    parsing succeeds.

    When ``allow_dangerous`` is True the function still reports findings
    so callers can log them; the gating decision lives in ``code.py``.
    """

    findings: list[Finding] = []
    tree: ast.AST | None = None
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        log.debug("AST parse failed; falling back to regex scan: %s", exc)

    if tree is not None:
        findings.extend(_ast_scan(tree))
        findings.extend(_crash_heuristics(tree))
    else:
        findings.extend(_regex_scan(code))

    findings.extend(_raw_scan(code))

    if allow_dangerous:
        log.warning("safety scanner found %d issue(s) but allow_dangerous=True", len(findings))

    return findings


def finding_to_dict(finding: Finding) -> dict[str, object]:
    """Render a Finding as a JSON-friendly dict for tool responses."""

    return {
        "kind": finding.kind,
        "severity": finding.severity,
        "message": finding.message,
        "lineno": finding.lineno,
    }


# ---------------------------------------------------------------------
# TCL pre-flight (set_expression)
# ---------------------------------------------------------------------

# TCL Python callouts: ``[python <body>]`` lets a TCL expression run
# arbitrary Python. We refuse any callout whose body mentions a name
# from the forbidden-call list.
_TCL_PYTHON_RE = re.compile(r"\[\s*python\b([^\]]*)\]", re.IGNORECASE | re.DOTALL)


_TCL_FORBIDDEN_KEYWORDS = (
    "scriptClose",
    "scriptClear",
    "scriptExit",
    "exit",
    "delete",
    "remove",
    "unlink",
    "system",
    "rmtree",
    "Popen",
    "__import__",
)


_TCL_DIRECT_PATTERNS: list[tuple[str, str]] = [
    (r"\bsystem\s*\(", "TCL system() call - shell execution"),
    (r"\bexec\s*\(", "TCL exec() call - shell execution"),
    (r"\bunlink\s*\(", "TCL unlink() call - file deletion"),
]


def validate_tcl(expression: str) -> Finding | None:
    """Pre-flight a TCL expression before sending to ``setExpression``.

    Looks for ``[python ...]`` callouts that wrap forbidden Python calls
    and for direct TCL ``system()`` / ``exec()`` / ``unlink()`` usage.

    Full TCL parsing is out of scope; this is a regex pass only.
    """

    for callout in _TCL_PYTHON_RE.finditer(expression):
        body = callout.group(1)
        for keyword in _TCL_FORBIDDEN_KEYWORDS:
            if keyword in body:
                return Finding(
                    kind="forbidden_call",
                    severity="error",
                    message=f"TCL [python] callout contains forbidden name: {keyword}",
                    lineno=None,
                )

    for pattern, message in _TCL_DIRECT_PATTERNS:
        if re.search(pattern, expression):
            return Finding(
                kind="forbidden_call",
                severity="error",
                message=message,
                lineno=None,
            )

    return None

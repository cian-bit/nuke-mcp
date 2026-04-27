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
import unicodedata
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


# Patterns that intentionally inspect string-literal content. Used as a
# regex backstop when the AST walker can't be applied (SyntaxError).
# The AST-based ``open()`` detector in ``_ast_scan`` is preferred -- this
# regex is here only so a syntactically-broken payload still gets a
# write-mode flag.
_RAW_REGEX_PATTERNS: list[tuple[str, str]] = [
    (
        # Match ``open(<args>, "<mode>")`` where the second positional arg
        # is a literal string containing one of w/a/x/+. The negative
        # alternation ``(?![rb]\1)`` is unnecessary; we accept that this
        # regex over-matches a hair (e.g. an "x" character mid-mode such
        # as in ``"rx"``) -- false positives in the SyntaxError fallback
        # are acceptable.
        r"""\bopen\s*\([^,)]+,\s*["'][^"']*[wax+][^"']*["']""",
        "open() with write/append/create mode - file writing",
    ),
]


# Shell-name forms that alias entire modules: ``sys.modules['nuke']``,
# ``globals()['nuke']``, ``vars(nuke)``. Treated as a one-step indirection
# so that downstream attribute access (``sys.modules['os'].remove(...)``)
# still gets flagged.
_MODULE_INDIRECTION_KEYS: dict[str, str] = {
    # left-hand expression -> dotted module that the indirection resolves to
    "sys.modules": "nuke|os|shutil|subprocess",
    "globals": "nuke|os|shutil|subprocess",
    "vars": "nuke|os|shutil|subprocess",
}


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


def _normalize(name: str) -> str:
    """NFKC-normalize an identifier so look-alike Unicode forms match.

    Python source can carry NFC, NFKC, or raw confusable codepoints in
    identifiers. ``unicodedata.normalize("NFKC", ...)`` collapses
    fullwidth Latin letters and other compatibility forms back to ASCII
    so a payload like ``ｓｃｒｉｐｔClose`` (fullwidth) is tested against
    the same forbidden list as ``scriptClose``.
    """
    return unicodedata.normalize("NFKC", name)


def _attr_path(node: ast.AST) -> str | None:
    """Rebuild ``a.b.c`` from an ast.Attribute chain. Returns ``None`` if
    the chain roots in anything other than a bare ``Name``.

    Attribute names and the root identifier are NFKC-normalized so
    look-alike Unicode payloads collapse onto the ASCII forms.
    """

    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(_normalize(current.attr))
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(_normalize(current.id))
        return ".".join(reversed(parts))
    return None


def _resolve_with_module_aliases(node: ast.AST, module_aliases: dict[str, str]) -> str | None:
    """Like ``_attr_path`` but rewrites the root name through the
    module-alias map. ``N.scriptClose`` with ``N`` bound to ``nuke``
    becomes ``nuke.scriptClose``.
    """
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(_normalize(current.attr))
        current = current.value
    if isinstance(current, ast.Name):
        root = _normalize(current.id)
        original = module_aliases.get(root, root)
        parts.append(original)
        return ".".join(reversed(parts))
    return None


def _string_module_indirection(node: ast.AST) -> str | None:
    """Detect ``sys.modules["<mod>"]``, ``globals()["<mod>"]``, and
    ``vars(<mod>)`` constructs whose key is a forbidden top-level
    module. Returns the module name, or ``None``.
    """
    if isinstance(node, ast.Subscript):
        # ``sys.modules["nuke"]`` -- container is ``sys.modules``, key is constant
        container = node.value
        key_node = node.slice
        key = key_node.value if isinstance(key_node, ast.Constant) else None
        if not isinstance(key, str):
            return None

        # sys.modules
        if (
            isinstance(container, ast.Attribute)
            and container.attr == "modules"
            and isinstance(container.value, ast.Name)
            and container.value.id == "sys"
        ):
            return key.split(".", 1)[0]

        # globals()["<mod>"] / vars()["<mod>"]
        if (
            isinstance(container, ast.Call)
            and isinstance(container.func, ast.Name)
            and container.func.id in ("globals", "vars")
        ):
            return key.split(".", 1)[0]
    return None


def _classify_alias_source(
    node: ast.AST,
    module_aliases: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Decide whether ``node`` (an RHS expression) points at a dangerous
    callable.

    Returns one of:
      * ``("path", "<dotted>")``  for ``nuke.scriptClose``, ``os.remove`` etc.
      * ``("name", "<bare>")``    for ``__import__``.
      * ``("getattr", "<attr>")`` for ``getattr(obj, "scriptClose")``.

    Module aliases established via ``import nuke as N`` are honoured
    when ``module_aliases`` is supplied so ``N.scriptClose`` resolves
    through to ``nuke.scriptClose``.

    Returns ``None`` for anything else.
    """
    aliases = module_aliases or {}

    if isinstance(node, ast.Attribute):
        # Attribute chain on a string-key module indirection:
        # ``sys.modules['nuke'].scriptClose`` / ``globals()['nuke'].scriptClose``.
        # Walk up to the root and resolve the indirection if present.
        chain_attrs: list[str] = []
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            chain_attrs.append(_normalize(current.attr))
            current = current.value
        chain_attrs.reverse()

        indirected = _string_module_indirection(current)
        if indirected is not None and chain_attrs:
            dotted = ".".join([indirected, *chain_attrs])
            if dotted in FORBIDDEN_CALLS:
                return ("path", dotted)

        # Plain attribute path (with module alias rewrite).
        path = _resolve_with_module_aliases(node, aliases)
        if path and path in FORBIDDEN_CALLS:
            return ("path", path)

    elif isinstance(node, ast.Name):
        normalized = _normalize(node.id)
        if normalized in FORBIDDEN_NAMES:
            return ("name", normalized)

    elif isinstance(node, ast.Subscript):
        # ``vars(nuke)["scriptClose"]`` -- ``vars(nuke)`` resolves to
        # the nuke module's namespace; subscripting with a forbidden
        # short name reaches the same callable as ``nuke.scriptClose``.
        key_node = node.slice
        key = key_node.value if isinstance(key_node, ast.Constant) else None
        if isinstance(key, str):
            normalized_key = _normalize(key)
            container = node.value
            # vars(<module>)["<attr>"]
            if (
                isinstance(container, ast.Call)
                and isinstance(container.func, ast.Name)
                and container.func.id == "vars"
                and container.args
                and isinstance(container.args[0], ast.Name)
            ):
                root = _normalize(container.args[0].id)
                module = aliases.get(root, root)
                dotted = f"{module}.{normalized_key}"
                if dotted in FORBIDDEN_CALLS:
                    return ("path", dotted)

    elif isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and _normalize(node.args[1].value) in _GETATTR_BYPASS_NAMES
        ):
            return ("getattr", _normalize(node.args[1].value))
    return None


def _collect_module_aliases(tree: ast.AST) -> dict[str, str]:
    """Build a ``{local_name: original_module}`` map for ``import M as A``.

    Covers ``import nuke``, ``import nuke as N``, ``import os.path as P``
    (where ``P`` is treated as ``os.path``). Used to expand
    ``N.scriptClose`` into ``nuke.scriptClose`` during the AST scan.

    Names are NFKC-normalized so look-alike Unicode payloads collapse
    onto the ASCII module name.
    """
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for name_node in node.names:
            original = _normalize(name_node.name)
            bound = _normalize(name_node.asname or name_node.name.split(".", 1)[0])
            # When ``import a.b.c`` lands without ``as``, Python binds
            # the top-level package ``a``; the user reaches ``b.c`` via
            # attribute access. We only need the binding name here.
            if name_node.asname:
                mapping[bound] = original
            else:
                mapping[bound] = bound  # ``import os`` -> os == os
    return mapping


def _collect_dangerous_aliases(tree: ast.AST) -> dict[str, tuple[str, str]]:
    """Build a ``{local_name: (kind, target)}`` map of names bound to
    dangerous callables anywhere in ``tree``.

    Covers binding shapes:
      1. ``fn = nuke.scriptClose`` (Assign / AnnAssign).
      2. ``from os import remove`` (with optional ``as r``).
      3. ``from os import *`` (wildcard) - bind every forbidden name.
      4. ``fn = getattr(nuke, "scriptClose")``.
      5. Walrus targets: ``(fn := nuke.scriptClose)``.
      6. ``import nuke as N``: ``N.scriptClose`` becomes recognisable in
         ``_ast_scan`` via the module-alias map (collected separately).
      7. ``fn = sys.modules["nuke"].scriptClose`` /
         ``fn = globals()["nuke"].scriptClose`` /
         ``fn = vars(nuke)["scriptClose"]`` -- string-key bypasses of
         the import sandbox.
      8. One-step dangerous-returner functions: ``def evil(): return
         nuke.scriptClose`` followed by ``evil()()`` taints the call
         site through the function name.

    Conservative: a name that is ever assigned a dangerous value stays
    tainted for the rest of the scan. Reassignment to something safe
    later does not un-taint. Names are NFKC-normalized.
    """

    module_aliases = _collect_module_aliases(tree)
    aliases: dict[str, tuple[str, str]] = {}

    def _record(name: str, alias: tuple[str, str] | None) -> None:
        if alias is not None:
            aliases[_normalize(name)] = alias

    def _classify(value: ast.AST) -> tuple[str, str] | None:
        return _classify_alias_source(value, module_aliases=module_aliases)

    # First pass: collect dangerous-returner function names. A function
    # whose body ends in ``return <forbidden>`` taints calls of the
    # function name. Walked in a separate pre-pass so the second pass
    # can treat ``evil()`` as a dangerous alias.
    returner_aliases: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Return) or stmt.value is None:
                continue
            alias = _classify(stmt.value)
            if alias is not None:
                returner_aliases[_normalize(node.name)] = alias
                break

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            alias = _classify(node.value)
            if alias is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    _record(target.id, alias)

        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            alias = _classify(node.value)
            if alias is None:
                continue
            if isinstance(node.target, ast.Name):
                _record(node.target.id, alias)

        elif isinstance(node, ast.NamedExpr):
            # ``(fn := nuke.scriptClose)`` walrus operator. The target is
            # always an ast.Name per the grammar.
            alias = _classify(node.value)
            if alias is not None and isinstance(node.target, ast.Name):
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

    # Merge in the dangerous-returner map last so explicit assignments
    # win on collision.
    for name, alias in returner_aliases.items():
        aliases.setdefault(name, alias)

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


def _open_call_writes(node: ast.Call) -> bool:
    """True when ``node`` is an ``open(...)`` call whose ``mode`` argument
    is a literal string containing one of ``w``, ``a``, ``x``, ``+``.

    Inspects the second positional argument and the ``mode=`` keyword.
    Only triggers on ``open`` -- both the bare name and the
    ``builtins.open`` / ``io.open`` attribute forms. Non-string mode
    arguments (a variable, an f-string) are treated as unknown and not
    flagged here; the regex backstop covers SyntaxError-level payloads.
    """
    func = node.func
    if isinstance(func, ast.Name):
        name = _normalize(func.id)
    elif isinstance(func, ast.Attribute):
        name = _normalize(func.attr)
    else:
        return False
    if name != "open":
        return False

    mode: str | None = None
    if (
        len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        mode = node.args[1].value
    if mode is None:
        for kw in node.keywords:
            if (
                kw.arg == "mode"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                mode = kw.value.value
                break
    if mode is None:
        return False
    return any(c in mode for c in "wax+")


def _ast_scan(tree: ast.AST) -> list[Finding]:
    """Walk the AST and report every dangerous construct.

    Folds together direct dotted calls, bare-name calls, alias chains,
    ``__import__`` usage, ``getattr(obj, "<literal>")`` bypasses, the
    ``sys.modules[...]`` / ``globals()[...]`` / ``vars(...)`` indirection
    family, and write-mode ``open()`` calls.
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

    def _record_write_open(lineno: int | None) -> None:
        msg = "open() with write/append/create mode - file writing"
        if msg in seen:
            return
        seen.add(msg)
        findings.append(
            Finding(
                kind="write_open",
                severity="error",
                message=msg,
                lineno=lineno,
            )
        )

    module_aliases = _collect_module_aliases(tree)
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

        lineno = getattr(node, "lineno", None)

        # Write-mode open() detection -- AST-based, replaces the regex
        # pass for syntactically-valid payloads.
        if _open_call_writes(node):
            _record_write_open(lineno)

        # Dotted dangerous call targets, with module-alias rewrite.
        if isinstance(node.func, ast.Attribute):
            path = _resolve_with_module_aliases(node.func, module_aliases)
            if path and path in FORBIDDEN_CALLS:
                _record(FORBIDDEN_CALLS[path], lineno)

            # ``sys.modules['nuke'].scriptClose()`` / ``globals()['nuke'].
            # scriptClose()`` -- chain on a string-key indirection.
            chain_attrs: list[str] = []
            current: ast.AST = node.func
            while isinstance(current, ast.Attribute):
                chain_attrs.append(_normalize(current.attr))
                current = current.value
            chain_attrs.reverse()
            indirected = _string_module_indirection(current)
            if indirected is not None and chain_attrs:
                dotted = ".".join([indirected, *chain_attrs])
                if dotted in FORBIDDEN_CALLS:
                    _record(
                        f"{FORBIDDEN_CALLS[dotted]} (via sys.modules/globals/vars indirection)",
                        lineno,
                    )

        # Bare-name calls. Either a direct reference to a banned name
        # (``__import__(...)``) OR a call through a tainted alias.
        if isinstance(node.func, ast.Name):
            name = _normalize(node.func.id)
            if name == "__import__":
                _record(FORBIDDEN_NAMES["__import__"], lineno)
            aliased = alias_map.get(name)
            if aliased is not None:
                msg = _message_for_alias(*aliased)
                if msg:
                    _record(msg, lineno)

        # getattr(obj, "<literal>") bypass.
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            bypass = _normalize(node.args[1].value)
            if bypass in _GETATTR_BYPASS_NAMES:
                _record(_GETATTR_BYPASS_NAMES[bypass], lineno)

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

    Primary pass is an AST walk. When ``ast.parse`` rejects the payload
    we fall back to the regex backstop AND the RAW pattern set, which
    inspects string-literal content (e.g. ``open("foo", "w")`` mode
    arguments). Crash heuristics only run when AST parsing succeeds.

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
# [python {...}] brace form covered: greedy [^\]]* matches braces.
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

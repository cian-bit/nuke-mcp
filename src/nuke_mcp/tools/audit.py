"""Read-only audit + QC tools (Phase C9).

These tools never mutate the comp. They scan the node graph and root
script state for policy / convention / settings drift, then emit a
flat ``findings`` list whose shape is::

    {
        "severity": "error" | "warning" | "info",
        "node": <str>,                  # node name (or "" / "__root__" for script-level)
        "message": <str>,               # human-readable description
        "fix_suggestion": <str | None>, # optional single-line hint
        ...                             # tool-specific extras (e.g. "path")
    }

The audit suite is intentionally hands-off: it never auto-fixes.
Severity is the artist's signal -- the model surfaces findings, the
artist decides whether to act.

The lone exception is ``qc_viewer_pair``: it BUILDS a Switch + Grade
diff chain so the artist can flip between two streams visually. That
is annotated ``BENIGN_NEW`` (creates new nodes, doesn't lose work).

Coordination with the C2 colour module
--------------------------------------
``audit_acescct_consistency`` is owned long-term by C2's
``tools.color`` module. Until C2 ships, this module exposes a thin
wrapper that returns a degraded shape so the tool slot is reserved
on the wire. The wrapper imports lazily so a missing C2 module never
blocks audit registration.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from nuke_mcp.annotations import BENIGN_NEW, READ_ONLY
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def _delegate_acescct() -> dict[str, Any] | None:
    """Try to import + call ``tools.color.audit_acescct_consistency``.

    Returns the delegate's result dict on success, or ``None`` if the C2
    module hasn't shipped yet / doesn't expose the symbol. Any import
    error or attribute miss is swallowed so the audit profile can load
    even when colour-side work is in flight on a parallel branch.
    """
    try:
        color_mod = import_module("nuke_mcp.tools.color")
    except ImportError:
        return None
    delegate = getattr(color_mod, "audit_acescct_consistency", None)
    if delegate is None:
        return None
    try:
        return delegate()
    except Exception:
        # Defensive: the colour module is parallel work; if it raises
        # we'd rather show degraded output than blow up the audit call.
        return None


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="audit", annotations=READ_ONLY)
    @nuke_command("audit_acescct_consistency")
    def audit_acescct_consistency() -> dict:
        """Scan the comp for ACEScct colourspace inconsistencies.

        Read-only QC pass. Real implementation lives in C2's
        ``tools.color`` module; this wrapper delegates when present and
        returns a graceful degraded payload otherwise so the tool slot
        is stable on the wire.

        Returns:
            ``{findings: list[AuditFinding], note?: str}`` -- when C2 is
            not yet installed the ``findings`` list is empty and a
            ``note`` field explains the gap.
        """
        # TODO(C2): drop this delegate once tools.color.audit_acescct_consistency
        # ships. The call site signature stays the same; the C2 implementation
        # owns the real logic (ACEScct knob scan, OCIO context check, etc.).
        delegated = _delegate_acescct()
        if delegated is not None:
            return delegated
        return {
            "findings": [],
            "note": "C2 color module not yet installed",
        }

    @nuke_tool(ctx, profile="audit", annotations=READ_ONLY)
    @nuke_command("audit_write_paths")
    def audit_write_paths(allow_roots: list[str] | None = None) -> dict:
        """Flag Write nodes whose path is outside the allow-listed roots.

        The default allow-list is ``["$SS"]`` (Salt Spill sandbox).
        ``$VAR`` tokens are expanded from the addon-side environment;
        unset variables produce a finding noting the missing expansion
        rather than silently allowing every path.

        Args:
            allow_roots: list of root prefixes. Tokens starting with
                ``$`` are env-expanded addon-side. Defaults to
                ``["$SS"]`` when ``None``.

        Returns:
            ``{findings: list[AuditFinding]}`` -- one entry per
            offending Write node with ``severity="error"`` and the
            offending ``path``.
        """
        if allow_roots is None:
            allow_roots = ["$SS"]
        params: dict[str, Any] = {"allow_roots": allow_roots}
        return run_on_main("audit_write_paths", params, "read")

    @nuke_tool(ctx, profile="audit", annotations=READ_ONLY)
    @nuke_command("audit_naming_convention")
    def audit_naming_convention(prefix: str = "ss_", case_sensitive: bool = True) -> dict:
        """Flag nodes whose names don't match the prefix convention.

        Severity is ``warning`` -- a misnamed node is artist-visible
        clutter rather than a render-blocking error.

        Args:
            prefix: required leading substring on every node name.
                Defaults to ``"ss_"`` (Salt Spill convention).
            case_sensitive: when False, the comparison ignores case.
                Defaults to True.

        Returns:
            ``{findings: list[AuditFinding]}``.
        """
        params: dict[str, Any] = {
            "prefix": prefix,
            "case_sensitive": case_sensitive,
        }
        return run_on_main("audit_naming_convention", params, "read")

    @nuke_tool(ctx, profile="audit", annotations=READ_ONLY)
    @nuke_command("audit_render_settings")
    def audit_render_settings(
        expected_fps: float = 24.0,
        expected_format: str = "2048x1080",
        expected_range: tuple[int, int] | None = None,
    ) -> dict:
        """Flag root-script settings that don't match the expected values.

        Each root-knob mismatch becomes one finding with
        ``severity="error"`` and ``node="__root__"``. Passing
        ``expected_range=None`` skips the frame-range check entirely
        so callers who only care about fps + format don't get noise.

        Args:
            expected_fps: required script fps.
            expected_format: required script format (Nuke format name
                or ``WIDTHxHEIGHT`` shorthand).
            expected_range: optional ``(first, last)`` tuple. ``None``
                disables the check.

        Returns:
            ``{findings: list[AuditFinding]}``.
        """
        params: dict[str, Any] = {
            "expected_fps": expected_fps,
            "expected_format": expected_format,
        }
        if expected_range is not None:
            params["expected_range"] = list(expected_range)
        return run_on_main("audit_render_settings", params, "read")

    @nuke_tool(ctx, profile="audit", annotations=BENIGN_NEW)
    @nuke_command("qc_viewer_pair")
    def qc_viewer_pair(beauty: str, recombined: str) -> dict:
        """Build a Switch + Grade(gain=10) diff chain for visual QC.

        Wires both ``beauty`` and ``recombined`` into a Switch so the
        artist can A/B them, plus a Merge(operation=difference) +
        Grade(gain=10) branch that amplifies any per-pixel divergence.
        Returns the Switch's NodeRef -- the artist points the viewer at
        it.

        Read-only-ish: creates new nodes but never mutates the inputs.
        Annotated ``BENIGN_NEW``.

        Args:
            beauty: name of the original beauty stream.
            recombined: name of the recombined / processed stream to
                diff against beauty.

        Returns:
            NodeRef of the Switch (``{name, type, x, y, inputs}``).
        """
        params: dict[str, Any] = {
            "beauty": beauty,
            "recombined": recombined,
        }
        return run_on_main("qc_viewer_pair", params, "mutate")

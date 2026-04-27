"""OCIO / ACEScct color-management tools (Phase C2).

Five tools that surface Nuke's root-level colour-management state and
provide audit + conversion primitives for ACEScg/ACEScct pipelines.
Every tool is a thin dispatch to a typed addon handler via
``run_on_main``; the addon-side handler runs on Nuke's main thread and
performs validation against the active OCIO config.

Tool taxonomy:

* ``get_color_management`` -- read the root knobs that drive OCIO. Pure
  read, no mutation.
* ``set_working_space`` -- mutate ``nuke.root()['workingSpaceLUT']``.
  Tagged ``DESTRUCTIVE`` because flipping the working space invalidates
  every downstream pixel calculation in the script.
* ``audit_acescct_consistency`` -- pure read. Returns a list of
  ``AuditFinding`` dicts (severity / node / message / fix_suggestion)
  for Reads with mismatched colorspace, Grades downstream of ACEScg
  without an ACEScct conversion, and Writes whose colorspace doesn't
  match a scene-linear delivery target.
* ``convert_node_colorspace`` -- atomic primitive that wraps a target
  node with a leading ``OCIOColorSpace(in_cs -> out_cs)`` upstream and
  a trailing ``OCIOColorSpace(out_cs -> in_cs)`` downstream so the
  surrounding graph still sees the original space. ``BENIGN_NEW`` per
  the C1 convention (Nuke auto-uniquifies; a duplicate call yields
  fresh ``OCIOColorSpace2`` etc.).
* ``create_ocio_colorspace`` -- single OCIOColorSpace primitive
  returning a ``NodeRef``. ``name=`` keyword makes the call idempotent
  when the node already exists with matching class + inputs.

Idempotency contract: matches ``tracking.py`` / ``deep.py``. ``name=``
present + class + inputs match -> existing NodeRef returned. ``name``
None -> always creates a fresh node.
"""

from __future__ import annotations

from nuke_mcp.annotations import BENIGN_NEW, DESTRUCTIVE, READ_ONLY
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="color", annotations=READ_ONLY)
    @nuke_command("get_color_management")
    def get_color_management() -> dict:
        """Return the script's color-management state.

        Reads ``nuke.root()`` knobs:

        * ``colorManagement`` -- ``"Nuke"`` or ``"OCIO"``.
        * ``OCIO_config`` -- selected OCIO config (e.g. ``aces_1.3`` or
          ``custom``).
        * ``workingSpaceLUT`` -- the script's working colourspace. In an
          ACES config this is typically ``ACES - ACEScg``.
        * ``defaultViewerLUT`` -- viewer transform applied at display.
        * ``monitorLut`` -- monitor LUT used by Nuke when no per-viewer
          override is active.

        Returns a dict with keys ``color_management``, ``ocio_config``,
        ``working_space``, ``default_view``, ``monitor_lut``. Knobs that
        don't exist on the running Nuke build come back as empty
        strings rather than missing keys -- the wire shape stays stable
        across versions.
        """
        return run_on_main("get_color_management", {}, "read")

    @nuke_tool(ctx, profile="color", annotations=DESTRUCTIVE)
    @nuke_command("set_working_space")
    def set_working_space(space: str) -> dict:
        """Set the script's working colourspace.

        Args:
            space: target working-space name. Must be one of the
                colourspaces enumerated by the active OCIO config; the
                addon validates against ``workingSpaceLUT.values()``
                before writing.

        Tagged ``DESTRUCTIVE`` because flipping the working space
        re-interprets every existing pixel calculation -- Reads,
        Grades, Writes downstream all change behaviour without
        explicit re-grading. Pair with a ``confirm=True`` gate at the
        caller level when wiring into an agent.
        """
        return run_on_main("set_working_space", {"space": space}, "mutate")

    @nuke_tool(ctx, profile="color", annotations=READ_ONLY)
    @nuke_command("audit_acescct_consistency")
    def audit_acescct_consistency(strict: bool = True) -> dict:
        """Walk the graph and flag colour-management mistakes.

        Findings list shape::

            {findings: [
                {severity, node, message, fix_suggestion},
                ...
            ]}

        Severity is one of ``"warning"`` / ``"error"``. The audit is
        read-only -- no nodes are created, deleted, or modified.

        Heuristics applied:

        1. Read with ``colorspace`` left at ``default`` BUT path
           contains ``_sRGB`` or ends in ``.png`` / ``.jpg`` /
           ``.jpeg``. Likely should be tagged ``sRGB - Texture`` (or
           the equivalent in the active config).
        2. Grade downstream of an ACEScg pipeline (i.e. working space
           is ACEScg or upstream OCIOColorSpace converts to ACEScg)
           without a preceding ``OCIOColorSpace ACEScg -> ACEScct``.
           ACEScg gradient is non-linear under multiplicative grading;
           ACEScct is the working space artists actually expect.
        3. Write whose ``colorspace`` knob doesn't match a
           scene-linear delivery target. A linear EXR write tagged
           ``sRGB`` is a common footgun.

        Args:
            strict: when ``False``, demotes the Grade-without-ACEScct
                heuristic from ``warning`` to ``info`` -- handy for
                older comps where a deliberate non-ACEScct grade is
                common. Reads/Writes still fire at full severity.
        """
        return run_on_main(
            "audit_acescct_consistency",
            {"strict": strict},
            "read",
        )

    @nuke_tool(ctx, profile="color", annotations=BENIGN_NEW)
    @nuke_command("convert_node_colorspace")
    def convert_node_colorspace(node: str, in_cs: str, out_cs: str) -> dict:
        """Wrap ``node`` in a colourspace conversion pair.

        Inserts an ``OCIOColorSpace(in_cs -> out_cs)`` upstream of
        ``node`` and a matching ``OCIOColorSpace(out_cs -> in_cs)``
        downstream so the rest of the graph still sees ``in_cs``. The
        original wiring is rebuilt: every downstream consumer that was
        feeding from ``node`` now feeds from the trailing converter.

        Args:
            node: target node to wrap.
            in_cs: current colourspace of ``node`` (passed to the
                leading converter's input field).
            out_cs: temporary working colourspace inside the wrapped
                region (passed to the leading converter's output
                field).

        Returns a dict ``{leading: NodeRef, trailing: NodeRef,
        wrapped: <node name>}`` so callers know which converters to
        clean up if they roll the change back.
        """
        return run_on_main(
            "convert_node_colorspace",
            {"node": node, "in_cs": in_cs, "out_cs": out_cs},
            "mutate",
        )

    @nuke_tool(ctx, profile="color", annotations=BENIGN_NEW)
    @nuke_command("create_ocio_colorspace")
    def create_ocio_colorspace(
        input_node: str,
        in_cs: str,
        out_cs: str,
        name: str | None = None,
    ) -> dict:
        """Create a single OCIOColorSpace node downstream of ``input_node``.

        Args:
            input_node: source node fed into the converter.
            in_cs: ``in_colorspace`` knob value.
            out_cs: ``out_colorspace`` knob value.
            name: explicit node name. When supplied AND an
                OCIOColorSpace of that name already exists with the
                same input AND the same in/out colourspace pair, the
                existing NodeRef is returned (idempotent re-call). A
                name collision with a different node class or input
                wiring raises a structured error rather than silently
                overwriting.

        Returns a flat ``NodeRef``: ``{name, type, x, y, inputs}``.
        """
        params: dict = {
            "input_node": input_node,
            "in_cs": in_cs,
            "out_cs": out_cs,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("create_ocio_colorspace", params, "mutate")

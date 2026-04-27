"""AOV / channel rebuild for Karma EXR pipelines (Phase C3).

Multi-layer Karma EXR renders ship every render path -- beauty plus
diffuse / specular / sss / volume / etc. -- packed into a single
multi-channel EXR. Comp-side, those layers need to be split back out
(Shuffle per layer), recombined (additive Merge over the beauty), and
QC'd (viewer A/B vs the original beauty).

Three tools live here:

* :func:`detect_aov_layers` -- read-only. Inspects ``Read.metadata()``
  ``exr/*`` keys plus ``Read.channels()`` and returns the layer
  catalogue (``{layers: list[str], format: str, channels_per_layer:
  dict}``). Caller picks which subset to wire.
* :func:`setup_karma_aov_pipeline` -- workflow tool. Builds a full
  Shuffle-per-layer + reconstruction Merge2 + Remove keep=rgba + QC
  Switch / diff Grade pair, all wrapped in a Group named
  ``KarmaAOV_<shot>``. Idempotent on the ``name=`` kwarg.
* :func:`setup_aov_merge` -- the legacy additive merge of N pre-split
  Read nodes. Migrated here from ``channels.py`` and reworked as a
  typed handler (used to ship f-string ``execute_python``). Same
  public signature; same wire shape.

The Karma layer dictionary mirrors what SideFX ships out of Solaris by
default; the addon-side handler treats unknown layers as a fallback
(``Shuffle`` on the layer name as-is) and reports them under
``unknown_layers`` so the caller can spot AOV-name drift.
"""

from __future__ import annotations

from nuke_mcp.annotations import BENIGN_NEW, READ_ONLY
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="aov", annotations=READ_ONLY)
    @nuke_command("detect_aov_layers")
    def detect_aov_layers(read_node: str) -> dict:
        """Inspect a Read node for Karma-style AOV layers.

        Parses ``Read.metadata()`` ``exr/*`` keys and ``Read.channels()``
        to surface every layer present in the EXR. Read-only -- does
        not mutate the script.

        Args:
            read_node: name of the Read node to inspect.

        Returns:
            Dict with keys:

            * ``layers`` -- ordered list of layer names found
              (e.g. ``["rgba", "diffuse_direct", "specular_indirect",
              "depth", "P", "N", "cryptomatte_object00"]``).
            * ``format`` -- Read format string (``"HD 1920x1080"``)
              when available.
            * ``channels_per_layer`` -- dict mapping each layer to its
              ordered channel sub-names (e.g.
              ``{"rgba": ["red", "green", "blue", "alpha"],
                "depth": ["z"]}``).
        """
        return run_on_main("detect_aov_layers", {"read_node": read_node}, "read")

    @nuke_tool(ctx, profile="aov", annotations=BENIGN_NEW)
    @nuke_command("setup_karma_aov_pipeline")
    def setup_karma_aov_pipeline(
        read_path: str,
        name: str | None = None,
    ) -> dict:
        """Build the full Karma EXR AOV split-and-rebuild pipeline.

        Creates a Read at ``read_path`` (re-uses an existing Read with
        that file path when present), then for every detected AOV layer
        builds a Shuffle into ``rgba``, plus a reconstruction Merge2
        (additive, ``operation=plus``) chain on top of the beauty pass,
        a ``Remove keep=rgba`` cleanup, and a QC viewer pair: a Switch
        between the original beauty and the reconstructed Merge plus a
        diff (``Merge difference`` followed by a ``Grade`` with
        ``multiply=10``).

        The whole sub-graph is wrapped in a Group named
        ``KarmaAOV_<shot>`` (or the explicit ``name`` if supplied) so
        the DAG stays tidy. Idempotent on the ``name`` kwarg --
        re-calling with the same name returns the existing Group's
        NodeRef.

        Args:
            read_path: file path for the Karma EXR sequence
                (e.g. ``$SS/renders/ss_0170/v001/ss_0170.####.exr``).
                The path is forwarded to a Read node verbatim; the
                addon-side handler validates the file actually contains
                AOV layers and falls back to a ``rgba``-only pipeline
                when no layers are detected.
            name: explicit Group name. When supplied AND a Group of
                that name already exists, the existing NodeRef is
                returned without rebuilding the sub-graph.

        Returns:
            ``NodeRef`` of the wrapper Group (``{name, type, x, y,
            inputs}``) plus a ``layers`` list summarising which AOV
            layers landed in the rebuild and any ``unknown_layers``
            present in the EXR but absent from the canonical layer
            dictionary.
        """
        params: dict = {"read_path": read_path}
        if name is not None:
            params["name"] = name
        return run_on_main("setup_karma_aov_pipeline", params, "mutate")

    @nuke_tool(ctx, profile="aov", annotations=BENIGN_NEW)
    @nuke_command("setup_aov_merge")
    def setup_aov_merge(read_nodes: str) -> dict:
        """Additively merge pre-split AOV Read nodes (legacy chain).

        Builds an additive ``Merge2 operation=plus`` chain over N
        Read nodes that already hold the per-AOV passes split into
        separate files. For modern multi-layer Karma EXRs, prefer
        :func:`setup_karma_aov_pipeline` -- this tool is kept for
        backwards compatibility with workflows that ship pre-split
        passes.

        Args:
            read_nodes: comma-separated list of Read node names to
                merge. Whitespace around each name is stripped.

        Migrated from ``tools/channels.py`` in Phase C3 and reworked
        as a typed handler -- the previous implementation shipped
        ``execute_python`` payloads.
        """
        names = [n.strip() for n in read_nodes.split(",") if n.strip()]
        return run_on_main("setup_aov_merge", {"read_nodes": names}, "mutate")

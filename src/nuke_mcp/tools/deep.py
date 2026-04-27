"""Deep-comp primitives.

C1 atomic primitives for DeepRecolor, DeepMerge, DeepHoldout,
DeepTransform, and DeepToImage. Mirrors the pattern in ``tracking.py``:
every tool dispatches to a typed addon handler via ``run_on_main`` and
returns a flat ``NodeRef`` (``{name, class, xpos, ypos, inputs}``).

Idempotency: when ``name=`` is supplied AND a node of the same class
with matching inputs exists at that name, the addon returns the
existing NodeRef without creating a duplicate. When ``name`` is None,
the tool is BENIGN_NEW (Nuke auto-uniquifies; a second call yields a
fresh ``DeepMerge2`` rather than mutating the first).
"""

from __future__ import annotations

from typing import Literal

from nuke_mcp.annotations import BENIGN_NEW
from nuke_mcp.main_thread import run_on_main
from nuke_mcp.tools._helpers import nuke_command

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("create_deep_recolor")
    def create_deep_recolor(
        deep_node: str,
        color_node: str,
        target_input_alpha: bool = True,
        name: str | None = None,
    ) -> dict:
        """Create a DeepRecolor fed by a deep stream + a 2D colour input.

        Args:
            deep_node: deep input on slot 0.
            color_node: 2D colour input on slot 1 (replaces the
                deep stream's RGB while preserving sample depth).
            target_input_alpha: forward the colour input's alpha into
                the deep samples.
            name: idempotent re-call key.
        """
        params: dict = {
            "deep_node": deep_node,
            "color_node": color_node,
            "target_input_alpha": target_input_alpha,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("create_deep_recolor", params, "mutate")

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("create_deep_merge")
    def create_deep_merge(
        a_node: str,
        b_node: str,
        op: Literal["over", "holdout"] = "over",
        name: str | None = None,
    ) -> dict:
        """Create a DeepMerge between two deep streams.

        Args:
            a_node: first deep stream (slot 0).
            b_node: second deep stream (slot 1).
            op: merge operation -- ``over`` (composite) or ``holdout``
                (subtract).
            name: idempotent re-call key.
        """
        params: dict = {
            "a_node": a_node,
            "b_node": b_node,
            "op": op,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("create_deep_merge", params, "mutate")

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("create_deep_holdout")
    def create_deep_holdout(
        subject_node: str,
        holdout_node: str,
        name: str | None = None,
    ) -> dict:
        """Create a DeepHoldout: subject minus holdout.

        Args:
            subject_node: deep stream to keep (slot 0).
            holdout_node: deep stream subtracted from the subject
                (slot 1).
            name: idempotent re-call key.
        """
        params: dict = {
            "subject_node": subject_node,
            "holdout_node": holdout_node,
        }
        if name is not None:
            params["name"] = name
        return run_on_main("create_deep_holdout", params, "mutate")

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("create_deep_transform")
    def create_deep_transform(
        input_node: str,
        translate: tuple[float, float, float] = (0.0, 0.0, 0.0),
        name: str | None = None,
    ) -> dict:
        """Create a DeepTransform with an optional translate vector.

        Args:
            input_node: deep input.
            translate: XYZ translate as a 3-tuple.
            name: idempotent re-call key.
        """
        params: dict = {
            "input_node": input_node,
            "translate": list(translate),
        }
        if name is not None:
            params["name"] = name
        return run_on_main("create_deep_transform", params, "mutate")

    @ctx.mcp.tool(annotations=BENIGN_NEW, output_schema=None)
    @nuke_command("deep_to_image")
    def deep_to_image(
        input_node: str,
        name: str | None = None,
    ) -> dict:
        """Flatten a deep stream to a 2D image via DeepToImage.

        Args:
            input_node: deep input.
            name: idempotent re-call key.
        """
        params: dict = {"input_node": input_node}
        if name is not None:
            params["name"] = name
        return run_on_main("deep_to_image", params, "mutate")

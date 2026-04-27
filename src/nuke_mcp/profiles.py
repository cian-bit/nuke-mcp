"""Skill profile registry (Phase B4).

The MCP spec lets a server change its tool list at runtime via the
``notifications/tools/list_changed`` notification. Phase B4 uses that
to ship a *paginated* tool surface: the model sees only the ``core``
profile (~38 tools) at boot, then calls ``load_profile("tracking")``
to surface tracker tools when the user actually starts a tracking
task. This trades context-window weight for a one-tool-call latency
hit when a task category goes live.

The profile name -> tool name mapping in :data:`PROFILES` mirrors the
``_profile`` attribute stamped by ``nuke_mcp.registry.nuke_tool``.
That stamping is the source of truth: this module is the human-friendly
catalog the ``load_profile`` / ``unload_profile`` / ``list_profiles``
runtime tools dispatch through.

Adding a new tool with ``@nuke_tool(profile="X")`` and a new profile
name? Add an entry below and a description in :data:`PROFILE_DESCRIPTIONS`.
"""

from __future__ import annotations

# Profile name -> ordered list of tool function-names. Cross-referenced
# at server build-time against the ``_profile`` attribute the registry
# decorator stamps on each function -- ``register_tools`` rejects a
# mismatch loudly so the catalog can't silently drift.
PROFILES: dict[str, list[str]] = {
    # Core: always-loaded baseline. Read paths, basic graph mutations,
    # script-level metadata, the digest pair, expressions/keyframes,
    # roto, viewer, and the scripted setup_* tools that don't fit a
    # specialised profile.
    "core": [
        # read.py
        "read_comp",
        "read_node_detail",
        "snapshot_comp",
        "diff_comp",
        # graph.py
        "create_node",
        "delete_node",
        "find_nodes",
        "list_nodes",
        "connect_nodes",
        "auto_layout",
        "modify_node",
        "disconnect_node_input",
        "set_node_position",
        # knobs.py
        "get_knob",
        "set_knob",
        # script.py
        "get_script_info",
        "save_script",
        "load_script",
        "set_frame_range",
        # render.py
        "setup_write",
        "render_frames",
        "setup_precomp",
        "list_precomps",
        # channels.py
        "list_channels",
        "shuffle_channels",
        # viewer.py
        "view_node",
        "set_viewer_lut",
        # comp.py (the graph-shape ones)
        "setup_merge",
        "setup_transform",
        "setup_denoise",
        # expressions.py
        "set_expression",
        "clear_expression",
        "set_keyframe",
        "list_keyframes",
        # roto.py
        "create_roto",
        "list_roto_shapes",
        # digest.py
        "scene_digest",
        "scene_delta",
        # profiles.py (the runtime profile-loader itself lives in core
        # so the model can always surface other profiles)
        "list_profiles",
        "load_profile",
        "unload_profile",
        # tasks.py (B2 Tasks primitive — the meta tools for inspecting
        # async work always live in core so model can navigate them)
        "tasks_list",
        "tasks_get",
        "tasks_cancel",
        "tasks_resume",
    ],
    # Graph-advanced: bulk-mutation + escape-hatch + selection-driven
    # reads. Heavier-weight surfaces that a typical comp-read session
    # doesn't need.
    "graph_advanced": [
        "create_nodes",
        "set_knobs",
        "execute_python",
        "read_selected",
    ],
    # Colour-pipeline setup tools.
    "color": [
        "setup_keying",
        "setup_color_correction",
    ],
    # AOV merge / Karma EXR layer recombine.
    "aov": [
        "setup_aov_merge",
    ],
    # 2D + 3D tracking primitives.
    "tracking": [
        "setup_camera_tracker",
        "setup_planar_tracker",
        "setup_tracker4",
        "bake_tracker_to_corner_pin",
        "solve_3d_camera",
        "bake_camera_to_card",
    ],
    # Deep-comp primitives.
    "deep": [
        "create_deep_recolor",
        "create_deep_merge",
        "create_deep_holdout",
        "create_deep_transform",
        "deep_to_image",
    ],
    # C9 audit + QC tools. Read-only scans plus one BENIGN_NEW QC
    # builder (qc_viewer_pair). audit_acescct_consistency is the C2
    # colour module's owned slot; the wrapper here delegates when C2
    # is present and returns a degraded payload otherwise.
    "audit": [
        "audit_acescct_consistency",
        "audit_write_paths",
        "audit_naming_convention",
        "audit_render_settings",
        "qc_viewer_pair",
    ],
}


# Human-readable summary of what each profile contains. Returned by
# the ``list_profiles`` tool so the model can pick the right
# ``load_profile`` argument without trial and error.
PROFILE_DESCRIPTIONS: dict[str, str] = {
    "core": (
        "Always-loaded baseline: graph reads, basic mutations, script "
        "metadata, expressions, viewer, roto, scene digest."
    ),
    "graph_advanced": ("Bulk graph operations and the execute_python escape hatch."),
    "color": "Keying and colour-correction setup tools.",
    "aov": "AOV merge / Karma EXR layer recombination.",
    "tracking": "2D + 3D tracking and camera-solve primitives.",
    "deep": "Deep-comp primitives (DeepRecolor, DeepMerge, DeepHoldout etc.).",
    "audit": (
        "Read-only QC scans (write paths, naming, render settings, "
        "ACEScct consistency) plus a Switch+Grade visual-diff builder."
    ),
}


# Profiles loaded by default when no explicit ``active_profiles`` is
# passed to ``build_server``. Surfaces only ``core`` -- everything else
# is opt-in via ``load_profile``.
DEFAULT_PROFILES: tuple[str, ...] = ("core",)


def all_profile_names() -> list[str]:
    """Return every known profile name in :data:`PROFILES`."""
    return list(PROFILES.keys())


def tools_for_profile(name: str) -> list[str]:
    """Return the tool name list for ``name``, or ``[]`` if unknown."""
    return list(PROFILES.get(name, []))


def profile_for_tool(tool_name: str) -> str | None:
    """Reverse lookup: which profile does ``tool_name`` live in?

    Used by the runtime ``load_profile`` tool to bail out cleanly if a
    profile lists a tool that no longer exists in the registry.
    """
    for profile_name, tools in PROFILES.items():
        if tool_name in tools:
            return profile_name
    return None


__all__ = [
    "DEFAULT_PROFILES",
    "PROFILES",
    "PROFILE_DESCRIPTIONS",
    "all_profile_names",
    "profile_for_tool",
    "tools_for_profile",
]

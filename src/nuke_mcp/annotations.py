"""MCP tool-annotation presets.

Pass these to ``@mcp.tool(annotations=...)`` instead of inline dicts so the
intent of every tool is greppable and we can flip behavior in one place if
the spec evolves. Combine with the dict-merge operator: ``DESTRUCTIVE | OPEN_WORLD``.

Hint semantics follow the MCP 2025-06-18 spec (and forward to 2025-11-25):
  * ``readOnlyHint`` -- the tool does not mutate Nuke state.
  * ``idempotentHint`` -- repeating the call with the same args yields the
    same end state. Naming-keyed mutates qualify.
  * ``destructiveHint`` -- the tool can lose work or replace user state.
    Pair with ``confirm=True`` gates at the tool level.
  * ``openWorldHint`` -- the tool reaches outside the Nuke session: filesystem,
    network, child process, render farm.
"""

from __future__ import annotations

READ_ONLY: dict[str, bool] = {"readOnlyHint": True}
IDEMPOTENT: dict[str, bool] = {"idempotentHint": True, "destructiveHint": False}
DESTRUCTIVE: dict[str, bool] = {"destructiveHint": True}
OPEN_WORLD: dict[str, bool] = {"openWorldHint": True}

# Tools that create new state but don't destroy. Distinct from
# IDEMPOTENT: a duplicate call DOES create duplicate nodes (Nuke names
# them ``Foo1``, ``Foo2`` etc.), so the second call's end-state isn't
# the same as the first. ``destructiveHint=False`` is still informative
# -- the schema test asserts every tool carries at least one explicit
# hint, of either polarity.
BENIGN_NEW: dict[str, bool] = {"destructiveHint": False}

# Common combinations -- spelled out for grep-ability.
READ_AND_IDEMPOTENT: dict[str, bool] = {**READ_ONLY, **IDEMPOTENT}
DESTRUCTIVE_OPEN: dict[str, bool] = {**DESTRUCTIVE, **OPEN_WORLD}

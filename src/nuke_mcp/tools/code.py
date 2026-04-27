"""Execute arbitrary Python code in Nuke."""

from __future__ import annotations

import logging

from nuke_mcp import connection
from nuke_mcp.annotations import DESTRUCTIVE_OPEN
from nuke_mcp.registry import nuke_tool
from nuke_mcp.tools import _safety
from nuke_mcp.tools._helpers import nuke_command

log = logging.getLogger(__name__)

if False:
    from nuke_mcp.server import ServerContext


def register(ctx: ServerContext) -> None:
    @nuke_tool(ctx, profile="graph_advanced", annotations=DESTRUCTIVE_OPEN)
    @nuke_command("execute_python")
    def execute_python(
        code: str,
        confirm: bool = False,
        allow_dangerous: bool = False,
    ) -> dict:
        """Run Python code inside Nuke's interpreter. Set ``__result__`` to return data.

        A safety scanner inspects the payload first. The following are
        blocked unless ``allow_dangerous=True`` is passed explicitly:
          * ``nuke.scriptClose`` / ``scriptClear`` / ``scriptExit`` / ``exit`` / ``delete``
          * ``nuke.removeAllKnobChanged`` / ``removeKnobChanged``
          * ``os.remove`` / ``os.unlink`` / ``os.rmdir`` / ``os.system``
          * ``shutil.rmtree`` / ``shutil.move``
          * ``subprocess.*`` (Popen, run, call, check_call, check_output)
          * ``open(path, "w"|"a"|"x")``
          * Alias chains, ``getattr`` bypass, ``__import__`` bypass

        Crash heuristics emit warnings but do not block. Findings are
        returned in the response dict under ``findings``.

        Args:
            code: Python code to execute. Assign to ``__result__`` to return data.
            confirm: Must be True to run. Call with False to preview the code and
                see scanner findings.
            allow_dangerous: Override the safety gate. Default False.
        """
        findings = _safety._detect_dangerous_code(code, allow_dangerous=allow_dangerous)
        finding_dicts = [_safety.finding_to_dict(f) for f in findings]
        errors = [f for f in findings if f.severity == "error"]

        if not confirm:
            return {
                "preview": f"will execute:\n{code}\ncall with confirm=True to run.",
                "findings": finding_dicts,
            }

        if errors and not allow_dangerous:
            log.warning("execute_python blocked: %d error finding(s)", len(errors))
            return {
                "status": "blocked",
                "findings": finding_dicts,
                "error": "code blocked by safety scanner",
            }

        result = connection.send("execute_python", code=code)
        if isinstance(result, dict) and finding_dicts:
            result.setdefault("findings", finding_dicts)
        return result

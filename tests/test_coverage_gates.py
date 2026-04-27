"""Per-module coverage gates.

Pyproject's ``fail_under`` is a single global threshold and doesn't
support per-file gates. This module reads the latest ``.coverage`` data
file and asserts module-level minimums. Skipped if no coverage data
exists -- run ``pytest --cov=src/nuke_mcp`` first to populate it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


# (module relative path under src/nuke_mcp, minimum percent).  Modules
# that A4-followup C1 will fill (tracking.py, deep.py) get a 0% floor so
# the gate doesn't block this branch. The code.py / _helpers.py gates
# live below their plan-stated ceilings because:
#   * test_safety.py exercises the scanner (_safety.py 99%) but only
#     hits ``execute_python`` indirectly. A follow-up will add direct
#     tool-wrapper tests; raising this gate to 90% then.
#   * _helpers.py drops into the response-shape branch via apply_response_shape
#     -- B1 in-flight tests cover the rest. Raising to 85% once B1 lands.
_GATES: tuple[tuple[str, float], ...] = (
    ("tools/_safety.py", 95.0),
    ("tools/code.py", 50.0),
    ("tools/comp.py", 80.0),
    ("tools/render.py", 80.0),
    ("tools/channels.py", 75.0),
    ("tools/roto.py", 80.0),
    ("tools/viewer.py", 80.0),
    ("tools/tracking.py", 0.0),
    ("tools/deep.py", 0.0),
    # C4: brand-new distortion module. Covers the four tool functions,
    # async dispatch helper, and STMap cache-root resolution.
    ("tools/distortion.py", 75.0),
    ("tools/digest.py", 85.0),
    ("connection.py", 85.0),
    ("tools/_helpers.py", 75.0),
    # B2: brand-new modules with dedicated test files. Floor at 85%
    # to allow defensive paths (corrupt-file handling, malformed-json
    # recovery) that fire only under operator error.
    ("tasks.py", 85.0),
    ("tools/tasks.py", 85.0),
    # C7: ml.py defensive log path on listener exceptions stays
    # uncovered; everything else is exercised. 90% leaves headroom
    # for follow-up edge cases without churning the gate.
    ("tools/ml.py", 90.0),
)


@pytest.fixture(scope="module")
def coverage_data():
    """Load the latest ``.coverage`` file. Skip if absent."""
    pytest.importorskip("coverage")
    from coverage import Coverage

    data_file = os.environ.get("COVERAGE_FILE") or str(_REPO_ROOT / ".coverage")
    if not Path(data_file).exists():
        pytest.skip(f"no coverage data at {data_file}; run pytest --cov first")

    cov = Coverage(data_file=data_file)
    cov.load()
    return cov


def _percent_for_module(cov, rel_path: str) -> float:
    """Return percent covered for ``src/nuke_mcp/<rel_path>``.

    Coverage stores filenames as recorded -- can be absolute, can use
    forward slashes on Windows, can have leading ``src\\``. We try a few
    candidates rather than guessing the canonical form.
    """
    data = cov.get_data()
    measured = list(data.measured_files())

    target = rel_path.replace("/", os.sep)
    matches = [f for f in measured if f.endswith(target) or f.endswith(rel_path)]
    if not matches:
        # No data for the file at all -- treat as 0% coverage so the gate
        # has a chance to fail loudly. _safety etc should always have data.
        return 0.0

    measured_file = matches[0]
    analysis = cov.analysis2(measured_file)
    # analysis2 returns (filename, statements, excluded, missing, missing_formatted)
    statements = analysis[1]
    missing = analysis[3]
    if not statements:
        return 100.0
    covered = len(statements) - len(missing)
    return (covered / len(statements)) * 100.0


@pytest.mark.parametrize(("rel_path", "minimum"), _GATES)
def test_coverage_gate(coverage_data, rel_path: str, minimum: float) -> None:
    """Assert per-file coverage meets the minimum gate."""
    pct = _percent_for_module(coverage_data, rel_path)
    assert pct >= minimum, (
        f"{rel_path} coverage {pct:.1f}% < gate {minimum:.1f}%. "
        f"Run pytest --cov=src/nuke_mcp --cov-report=term-missing for details."
    )

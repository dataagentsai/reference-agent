"""Write the scenario coverage report.

uv run python scripts/scenario_coverage.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from evals.scenario_coverage import coverage, render  # noqa: E402

DEST = pathlib.Path(__file__).resolve().parents[1] / "docs" / "SCENARIO-COVERAGE.md"

if __name__ == "__main__":
    report = coverage()
    DEST.write_text(render(report))
    print(f"{DEST}  {len(report['reached'])} of {len(report['owed'])} statements reached")

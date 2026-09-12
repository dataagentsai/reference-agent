"""Write the Pattern View from this agent's AOAS.

uv run python scripts/pattern_view.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from evals.pattern_view import entailed, render  # noqa: E402

DEST = pathlib.Path(__file__).resolve().parents[1] / "docs" / "PATTERN-VIEW.md"

if __name__ == "__main__":
    found = entailed()
    DEST.write_text(render(found))
    print(f"{DEST}  {len(found)} patterns entailed")

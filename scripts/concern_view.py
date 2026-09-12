"""Write the Concern View.

uv run python scripts/concern_view.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from evals.concern_view import gather, render  # noqa: E402

DEST = pathlib.Path(__file__).resolve().parents[1] / "docs" / "CONCERN-VIEW.md"

if __name__ == "__main__":
    by = gather()
    DEST.write_text(render(by))
    reached = sum(1 for items in by.values() if items)
    print(f"{DEST}  {sum(len(v) for v in by.values())} statements across {reached} concerns")

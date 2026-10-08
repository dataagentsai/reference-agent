"""Save the watch's scores from Langfuse, for the AAC coverage report.

    uv run python scripts/export_scores.py                       # every score
    uv run python scripts/export_scores.py --param name=watch.W-01 --out x.json

Pages `GET /api/public/v3/scores?fields=details,subject` (v2 is deprecated on
Langfuse Cloud from 16 Nov 2026) and writes the pages exactly as they came, as a
JSON array, to `reports/aac/langfuse-scores.json` — the file `aac.config.yaml`'s
langfuse source reads. Nothing is filtered or rewritten here: AAC's adapter
decides what a score evidences from the `aac` metadata the watch wrote.

Reads `LANGFUSE_URL`, `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`, as
`scripts/watch.py` does.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_harness.watch.langfuse import Langfuse  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "aac" / "langfuse-scores.json"


def export(langfuse: Langfuse, out: Path, query: dict[str, str]) -> int:
    """Write every page to `out`; returns how many scores were saved."""
    pages = list(langfuse.score_pages(**query))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pages, indent=2) + "\n")
    return sum(len(p.get("data") or []) for p in pages)


def _query(pairs: list[str]) -> dict[str, str]:
    query = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--param takes key=value, not {pair!r}")
        query[key] = value
    return query


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--param", action="append", default=[], help="an extra query parameter, key=value"
    )
    args = parser.parse_args()
    langfuse = Langfuse(
        os.environ.get("LANGFUSE_URL", "http://localhost:3000"),
        os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local-dev-only"),
        os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local-dev-only"),
    )
    saved = export(langfuse, args.out, _query(args.param))
    print(f"{saved} scores written to {args.out}")

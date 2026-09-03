"""Regenerate the A6 obligation manifest from the catalog.

`evals/a6_obligations.json` was hand-maintained, and it drifted: AAC-0110 was
added to the catalog at 0.12.0, is tagged A6, is `gate: true`, and **is already
exercised by a test** — but the manifest predated it, so the conformance report
filed our own passing test under *"CLAIMED BUT NOT IN THE CATALOG (fix the
test)"* and the gate went uncounted.

The test was right and the bookkeeping was wrong, which is the more dangerous
direction: a report that under-counts coverage invites someone to "fix" a correct
test. Hand-maintenance was the cause, so this replaces it.

The manifest deliberately carries **identifiers and metadata only** — never the
normative statement. The catalog is the single source of the obligation's text;
copying it here would create a second one that could disagree.

    uv run python scripts/sync_obligations.py [path/to/ai-assurance-catalog]
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

ARCHETYPE = "A6"
DEFAULT_CATALOG = Path.home() / "ai-assurance-catalog"
MANIFEST = Path(__file__).resolve().parents[1] / "evals" / "a6_obligations.json"

KEEP = ("id", "title", "dimension", "level", "gate", "stages", "mechanisms")


def entry(raw: dict) -> dict:
    """Metadata only. The obligation's statement stays in the catalog."""
    out = {k: raw[k] for k in KEEP if k in raw}
    out["title"] = str(out.get("title", "")).strip()
    return out


def _version(root: Path, files: list[Path]) -> str:
    """The highest `since:` any active obligation declares, not the declared
    package version.

    They disagree today — `package.json` says 0.11.4 while AAC-0110 says it
    arrived in 0.12.0 — because a version field is bumped by hand and content is
    not. Deriving it from the obligations means this manifest records which
    obligations it actually reflects, which is the only thing it is for.
    """
    declared = ""
    if (root / "package.json").exists():
        declared = str(json.loads((root / "package.json").read_text()).get("version", ""))

    seen = sorted(
        {str(yaml.safe_load(p.read_text()).get("since", "")) for p in files} - {""},
        key=lambda v: [int(part) for part in v.split(".") if part.isdigit()],
    )
    content = seen[-1] if seen else "unknown"
    if declared and declared != content:
        print(f"note: catalog package.json says {declared}, content goes to {content}")
    return content


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CATALOG
    files = sorted((root / "catalog").glob("AAC-*.yaml"))
    if not files:
        raise SystemExit(f"no catalog at {root} — pass its path as an argument")

    ours: list[dict] = []
    elsewhere: dict[str, dict] = {}
    for path in files:
        raw = yaml.safe_load(path.read_text())
        if raw.get("status") != "active":
            continue
        if ARCHETYPE in (raw.get("archetypes") or []):
            ours.append(entry(raw))
        else:
            elsewhere[raw["id"]] = {
                "archetypes": raw.get("archetypes", []),
                "title": str(raw.get("title", "")).strip(),
            }

    existing = json.loads(MANIFEST.read_text())
    before = {o["id"] for o in existing["obligations"]}
    after = {o["id"] for o in ours}

    version = _version(root, files)
    MANIFEST.write_text(
        json.dumps(
            {
                **{k: existing[k] for k in ("source", "archetype", "note") if k in existing},
                "catalog_version": version,
                "generated": datetime.now(UTC).date().isoformat(),
                "obligations": ours,
                "elsewhere_in_catalog": elsewhere,
                **{k: existing[k] for k in ("elsewhere_note",) if k in existing},
            },
            indent=2,
        )
        + "\n"
    )

    print(f"{ARCHETYPE}: {len(before)} -> {len(ours)} obligations, catalog {version}")
    for added in sorted(after - before):
        print(f"  + {added}")
    for gone in sorted(before - after):
        print(f"  - {gone}")


if __name__ == "__main__":
    main()

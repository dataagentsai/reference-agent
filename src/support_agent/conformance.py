"""Which obligations this suite exercises: the harness's report, read against this agent's manifest.

The report is the harness's (`agent_harness.conformance`). Which obligations it
reads — the A6 manifest in this repository's `evals/` — is this agent's, and is
named here, at import, so `Report()` reads it as it always has.
"""

from __future__ import annotations

from pathlib import Path

from agent_harness.conformance import (
    Coverage,
    Elsewhere,
    Obligation,
    Report,
    Verdict,
    load,
    load_elsewhere,
    use_manifest,
)

MANIFEST = Path(__file__).resolve().parents[2] / "evals" / "a6_obligations.json"

use_manifest(MANIFEST)

__all__ = [
    "MANIFEST",
    "Coverage",
    "Elsewhere",
    "Obligation",
    "Report",
    "Verdict",
    "load",
    "load_elsewhere",
]

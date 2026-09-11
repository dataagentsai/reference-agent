"""Every statement a test may claim to discharge, from every spec that holds one.

A test names what it verifies with `@pytest.mark.discharges(...)`. The ids come
from five places, told apart by their shape:

| Shape | Spec | Source |
|---|---|---|
| `AAC-0047` | assurance catalog — what must be TRUE | `evals/a6_obligations.json` |
| `AHC-0074` | harness catalog — what must EXIST | the sibling `ai-harness-catalog` checkout |
| `B5` | the Baseline profile | the sibling `clean-ai-engineering/BASELINE.md` |
| `P-CANCEL`, `R-STYLE`, `Q-COST` | this agent's AOAS — policies, refusals, properties | its spec |
| `op:…`, `esc:…`, `ext:…` | AOAS operations, escalation rules, external contracts | its spec |

**An unknown id fails collection**, before a single test runs. A tag that names
nothing is worse than no tag: it reads as coverage and verifies nothing.

Which statements this agent *owes* is also answered here, because the map is
only useful against the right denominator: AAC and AHC scoped to the archetypes
the AOAS claims, minus what it declares excluded; every AOAS statement; every
Baseline item.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from agenttwin.loader import resolve_spec
from agenttwin.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
AAC_MANIFEST = ROOT / "evals" / "a6_obligations.json"
AHC_DIR = ROOT.parent / "ai-harness-catalog" / "capabilities"
BASELINE = ROOT.parent / "clean-ai-engineering" / "BASELINE.md"
WORLD = ROOT / "worlds" / "clothing.yaml"

FAMILIES = ("AOAS", "AAC", "AHC", "Baseline")


def family(statement_id: str) -> str:
    if statement_id.startswith("AAC-"):
        return "AAC"
    if statement_id.startswith("AHC-"):
        return "AHC"
    if re.fullmatch(r"B\d+", statement_id):
        return "Baseline"
    return "AOAS"


@dataclass(frozen=True)
class Statement:
    id: str
    family: str
    title: str
    owed: bool
    """Whether this agent is answerable for it. Owed and unexercised is a gap;
    known and not owed is a tag worth keeping and nothing more."""


@dataclass
class Vocabulary:
    statements: dict[str, Statement] = field(default_factory=dict)
    unchecked: list[str] = field(default_factory=list)
    """Families whose source was not found, so their ids cannot be validated.
    Said aloud rather than passed silently."""

    def unknown(self, ids: tuple[str, ...]) -> list[str]:
        return [i for i in ids if i not in self.statements and family(i) not in self.unchecked]

    def owed(self, fam: str) -> list[Statement]:
        return [s for s in self.statements.values() if s.family == fam and s.owed]


def _aoas_statements(doc: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, p in doc.get("policies", {}).items():
        if key.startswith("P-"):
            out.append((key, p["rule"]))
    for r in doc["purpose"].get("refuses", []):
        out.append((r["id"], f"refuses {r['what']}"))
    for q in doc.get("required", {}).get("properties", []):
        out.append((q["id"], q["must"]))
    for name, op in doc.get("operations", {}).items():
        out.append((f"op:{name}", f"{op['side_effect']} operation on {op['entity']}"))
    esc = doc.get("policies", {}).get("escalation", {})
    for rule in (*esc.get("on_request", []), *esc.get("on_condition", [])):
        when = rule["when"] if isinstance(rule["when"], str) else f"{rule['when']['field']}"
        out.append((f"esc:{rule['id']}", f"escalate when {when}"))
    for name in doc.get("external", {}):
        out.append((f"ext:{name}", f"external contract with {name}"))
    return out


def load(world: Path = WORLD) -> Vocabulary:
    vocab = Vocabulary()

    doc = load_spec(resolve_spec(world))
    claim = doc.get("conformance", {})
    shapes = set(claim.get("archetypes", []))
    excluded = {x["id"] for c in ("aac", "ahc") for x in claim.get(c, {}).get("excluded", [])}

    for sid, title in _aoas_statements(doc):
        vocab.statements[sid] = Statement(sid, "AOAS", title, owed=True)

    aac = json.loads(AAC_MANIFEST.read_text())
    for o in aac["obligations"]:  # tagged A6 — the agent's shape
        vocab.statements[o["id"]] = Statement(
            o["id"], "AAC", o["title"], owed=o["id"] not in excluded
        )
    for oid, meta in aac.get("elsewhere_in_catalog", {}).items():
        owed = bool(shapes & set(meta.get("archetypes", []))) and oid not in excluded
        vocab.statements[oid] = Statement(oid, "AAC", meta.get("title", ""), owed=owed)

    if AHC_DIR.is_dir():
        for f in sorted(AHC_DIR.glob("AHC-*.yaml")):
            c = yaml.safe_load(f.read_text())
            owed = bool(shapes & set(c.get("archetypes", []))) and c["id"] not in excluded
            vocab.statements[c["id"]] = Statement(c["id"], "AHC", c["title"], owed=owed)
    else:
        vocab.unchecked.append("AHC")

    if BASELINE.is_file():
        for num, item in re.findall(r"^\| B(\d+) \| ([^|]+) \|", BASELINE.read_text(), re.M):
            vocab.statements[f"B{num}"] = Statement(f"B{num}", "Baseline", item.strip(), owed=True)
    else:
        vocab.unchecked.append("Baseline")

    return vocab


__all__ = ["FAMILIES", "Statement", "Vocabulary", "family", "load"]

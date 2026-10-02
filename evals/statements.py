"""Every statement a test may claim to discharge, from every spec that holds one.

A test names what it verifies with `@pytest.mark.discharges(...)`. The ids come
from five places, told apart by their shape:

| Shape | Spec | Source |
|---|---|---|
| `AAC-0047` | assurance catalog — what must be TRUE | `evals/a6_obligations.json` |
| `AHC-0074` | harness catalog — what must EXIST | the sibling `ai-harness-catalog` checkout |
| `B5` | the Baseline profile | the sibling `clean-ai-engineering/BASELINE.md` |
| `P-CANCEL`, `R-STYLE`, `Q-COST` | this agent's AOAS — policies, refusals | its spec |
| `op:…`, `esc:…`, `fact:…`, `ext:…` | its operations, rules, facts, contracts | its spec |

A policy id may be a key of `policies` or one of the id'd lines under its
`approval` and `escalation` blocks. Those lines were prose with no ids until
2026-09-12, which is why the largest cluster of untagged tests in the G0.1 audit
was the escalation and approval rules: every one was verified, and none of them
could be named.

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


NOT_EVIDENCE = ("documents_gap", "tooling", "unwired")
"""Markers that make a test evidence of none of the ids it names."""


def aac_claim(ids: tuple[str, ...], markers: set[str]) -> str:
    """The AAC ids among a test's claims, as AAC's junit adapter reads them.

    That adapter reads a junit property named `aac` (adapters/junit.js), not
    `discharges`, and only AAC ids in it. A test that documents a gap, tests an
    instrument, or tests a component the agent never calls is evidence of none
    of them; the assurance map leaves those out, and so does this."""
    if markers & set(NOT_EVIDENCE):
        return ""
    return " ".join(i for i in ids if family(i) == "AAC")


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


# The concern of a derived id. The four families below are not authored
# statements — they are read off the operations, escalation rules, facts and
# external contracts a spec declares — so their concern is derived the way
# `phase` is, from what kind of thing they are, rather than tagged on each.
#
# The reasoning, since each is a judgement:
#
# * an **operation** is filed by what it can do to the world. An irreversible
#   one is a safety statement — it is where the operational constraint lives —
#   and a read or a reversible write is functional suitability.
# * an **escalation rule** is oversight, which the assurance catalog's own
#   crosswalk places under safety: fetching a person is an interlock.
# * a **fact** exists so a rule can be reproduced from a trace. Nothing the
#   customer experiences depends on it directly; what depends on it is anybody's
#   ability to explain a decision afterwards.
# * an **external contract** is the one place this family reaches
#   `compatibility` honestly: it is a statement about working with a system
#   somebody else owns and versions.
DERIVED_CONCERN = {
    "esc": ("safety", "operational constraint"),
    "fact": ("maintainability", "analysability"),
    "ext": ("compatibility", "interoperability"),
}


def _operation_concern(side_effect: str) -> tuple[str, str]:
    if side_effect == "irreversible":
        return ("safety", "operational constraint")
    return ("functional-suitability", "functional correctness")


def _aoas_statements(doc: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    policies = doc.get("policies", {})
    for key, p in policies.items():
        if key.startswith("P-"):
            out.append((key, p["rule"]))
    # The approval and escalation blocks state their rules as id'd lines. They
    # were prose with no ids, so no test could name them and the map read them
    # as unverified — the largest untagged cluster the G0.1 audit found.
    for block in ("approval", "escalation"):
        for statement in policies.get(block, {}).get("statements", []):
            out.append((statement["id"], statement["rule"]))
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
    # A fact is a statement too: this spec requires the harness to maintain it,
    # and `derived` says what computes it.
    for name, f in doc.get("facts", {}).items():
        out.append((f"fact:{name}", f.get("derived", name)))
    for name in doc.get("external", {}):
        out.append((f"ext:{name}", f"external contract with {name}"))
    return out


def aoas_document(world: Path = WORLD) -> dict:
    """This agent's AOAS, resolved through the world that cites it.

    The world names the spec and its version; the spec may extend another by
    merge patch. Going through the world rather than reading a path means a
    generated view and a running scenario are looking at the same document.
    """
    return load_spec(resolve_spec(world))


def load(world: Path = WORLD) -> Vocabulary:
    vocab = Vocabulary()

    doc = aoas_document(world)
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


__all__ = [
    "FAMILIES",
    "NOT_EVIDENCE",
    "Statement",
    "Vocabulary",
    "aac_claim",
    "family",
    "load",
]

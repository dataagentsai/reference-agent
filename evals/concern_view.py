"""Everything this agent's specs say about one quality, from all of them at once.

The Assurance Map answers *is there a test*. Scenario coverage answers *has a
conversation exercised it*. This answers the question an architect asks and
nothing here could answer before: **show me everything about privacy. Or cost.
Or reliability.**

That question has no home in the family's structure, because the structure is
organised the other way — by what kind of statement a thing is, and which
artifact owns it. A spec family with six artifacts has the privacy statements in
all six, and reading them together meant reading all six.

## Why it is a join and not a document

Nobody writes this page. It is produced by joining four sources on one string:

| Source | Concern comes from |
|---|---|
| this agent's **AOAS** | authored on statements; derived for operations, rules, facts |
| the **harness catalog** | authored on every capability |
| the **assurance catalog** | derived from its `dimension`, through its own crosswalk |
| the **scenarios** | declared where a scenario exists for one quality in particular |

The vocabulary is the nine quality characteristics of ISO/IEC 25010:2023 plus
`cost`, cited rather than invented, and the four sources agree on the spelling
because each one's linter checks it.

## What it is for, beyond reading

**An NFR is a statement whose concern is not functional suitability.** That is
the whole definition, and it is why this family has no separate non-functional
requirements document: this page, filtered to the other nine, *is* that document
— and unlike one somebody maintains, it cannot fall behind the specs, because it
is made of them.

The emptier rows are the more useful ones. A concern with obligations and no
capabilities is something this project knows it should check and has built
nothing to do; a concern with capabilities and no scenarios is something built
and never demonstrated. Both are visible at a glance here and invisible
everywhere else.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml
from agenttwin import load_scenario

from evals import statements

ROOT = Path(__file__).resolve().parents[1]
AHC_DIR = ROOT.parent / "ai-harness-catalog" / "capabilities"
AAC_CROSSWALK = ROOT.parent / "ai-assurance-catalog" / "taxonomy" / "concerns.yaml"
AAC_MANIFEST = ROOT / "evals" / "a6_obligations.json"
SCENARIOS = ROOT / "scenarios"

ORDER = (
    "functional-suitability",
    "safety",
    "security",
    "reliability",
    "cost",
    "maintainability",
    "performance-efficiency",
    "compatibility",
    "interaction-capability",
    "flexibility",
)
"""Reading order, not importance order — though they correlate here. Functional
suitability first because it is the bulk and the reader needs the shape of the
pile before the interesting sparse rows mean anything."""


@dataclass(frozen=True)
class Item:
    id: str
    source: str
    title: str
    facet: str = ""


def _aoas_concerns() -> dict[str, str]:
    """Statement id → concern, for everything the AOAS holds."""
    doc = statements.aoas_document()
    policies = doc.get("policies", {})
    out: dict[str, str] = {}
    for key, value in policies.items():
        if key.startswith("P-"):
            out[key] = value["concern"]
    for block in ("approval", "escalation"):
        for st in policies.get(block, {}).get("statements", []):
            out[st["id"]] = st["concern"]
    for r in doc["purpose"].get("refuses", []):
        out[r["id"]] = r["concern"]
    for q in doc.get("required", {}).get("properties", []):
        out[q["id"]] = q["concern"]
    for name, op in doc.get("operations", {}).items():
        out[f"op:{name}"] = statements._operation_concern(op["side_effect"])[0]
    esc = policies.get("escalation", {})
    for rule in (*esc.get("on_request", []), *esc.get("on_condition", [])):
        out[f"esc:{rule['id']}"] = statements.DERIVED_CONCERN["esc"][0]
    for name in doc.get("facts", {}):
        out[f"fact:{name}"] = statements.DERIVED_CONCERN["fact"][0]
    for name in doc.get("external", {}):
        out[f"ext:{name}"] = statements.DERIVED_CONCERN["ext"][0]
    return out


def gather() -> dict[str, list[Item]]:
    """Concern → everything about it, from all four sources."""
    by: dict[str, list[Item]] = defaultdict(list)

    doc = statements.aoas_document()
    titles = dict(statements._aoas_statements(doc))
    for sid, concern in _aoas_concerns().items():
        by[concern].append(Item(sid, "AOAS", titles.get(sid, sid)))

    owed = {s.id for s in statements.load().owed("AHC")}
    for path in sorted(AHC_DIR.glob("AHC-*.yaml")):
        cap = yaml.safe_load(path.read_text())
        if cap["id"] in owed:
            by[cap["concern"]].append(
                Item(cap["id"], "AHC", cap["title"].strip(), cap.get("facet", ""))
            )

    crosswalk = {
        row["dimension"]: row["concern"]
        for row in yaml.safe_load(AAC_CROSSWALK.read_text())["crosswalk"]
    }
    owed_aac = {s.id for s in statements.load().owed("AAC")}
    for ob in json.loads(AAC_MANIFEST.read_text())["obligations"]:
        if ob["id"] in owed_aac:
            by[crosswalk[ob["dimension"]]].append(Item(ob["id"], "AAC", ob["title"]))

    for path in sorted(SCENARIOS.glob("*.yaml")):
        scenario = load_scenario(path)
        if scenario.concern:
            by[scenario.concern].append(Item(path.stem, "scenario", scenario.scenario))
    return by


def render(by: dict[str, list[Item]]) -> str:
    lines = [
        "# Concern View",
        "",
        "*Generated by `scripts/concern_view.py`. Never edit by hand — regenerate.*",
        "",
        "Everything this agent's specifications say about one quality, from all of",
        "them at once. The vocabulary is the nine quality characteristics of",
        "**ISO/IEC 25010:2023** plus `cost`, cited rather than invented, and shared",
        "across the family (Spec Charter, §3).",
        "",
        "**An NFR is a statement whose concern is not functional suitability.** This",
        "page, read past the first section, is this agent's non-functional",
        "requirements — and unlike a document somebody maintains, it cannot fall",
        "behind the specs, because it is made of them.",
        "",
        "| Concern | AOAS | AHC | AAC | Scenarios |",
        "|---|---|---|---|---|",
    ]
    for concern in ORDER:
        items = by.get(concern, [])
        counts = {s: sum(1 for i in items if i.source == s) for s in ("AOAS", "AHC", "AAC")}
        scenarios = sum(1 for i in items if i.source == "scenario")
        row = f"{counts['AOAS']} | {counts['AHC']} | {counts['AAC']} | {scenarios}"
        lines.append(f"| **{concern}** | {row} |")

    for concern in ORDER:
        items = by.get(concern, [])
        lines += ["", f"## {concern} — {len(items)}", ""]
        if not items:
            lines += [
                "Nothing. Neither specification requires anything here and no scenario",
                "faces it, which is a claim about this agent and not an omission in the",
                "vocabulary — the characteristic stays listed so the silence is visible.",
            ]
            continue
        for source in ("AOAS", "AHC", "AAC", "scenario"):
            group = [i for i in items if i.source == source]
            if not group:
                continue
            lines += [f"**{source}**", ""]
            lines += [
                f"- `{i.id}` {i.title}" + (f" *({i.facet})*" if i.facet else "") for i in group
            ]
            lines += [""]
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["Item", "gather", "render"]

"""The pattern catalog, the online rules and the alert rules name the same things.

AAC's `patterns/` is the catalog of what can go wrong and how it shows
(AACP-xxxx, each with a `signal`). A turn- or conversation-level signal is
detected in `watch/rules.py`; a window-level one in Prometheus's rules. This is
the registry check a cost analyser runs over its detectors: every pattern with a
signal is detected somewhere or listed in `NOT_YET` with the reason, and every
detector names a pattern that exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from support_agent.watch import rules as watch

pytestmark = pytest.mark.tooling

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT.parent / "ai-assurance-catalog" / "patterns"
PROMETHEUS = ROOT / "deploy" / "prometheus" / "rules" / "agent.yml"

needs_catalog = pytest.mark.skipif(
    not CATALOG.is_dir(), reason="the assurance catalog is a sibling checkout"
)

OUTCOMES = {"AACP-0041": "outcomes.returned", "AACP-0042": "outcomes.asked_for_person"}
"""Conversation-level patterns detected as outcomes rather than as rules."""


def patterns() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for path in sorted(CATALOG.glob("*.yaml")):
        for pattern in yaml.safe_load(path.read_text())["patterns"]:
            found[pattern["id"]] = pattern
    return found


def alerting() -> dict[str, list[str]]:
    """pattern id → the Prometheus rules that carry it."""
    by: dict[str, list[str]] = {}
    for group in yaml.safe_load(PROMETHEUS.read_text())["groups"]:
        for rule in group["rules"]:
            name = rule.get("alert") or rule.get("record")
            by.setdefault(rule["labels"]["pattern"], []).append(name)
    return by


def detected() -> set[str]:
    return (
        {r.pattern for r in watch.RULES}
        | {r.pattern for r in watch.CONVERSATION_RULES}
        | set(OUTCOMES)
        | {p for p in alerting() if p != "watch"}
    )


@needs_catalog
def test_every_pattern_with_a_signal_is_detected_or_says_why_not() -> None:
    signalled = {pid for pid, p in patterns().items() if "signal" in p}
    missing = sorted(signalled - detected() - set(watch.NOT_YET))
    assert missing == [], f"patterns with a signal and no detector, and no reason: {missing}"


@needs_catalog
def test_a_pattern_is_not_both_detected_and_owed() -> None:
    assert sorted(detected() & set(watch.NOT_YET)) == []


@needs_catalog
def test_every_detector_names_a_pattern_that_exists() -> None:
    known = set(patterns())
    cited = {r.pattern for r in watch.RULES} | {r.pattern for r in watch.CONVERSATION_RULES}
    cited |= {p for p in alerting() if p != "watch"} | set(watch.NOT_YET)
    assert sorted(cited - known) == []


# (why, the rules, the signal levels they may implement)
LEVELS = [
    ("turn rules read one turn", watch.RULES, {"turn", "window"}),
    ("conversation rules read one conversation", watch.CONVERSATION_RULES, {"conversation"}),
]


@needs_catalog
@pytest.mark.parametrize(("why", "rules", "levels"), LEVELS, ids=[r[0] for r in LEVELS])
def test_a_rule_implements_a_signal_of_its_own_level(why: str, rules: tuple, levels: set) -> None:
    """A turn rule may implement a window pattern one turn at a time — a slow
    turn is a point on the latency tail — but never a conversation's."""
    known = patterns()
    wrong = [r.id for r in rules if known[r.pattern]["signal"]["level"] not in levels]
    assert wrong == []


def test_every_alert_says_how_loudly_and_which_pattern() -> None:
    for group in yaml.safe_load(PROMETHEUS.read_text())["groups"]:
        for rule in group["rules"]:
            labels = rule["labels"]
            assert labels["pattern"] == "watch" or labels["pattern"].startswith("AACP-"), rule
            if "alert" in rule:
                assert labels["severity"] in {"page", "ticket"}, rule["alert"]

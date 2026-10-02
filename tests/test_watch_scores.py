"""The watch's scores, read as coverage evidence by AAC's Langfuse adapter.

A score the watch writes is evidence for an AAC coverage report only if it says
which obligations it evidences (`metadata.aac`), how the check decided
(`aac.mechanism`), and — where the value is not itself a verdict — what the
verdict is (`aac.outcome`). And it is evidence of coverage only if the passes
are written as well as the findings: an export of findings alone reads as
nothing but failures (AAC docs/ADAPTERS.md, "What a score never says").

The last test hands what the writer wrote to the adapter itself, through node,
when the catalog is checked out beside this repository.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from support_agent.watch import Watch
from support_agent.watch import rules as watch
from support_agent.watch.langfuse import Langfuse
from support_agent.watch.outcomes import Outcome
from support_agent.watch.record import ToolUse, Turn

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT.parent / "ai-assurance-catalog"
FIXTURE = ROOT / "tests" / "fixtures" / "langfuse-scores.v3.json"

needs_catalog = pytest.mark.skipif(
    not (CATALOG / "patterns").is_dir(), reason="the assurance catalog is a sibling checkout"
)

ALL_RULES: list[watch.Rule | watch.ConversationRule] = [
    *watch.RULES,
    *watch.CONVERSATION_RULES,
]


def turn(**changes: Any) -> Turn:
    base = Turn(
        trace_id="t1",
        run_id="run_1",
        session_id="cnv_1",
        user_id="C-1042",
        started=1_000_000.0,
        duration_s=2.0,
        synthetic=False,
        captured=True,
        result="completed",
        rule_id="",
        reply_redacted=False,
        input="where is AB-10002?",
        reply="AB-10002 is pending and will ship soon.",
        route="agentic",
        termination="goal_reached",
        cost_usd=0.002,
        model_calls=2,
        malformed=0,
        unbacked_promise=False,
        tools=(
            ToolUse(
                "get_order",
                "read",
                "ok",
                {"id": "AB-10002"},
                {"id": "AB-10002", "status": "pending"},
                0.1,
            ),
        ),
    )
    return replace(base, **changes)


class Recorded(Langfuse):
    """The real writer, with the HTTP call replaced by a list."""

    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        super().__init__("http://langfuse.invalid", "pk", "sk")
        self.posted: list[dict[str, Any]] = []
        self.asked: list[str] = []
        self._pages = list(pages or [])

    def _call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        if method == "POST":
            self.posted.append(body)
            return {}
        self.asked.append(path)
        return self._pages.pop(0)


# --------------------------------------------------------------------------- #
# What each rule evidences.
# --------------------------------------------------------------------------- #


def caught_by() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted((CATALOG / "patterns").glob("*.yaml")):
        for pattern in yaml.safe_load(path.read_text())["patterns"]:
            found[pattern["id"]] = set(pattern.get("caught_by") or [])
    return found


def owed_by_a6() -> set[str]:
    cases = (yaml.safe_load(p.read_text()) for p in (CATALOG / "catalog").glob("AAC-*.yaml"))
    return {c["id"] for c in cases if c["status"] == "active" and "A6" in c["archetypes"]}


@needs_catalog
@pytest.mark.discharges("AAC-0014")
@pytest.mark.parametrize("rule", ALL_RULES, ids=[r.id for r in ALL_RULES])
def test_each_rule_evidences_only_what_its_pattern_catches_and_this_shape_owes(
    rule: watch.Rule | watch.ConversationRule,
) -> None:
    """Derived from the pattern's `caught_by`, cut to what A6 owes. Anything
    beyond the pattern is listed in `BEYOND_THE_PATTERN` with its reason."""
    evidence = watch.EVIDENCE[rule.id]
    beyond = {aac for aac in evidence.aac if (rule.id, aac) in watch.BEYOND_THE_PATTERN}
    assert set(evidence.aac) - beyond <= caught_by()[rule.pattern]
    assert set(evidence.aac) <= owed_by_a6()
    if isinstance(rule, watch.Rule):
        # Over the words, a deterministic assertion; over the span record, a trace one.
        assert evidence.mechanism == ("M1" if rule.needs_words else "M5")
    assert evidence.mechanism != "M3", "a model-graded rule owes a judge's obligations"


def test_every_rule_says_what_it_evidences() -> None:
    assert set(watch.EVIDENCE) == {r.id for r in ALL_RULES}


# --------------------------------------------------------------------------- #
# What each score says.
# --------------------------------------------------------------------------- #

# (name, a turn, the rule, the value written, the aac metadata expected)
SCORES = [
    (
        "a page rule that held: BOOLEAN true is its own verdict",
        turn(),
        "W-01",
        1,
        {"aac": ["AAC-0029"], "aac.mechanism": "M1"},
    ),
    (
        "a page rule that fired: BOOLEAN false is its own verdict",
        turn(reply="AB-10002 has shipped and is on its way."),
        "W-01",
        0,
        {"aac": ["AAC-0029"], "aac.mechanism": "M1"},
    ),
    (
        "a trend rule that fired: one turn is not the rate's verdict",
        turn(duration_s=45.0),
        "W-07",
        0,
        {"aac": ["AAC-0007"], "aac.mechanism": "M5", "aac.outcome": "unknown"},
    ),
    (
        "a trend rule that held says unknown too",
        turn(),
        "W-07",
        1,
        {"aac": ["AAC-0007"], "aac.mechanism": "M5", "aac.outcome": "unknown"},
    ),
    (
        "a rule that evidences nothing declares nothing",
        turn(termination="step_budget_exhausted"),
        "W-04",
        0,
        {},
    ),
]


@pytest.mark.discharges("AAC-0014", "AHC-0028")
@pytest.mark.parametrize(
    ("name", "given", "rule", "value", "declared"), SCORES, ids=[s[0] for s in SCORES]
)
def test_a_rule_score_says_what_it_evidences(
    name: str, given: Turn, rule: str, value: int, declared: dict[str, Any]
) -> None:
    sink = Recorded()
    write(sink, [given])

    body = next(b for b in sink.posted if b["name"] == f"watch.{rule}")
    assert (body["dataType"], body["value"]) == ("BOOLEAN", value)
    aac = {k: v for k, v in body["metadata"].items() if k == "aac" or k.startswith("aac.")}
    assert aac == declared
    assert body["metadata"]["version"], "a score names the rule version (AHC-0028)"


# (name, what the turn's words were, rules that write a score on it)
COVERAGE = [
    ("captured: every rule writes, pass or fail", True, len(watch.RULES)),
    (
        "not captured: a rule that needs the words writes nothing at all",
        False,
        sum(1 for r in watch.RULES if not r.needs_words),
    ),
]


@pytest.mark.discharges("AAC-0014", "AHC-0114")
@pytest.mark.parametrize(("name", "captured", "written"), COVERAGE, ids=[c[0] for c in COVERAGE])
def test_every_rule_that_ran_writes_a_verdict_and_one_that_did_not_writes_none(
    name: str, captured: bool, written: int
) -> None:
    sink = Recorded()
    given = turn(captured=captured, reply=None if not captured else turn().reply)
    write(sink, [given])

    rule_scores = [b for b in sink.posted if b["name"].startswith("watch.W-")]
    assert len(rule_scores) == written
    assert all(b["metadata"].get("aac.outcome") != "skipped" for b in sink.posted)


# (name, how it is written, the score name, the aac metadata expected)
OTHER_SCORES = [
    (
        "the per-trace count: the trace was scored, whatever it found",
        lambda lf: lf.evaluated("t1", 2, "v"),
        "watch.findings",
        {"aac": ["AAC-0014"], "aac.mechanism": "M5", "aac.outcome": "pass"},
    ),
    (
        "an outcome on the run that produced it: the join is the verdict",
        lambda lf: lf.outcome(Outcome("feedback_down", "stated", "t1", "cnv_1", "C-1042", "down")),
        "outcome",
        {"aac": ["AAC-0115"], "aac.mechanism": "M5", "aac.outcome": "pass"},
    ),
]


@pytest.mark.discharges("AAC-0014", "AAC-0115")
@pytest.mark.parametrize(
    ("name", "write", "score", "declared"), OTHER_SCORES, ids=[s[0] for s in OTHER_SCORES]
)
def test_a_numeric_or_categorical_score_states_its_verdict(
    name: str, write: Any, score: str, declared: dict[str, Any]
) -> None:
    sink = Recorded()
    write(sink)
    (body,) = sink.posted
    assert body["name"] == score
    assert {k: v for k, v in body["metadata"].items() if k.startswith("aac")} == declared


# --------------------------------------------------------------------------- #
# The export.
# --------------------------------------------------------------------------- #


def page(n: int, cursor: str | None) -> dict[str, Any]:
    return {"data": [{"id": f"s{n}-{i}"} for i in range(2)], "meta": {"cursor": cursor}}


# (name, what the API answers, pages saved, scores saved)
EXPORTS = [
    ("one page", [page(1, None)], 1, 2),
    ("three pages by cursor", [page(1, "c1"), page(2, "c2"), page(3, None)], 3, 6),
    (
        "an empty page ends it even with a cursor",
        [page(1, "c1"), {"data": [], "meta": {"cursor": "c2"}}],
        2,
        2,
    ),
]


@pytest.mark.tooling
@pytest.mark.parametrize(
    ("name", "answers", "pages", "scores"), EXPORTS, ids=[e[0] for e in EXPORTS]
)
def test_the_export_pages_v3_with_details_and_keeps_every_page_whole(
    name: str, answers: list[dict[str, Any]], pages: int, scores: int, tmp_path: Path
) -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from scripts.export_scores import export

    langfuse = Recorded(answers)
    out = tmp_path / "scores.json"
    assert export(langfuse, out, {}) == scores

    saved = json.loads(out.read_text())
    assert len(saved) == pages and saved == answers[:pages]
    assert all(a.startswith("/api/public/v3/scores?") for a in langfuse.asked)
    assert all("fields=details%2Csubject" in a for a in langfuse.asked)
    assert [("cursor=" in a) for a in langfuse.asked] == [False] + [True] * (pages - 1)


# --------------------------------------------------------------------------- #
# Read by AAC's own adapter.
# --------------------------------------------------------------------------- #


def as_v3(posted: list[dict[str, Any]]) -> dict[str, Any]:
    """What `GET /api/public/v3/scores?fields=details,subject` answers for the
    bodies the writer posted: value typed by dataType, the trace under subject."""
    rows = [
        {
            "id": b["id"],
            "name": b["name"],
            "source": "API",
            "dataType": b["dataType"],
            "value": bool(b["value"]) if b["dataType"] == "BOOLEAN" else b["value"],
            "comment": b["comment"],
            "metadata": b["metadata"],
            "subject": {"kind": "trace", "id": b["traceId"]},
            "timestamp": "2026-10-01T12:00:00.000Z",
        }
        for b in posted
    ]
    return {"data": rows, "meta": {"cursor": None}}


def fixture_turns() -> list[Turn]:
    """Four turns, each wrong in at most one way the report should show."""
    return [
        turn(trace_id="tr-held", session_id="cnv_a"),
        turn(trace_id="tr-contradicts", session_id="cnv_b", reply="AB-10002 has shipped."),
        turn(trace_id="tr-slow", session_id="cnv_c", duration_s=45.0),
        turn(trace_id="tr-unseen", session_id="cnv_d", captured=False, reply=None),
    ]


def fixture_page() -> dict[str, Any]:
    sink = Recorded()
    write(sink, fixture_turns())
    sink.outcome(Outcome("feedback_up", "stated", "tr-held", "cnv_a", "C-1042", "up"))
    built = as_v3(sink.posted)
    return {
        "_comment": (
            "Fixture for the AAC report while Langfuse is not running: what GET"
            " /api/public/v3/scores?fields=details,subject would answer for the scores"
            " the watch's own writer posts over four turns (held; a status contradicting"
            " the store; slow; words not captured) and one feedback outcome. The one"
            " failing score (watch.W-01 on tr-contradicts) is planted, to show a finding"
            " reaching the report."
            " Rebuilt by tests/test_watch_scores.py with AAC_FIXTURE=write; not an"
            " export from a live project."
        ),
        **built,
    }


@pytest.mark.discharges("AAC-0014")
def test_the_saved_fixture_is_what_the_writer_writes() -> None:
    import os

    if os.environ.get("AAC_FIXTURE") == "write":
        FIXTURE.write_text(json.dumps(fixture_page(), indent=2) + "\n")
    assert json.loads(FIXTURE.read_text()) == fixture_page()


ADAPTED = [
    ("a contradiction fails the claim's obligation", "AAC-0029", "fail"),
    ("an action claim that held passes", "AAC-0110", "pass"),
    ("a slow turn is covered with no verdict", "AAC-0007", "unknown"),
    ("the count says scoring ran", "AAC-0014", "pass"),
    ("feedback joined to its run", "AAC-0115", "pass"),
]


@needs_catalog
@pytest.mark.skipif(shutil.which("node") is None, reason="node runs AAC's adapter")
@pytest.mark.discharges("AAC-0014", "AAC-0115")
@pytest.mark.parametrize(("name", "case", "outcome"), ADAPTED, ids=[a[0] for a in ADAPTED])
def test_aacs_langfuse_adapter_reads_the_watch_as_coverage(
    name: str, case: str, outcome: str
) -> None:
    script = (
        "const a=require(process.argv[1]);"
        "const r=a.extract(require('fs').readFileSync(process.argv[2],'utf8'),{});"
        "console.log(JSON.stringify({r,w:a.extract.warnings}))"
    )
    ran = subprocess.run(  # noqa: S603 — fixed program, paths from this repository
        ["node", "-e", script, str(CATALOG / "adapters" / "langfuse.js"), str(FIXTURE)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    read = json.loads(ran.stdout)
    assert read["w"] == []
    got = {(r["case"], r["outcome"]) for r in read["r"] if r.get("ran", True)}
    assert (case, outcome) in got, sorted(got)


def write(sink: Recorded, turns: list[Turn]) -> None:
    """What one pass of the watch writes for `turns`, without reading spans."""
    verdicts, evaluated = watch.judge(turns)
    Watch(source=None, sink=sink)._write(evaluated, verdicts, [])  # type: ignore[arg-type]

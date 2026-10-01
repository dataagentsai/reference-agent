"""The conformance plugin.

A test declares what it discharges with a marker; the run collects outcomes and
prints a report at the end naming what was covered, what failed, and what was
never exercised.

    @pytest.mark.discharges("AAC-0055", "AHC-0041", "Q-STEPS")
    async def test_the_step_budget_terminates(): ...
    @pytest.mark.scored          # model-driven: pass-rate, not pass/fail
    @pytest.mark.tooling         # tests an instrument, not the agent
    @pytest.mark.documents_gap("why")   # asserts a statement is missing

`discharges` takes an id from any spec in the family — see `evals/statements.py`
for the shapes. An id that names nothing fails collection. `pytest
--assurance-map` also writes the generated map to `evals/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from evals import assurance_map as amap
from evals import statements

from support_agent.conformance import Report

_report: Report | None = None
_vocab: statements.Vocabulary | None = None
_outcomes: list[amap.Outcome] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--assurance-map",
        action="store_true",
        help="write the generated Assurance Map to evals/",
    )


def pytest_configure(config: pytest.Config) -> None:
    global _report, _vocab
    config.addinivalue_line(
        "markers",
        "discharges(*ids): statement ids this test verifies — AAC-, AHC-, B#, or the AOAS's own",
    )
    config.addinivalue_line(
        "markers", "scored: a model-driven case — reported as a pass rate, never as pass/fail"
    )
    config.addinivalue_line(
        "markers",
        "tooling: tests an instrument, not the agent — kept out of the untagged remainder",
    )
    config.addinivalue_line(
        "markers",
        "unwired: tests a component the agent never calls — its ids are not counted",
    )
    config.addinivalue_line(
        "markers",
        "documents_gap(why): asserts a statement is missing — listed with its reason, "
        "never counted as an untagged oversight",
    )
    _report = Report()
    _vocab = statements.load()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Fail before running anything if a test claims a statement that does not exist.

    Also puts each test's claims on its JUnit record, as properties — the
    convention the gates read (`clean-ai-engineering/tools/gates`), so the
    features gate can judge any implementation's suite without importing it:
    `discharges` (comma-separated ids), and `tooling` or `unwired` when set.
    """
    for item in items:
        marker = item.get_closest_marker("discharges")
        if marker is not None:
            item.user_properties.append(("discharges", ",".join(marker.args)))
        for flag in ("tooling", "unwired"):
            if item.get_closest_marker(flag) is not None:
                item.user_properties.append((flag, "true"))
    if _vocab is None:
        return
    bad = {}
    for item in items:
        marker = item.get_closest_marker("discharges")
        if marker is not None and (unknown := _vocab.unknown(marker.args)):
            bad[item.nodeid.split("[", 1)[0]] = unknown
    if bad:
        detail = "\n".join(f"  {test}: {', '.join(ids)}" for test, ids in sorted(bad.items()))
        raise pytest.UsageError(f"discharges names statements that do not exist:\n{detail}")


def _gap_of(item: pytest.Item) -> str:
    marker = item.get_closest_marker("documents_gap")
    if marker is None:
        return ""
    return str(marker.args[0]) if marker.args else "unstated"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    result = outcome.get_result()
    if result.when != "call" or _report is None:
        return
    marker = item.get_closest_marker("discharges")
    ids = tuple(marker.args) if marker is not None else ()
    _outcomes.append(
        amap.Outcome(
            nodeid=item.nodeid,
            ids=ids,
            passed=result.passed,
            tooling=item.get_closest_marker("tooling") is not None,
            unwired=item.get_closest_marker("unwired") is not None,
            gap=_gap_of(item),
            skipped=result.skipped,
        )
    )
    if result.skipped:
        # A test that did not run is not evidence either way: the obligation stays
        # "not exercised" rather than turning into a failure nobody can find.
        return
    scored = item.get_closest_marker("scored") is not None
    for obligation_id in ids:
        if statements.family(obligation_id) == "AAC":
            _report.record(obligation_id, item.nodeid, passed=result.passed, scored=scored)


@pytest.fixture(autouse=True)
def _spans_conform_to_the_contract():
    """Every test's spans are checked against the span contract.

    Enforced suite-wide rather than per-test, because AHC-0011's "complete trace"
    is only meaningful if completeness is checked everywhere it is emitted. A
    contract asserted in the three tests that thought to look is not a contract.
    """
    from support_agent import telemetry as tel

    yield
    exporter = getattr(tel, "_LAST_EXPORTER", None)
    if exporter is None:
        return
    violations = tel.validate(exporter.get_finished_spans())
    assert not violations, "span contract violated:\n  " + "\n  ".join(violations[:12])


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:
    if _report is None or config.getoption("quiet", 0) > 1:
        return
    terminalreporter.write(_report.render())
    if _vocab is not None and _outcomes:
        built = amap.build(_outcomes, _vocab)
        terminalreporter.write_line(amap.summary(built))
        if config.getoption("assurance_map"):
            amap.write(built, Path(__file__).resolve().parents[1] / "evals")
            terminalreporter.write_line("assurance map written to evals/ASSURANCE-MAP.md")

"""The conformance plugin.

A test declares what it discharges with a marker; the run collects outcomes and
prints a report at the end naming what was covered, what failed, and what was
never exercised.

    @pytest.mark.discharges("AAC-0055")
    async def test_the_step_budget_terminates(): ...
    @pytest.mark.scored          # model-driven: pass-rate, not pass/fail
"""

from __future__ import annotations

import pytest

from support_agent.conformance import Report

_report: Report | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _report
    config.addinivalue_line("markers", "discharges(*ids): AAC obligation ids this test exercises")
    config.addinivalue_line(
        "markers", "scored: a model-driven case — reported as a pass rate, never as pass/fail"
    )
    _report = Report()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    result = outcome.get_result()
    if result.when != "call" or _report is None:
        return
    marker = item.get_closest_marker("discharges")
    if marker is None:
        return
    scored = item.get_closest_marker("scored") is not None
    for obligation_id in marker.args:
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

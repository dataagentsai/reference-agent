"""`pass^k`, the arithmetic and what it refuses to say (T-007, AAC-0010).

The measure is the point of the item, so the rows here are about the measure:
what it reports, how sharply it falls, and where it declines to answer. Offline
and instant — the expensive half is running the scenarios, and that is a script
a person runs, not a gate.
"""

from __future__ import annotations

import pytest
from evals import reliability as rel

# (why, passes, runs, k, the figure)
ARITHMETIC = [
    ("every run passed, so every k does", 5, 5, 3, 1.0),
    ("one attempt is the pass rate", 4, 5, 1, 0.8),
    ("four of five passed: two draws both passing", 4, 5, 2, 6 / 10),
    ("and three draws", 4, 5, 3, 4 / 10),
    ("fewer passes than attempts asked for is zero", 2, 5, 3, 0.0),
    ("nothing passed", 0, 5, 1, 0.0),
    ("k equal to the runs is all-or-nothing", 5, 5, 5, 1.0),
    ("one failure in ten kills pass^10", 9, 10, 10, 0.0),
]


@pytest.mark.parametrize(
    ("why", "passed", "runs", "k", "figure"), ARITHMETIC, ids=[r[0] for r in ARITHMETIC]
)
@pytest.mark.discharges("AAC-0010")
def test_pass_hat_k_is_c_over_c(why: str, passed: int, runs: int, k: int, figure: float) -> None:
    assert rel.pass_hat_k(passed, runs, k) == pytest.approx(figure)


# (why, passes, runs, k)
REFUSED = [
    ("more attempts than were run cannot be estimated", 5, 5, 6),
    ("nor can zero attempts", 5, 5, 0),
]


@pytest.mark.parametrize(("why", "passed", "runs", "k"), REFUSED, ids=[r[0] for r in REFUSED])
@pytest.mark.discharges("AAC-0010")
def test_it_refuses_a_figure_it_cannot_estimate(why: str, passed: int, runs: int, k: int) -> None:
    """Returning something plausible would be worse than refusing: a number
    nobody can trace back to runs is a number somebody will quote."""
    with pytest.raises(ValueError):
        rel.pass_hat_k(passed, runs, k)


@pytest.mark.discharges("AAC-0010")
def test_the_suite_figure_is_the_average_scenario_not_the_best() -> None:
    """A deployment's reliability is the average customer's experience. One
    perfect scenario must not carry a broken one."""
    scenarios = [
        rel.Scenario(name="reads", passed=5, runs=5),
        rel.Scenario(name="refunds", passed=1, runs=5),
    ]
    assert rel.averaged(scenarios, 1) == pytest.approx((1.0 + 0.2) / 2)
    assert rel.averaged(scenarios, 2) == pytest.approx((1.0 + 0.0) / 2)


@pytest.mark.discharges("AAC-0010")
def test_the_curve_falls_as_the_customer_asks_again() -> None:
    """The shape is the argument for reporting it: 80% per attempt is 40% by
    the third, and a mean hides exactly that."""
    curve = rel.curve([rel.Scenario(name="one", passed=4, runs=5)], 5)

    assert [k for k, _ in curve] == [1, 2, 3, 4, 5]
    values = [value for _, value in curve]
    assert values == sorted(values, reverse=True), "it can only fall"
    assert values[0] == pytest.approx(0.8)
    assert values[-1] == 0.0


# (why, the checks of each run, passes counted)
RUNS = [
    ("a run passes when every check in it did", [[True, True], [True, False]], 1),
    ("all checks, all runs", [[True], [True]], 2),
    ("a run that asserted nothing is not a pass", [[], [True]], 1),
]


@pytest.mark.parametrize(("why", "runs", "passed"), RUNS, ids=[r[0] for r in RUNS])
@pytest.mark.discharges("AAC-0010")
def test_a_scenario_passes_only_when_all_of_its_checks_do(
    why: str, runs: list[list[bool]], passed: int
) -> None:
    (scenario,) = rel.of_runs({"one": runs})
    assert (scenario.passed, scenario.runs) == (passed, len(runs))


@pytest.mark.tooling
def test_the_report_puts_the_worst_scenario_first() -> None:
    """A report read top-down should start where the work is."""
    scenarios = rel.of_runs(
        {
            "always": [[True], [True]],
            "never": [[False], [False]],
            "sometimes": [[True], [False]],
        }
    )
    table = rel.report(scenarios, runs=2)
    lines = [line for line in table.splitlines() if line.startswith("| `")]

    assert [line.split("`")[1] for line in lines] == ["never", "sometimes", "always"]
    assert "pass^1 | pass^2" in table
    assert "**all scenarios**" in table


# --------------------------------------------------------------------------- #
# What can be measured live at all.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_a_forced_scenario_is_not_measured_live() -> None:
    """A scenario that declares `forces` exists because the model was pushed
    down a path it does not take on its own — the step budget spent, a promise
    with nothing behind it. Run live it measures the forcing, and counting it
    read the agent at 0.93 where its own behaviour was 0.99.
    """
    from scripts.reliability import measurable

    chosen, forced = measurable(None)

    assert forced, "the suite has forced scenarios, and they are named with their reason"
    assert all(why for why in forced.values()), "each says what it forces"
    assert not ({p.stem for p in chosen} & set(forced)), "and none of them is measured"

"""`pass^k`: how often the agent gets it right, not whether it can (T-007, AAC-0010).

Every scenario in the suite runs once against a scripted model, so the suite is
deterministic by construction: it proves the harness works and says nothing
about consistency. This is the other question, and the arithmetic for it.

## Why the pessimistic metric

`pass@k` — did **at least one** of k attempts succeed — suits code generation,
where you can produce ten candidates and ship whichever passes. A support agent
has no such luxury: every customer gets one attempt, and the one they get is
drawn at random from the agent's behaviour. So the measure is τ-bench's
`pass^k`: the probability that **all k** independent attempts of a task
succeed.

Run a scenario `n` times and count `c` passes. The chance that k draws *without
replacement* from those n runs are all passes is

    pass^k = C(c, k) / C(n, k)

which is an unbiased estimate of the probability that k independent attempts all
succeed, and is exactly 0 once `c < k`. Averaged over scenarios, because a
deployment's reliability is the average customer's experience, not the best
scenario's.

## The shape to expect

The numbers do not degrade gently, and that is the point of reporting them: an
agent at 90% per attempt is at 59% by `pass^5`. A mean accuracy hides precisely
the property a deployment cares about, and hides it worse the more the agent is
used.

This is a **report, not a gate.** A reliability figure that fails a build is a
test that is disabled within a week; the number belongs where somebody reads it
and decides, beside the runs that produced it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import comb


@dataclass(frozen=True)
class Scenario:
    """One scenario's runs. `passed` counts the runs where *every* check held —
    a scenario that got the status right and the refusal wrong did not pass."""

    name: str
    passed: int
    runs: int
    failures: tuple[str, ...] = ()

    @property
    def rate(self) -> float:
        return self.passed / self.runs if self.runs else 0.0

    def at(self, k: int) -> float:
        return pass_hat_k(self.passed, self.runs, k)


def pass_hat_k(passed: int, runs: int, k: int) -> float:
    """The chance that k attempts of this scenario all succeed.

    `C(c, k) / C(n, k)`, which is 0 when fewer than k of the runs passed and 1
    when all of them did. Asking for more attempts than were run is not a
    number this can estimate, and says so by refusing rather than by returning
    something plausible.
    """
    if k < 1:
        raise ValueError("k is a number of attempts, so at least one")
    if k > runs:
        raise ValueError(f"pass^{k} needs at least {k} runs; {runs} were made")
    if passed < k:
        return 0.0
    return comb(passed, k) / comb(runs, k)


def averaged(scenarios: Sequence[Scenario], k: int) -> float:
    """`pass^k` over the suite: the mean of each scenario's, because the figure
    that matters is the average customer's experience rather than the best
    scenario's."""
    if not scenarios:
        return 0.0
    return sum(s.at(k) for s in scenarios) / len(scenarios)


def curve(scenarios: Sequence[Scenario], runs: int) -> list[tuple[int, float]]:
    """`pass^k` for every k the runs can support, k = 1 … n."""
    return [(k, averaged(scenarios, k)) for k in range(1, runs + 1)]


def of_runs(results: Mapping[str, Iterable[Iterable[bool]]]) -> list[Scenario]:
    """Scenarios from *runs of checks*: `{scenario: [[check, check], …]}`.

    A run passes when every check in it passed, and a run with no checks at all
    is a failure — a scenario that asserted nothing tells us nothing, and
    counting it as a pass is how a suite quietly measures its own silence.
    """
    out = []
    for name, runs in results.items():
        attempts = [tuple(checks) for checks in runs]
        passed = sum(1 for checks in attempts if checks and all(checks))
        out.append(Scenario(name=name, passed=passed, runs=len(attempts)))
    return sorted(out, key=lambda s: (s.rate, s.name))


def report(scenarios: Sequence[Scenario], runs: int) -> str:
    """The table a person reads, worst scenario first."""
    if not scenarios:
        return "No scenarios ran.\n"
    ks = [k for k, _ in curve(scenarios, runs)]
    head = " | ".join(f"pass^{k}" for k in ks)
    lines = [
        f"| Scenario | runs | passed | {head} |",
        "|---|---:|---:|" + "---:|" * len(ks),
    ]
    for scenario in scenarios:
        cells = " | ".join(f"{scenario.at(k):.2f}" for k in ks)
        lines.append(f"| `{scenario.name}` | {scenario.runs} | {scenario.passed} | {cells} |")
    overall = " | ".join(f"**{value:.2f}**" for _, value in curve(scenarios, runs))
    lines.append(f"| **all scenarios** | {runs} | | {overall} |")
    return "\n".join(lines) + "\n"


__all__ = ["Scenario", "averaged", "curve", "of_runs", "pass_hat_k", "report"]

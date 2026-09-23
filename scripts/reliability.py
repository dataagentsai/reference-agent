"""How often the agent gets it right, measured (T-007, AAC-0010).

    AGENT_PROVIDER_API_KEY=... uv run python scripts/reliability.py --runs 5

Runs every declared scenario `n` times against a real provider and reports
`pass^k` — the chance that all k attempts of a scenario succeed — per scenario
and averaged over the suite, worst first, with the failures kept.

**A report, not a gate.** A reliability figure wired into the build is a test
that fails a third of the time and is disabled within a week. This writes
`docs/RELIABILITY.md` and prints the curve; what to do about the number is a
person's decision.

It reuses `live_runs.once`, so what is measured is the same run the simulation
report describes — one runner, one set of scenarios, two questions asked of it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
from collections import defaultdict
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agenttwin import load_scenario  # noqa: E402
from evals import reliability as rel  # noqa: E402
from scripts.live_runs import SCENARIOS, once  # noqa: E402

from support_agent.config import Settings, resolve  # noqa: E402
from support_agent.contracts import ModelUnavailable, ToolUnavailable  # noqa: E402

UNMEASURED = (ModelUnavailable, ToolUnavailable)
"""A dependency that was down is not the agent getting it wrong. A run that
never reached the model is an attempt nobody made, and counting it as a failure
makes the number describe the machine room instead of the agent — a whole
measurement read 0.00 that way, on an evening the gateway lost its database."""

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEST = ROOT / "docs" / "RELIABILITY.md"


def measurable(only: str | None) -> tuple[list[pathlib.Path], dict[str, str]]:
    """The scenarios worth running live, and the forced ones with their reason."""
    chosen, forced = [], {}
    for path in SCENARIOS:
        if only is not None and not any(part in path.stem for part in only.split(",")):
            continue
        declared = load_scenario(path).forces
        if declared:
            forced[path.stem] = declared
        else:
            chosen.append(path)
    return chosen, forced


async def measure(
    runs: int, only: str | None, pause: float = 10.0
) -> tuple[list[rel.Scenario], float, dict, dict]:
    """Every measurable scenario, `runs` times each.

    A scenario that declares `forces` exists only because the model was pushed
    down a path it does not take on its own — the step budget exhausted, a
    promise made and nothing doing it. Run live it measures the forcing, not the
    agent, and both of ours say so in their own comments: *live, the model asked
    for an approval instead, which is better*. Counting them read the agent at
    0.93 where its own behaviour was 0.99, so they are left out and listed.
    """
    settings = Settings(provider_api_key=os.environ.get("AGENT_PROVIDER_API_KEY", ""))
    chosen, forced = measurable(only)
    checks: dict[str, list[list[bool]]] = defaultdict(list)
    failures: dict[str, list[str]] = defaultdict(list)
    unmeasured: list[str] = []
    spend = 0.0

    for path in chosen:
        for attempt in range(runs):
            # Paced on purpose, and paced to the *agent's own* gateway key:
            # 30 requests a minute (T-029), which a measurement shares with
            # every other caller. Unpaced, the runs come back throttled and the
            # figure ends up over a handful of attempts — the first attempt at
            # this measured LiteLLM's rate limiter rather than the agent.
            await asyncio.sleep(pause)
            spend += await attempt_once(path, settings, checks, failures, unmeasured, attempt, runs)

    if unmeasured:
        failures["— not measured, a dependency was down —"] = unmeasured[:20]
    return rel.of_runs(checks), spend, failures, forced


async def attempt_once(
    path: pathlib.Path,
    settings: Settings,
    checks: dict,
    failures: dict,
    unmeasured: list[str],
    attempt: int,
    runs: int,
) -> float:
    """One run, filed under what it was: a result, a failure, or not measured."""
    where = f"run {attempt + 1}/{runs}"
    try:
        outcomes, _, cost, _ = await once(path, settings)
    except UNMEASURED as exc:
        unmeasured.append(f"{path.stem} {where}: {type(exc).__name__}: {exc}")
        print(f"  --   {path.stem} {where}: not measured ({type(exc).__name__})", flush=True)
        return 0.0
    except Exception as exc:  # noqa: BLE001 — any other crash is a failed run
        checks[path.stem].append([False])
        failures[path.stem].append(f"{where}: {type(exc).__name__}: {exc}")
        print(f"  FAIL {path.stem} {where}: {type(exc).__name__}", flush=True)
        return 0.0

    checks[path.stem].append([o.passed for o in outcomes])
    failures[path.stem] += [f"{where}: {o.check} — {o.detail}" for o in outcomes if not o.passed]
    held = sum(o.passed for o in outcomes)
    mark = "ok  " if held == len(outcomes) and outcomes else "FAIL"
    # Flushed: a run that takes half an hour and prints nothing until the end
    # is a run nobody can tell from a hang.
    print(f"  {mark} {path.stem} {where}: {held}/{len(outcomes)} checks", flush=True)
    return cost


def render(
    scenarios: list[rel.Scenario], runs: int, spend: float, failures: dict, forced: dict
) -> str:
    settings = Settings(provider_api_key="")
    config = resolve(settings)
    curve = rel.curve(scenarios, runs)
    lines = [
        "# Reliability — `pass^k`",
        "",
        f"*{datetime.now(UTC):%Y-%m-%d %H:%M} UTC · {len(scenarios)} scenarios × {runs} runs · "
        f"{config.model} via {config.provider} · ${spend:.4f}*",
        "",
        "`pass^k` is the chance that **all k** attempts of a scenario succeed — the",
        "pessimistic measure, because every customer gets one attempt and the one they",
        "get is drawn at random from the agent's behaviour. A suite that runs each",
        "scenario once says whether the agent *can*; this says how often it *does*.",
        "",
        "The figure for the suite is the average scenario's, not the best one's.",
        "",
        rel.report(scenarios, runs),
        "## The curve",
        "",
        "| k | pass^k |",
        "|---:|---:|",
    ]
    lines += [f"| {k} | {value:.2f} |" for k, value in curve]
    lines += ["", "## Not measurable live", ""]
    if not forced:
        lines.append("Nothing: every scenario runs on the model's own choices.")
    else:
        lines.append(
            "These force a behaviour the model does not choose on its own, so a live run "
            "measures the forcing rather than the agent. Proven offline; excluded here."
        )
        lines.append("")
        lines += [f"- `{name}` — {why}" for name, why in sorted(forced.items())]
    lines += ["", "## What failed", ""]
    if not any(failures.values()):
        lines.append("Nothing.")
    for name in sorted(failures):
        if failures[name]:
            lines.append(f"### `{name}`")
            lines += [f"- {line}" for line in failures[name]]
            lines.append("")
    return "\n".join(lines) + "\n"


async def main(runs: int, only: str | None, pause: float) -> int:
    if not os.environ.get("AGENT_PROVIDER_API_KEY"):
        print("AGENT_PROVIDER_API_KEY is not set", file=sys.stderr)
        return 2
    scenarios, spend, failures, forced = await measure(runs, only, pause)
    thin = [s for s in scenarios if s.runs < runs]
    if thin:
        print(
            f"\n  {len(thin)} scenario(s) measured fewer than {runs} times: a dependency was "
            "down for some attempts, and pass^k is over what actually ran.",
            file=sys.stderr,
        )
    if not scenarios:
        print("no scenarios matched", file=sys.stderr)
        return 2

    DEST.write_text(render(scenarios, runs, spend, failures, forced))
    print(f"\n{rel.report(scenarios, runs)}")
    for k, value in rel.curve(scenarios, runs):
        print(f"  pass^{k} = {value:.2f}")
    print(f"\n{DEST}  ${spend:.4f}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5, help="attempts per scenario")
    parser.add_argument("--only", help="comma-separated substrings of the scenarios to measure")
    parser.add_argument(
        "--pause",
        type=float,
        default=10.0,
        help="seconds between runs, to stay under the key's rpm",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.runs, args.only, args.pause)))

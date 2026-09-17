"""Run every declared scenario against a real model, N times, and publish it.

Everything up to here has driven a scripted model: the suite fixed the agent's
choices and proved the wiring, the world, the guardrails and the oracles. **This
is the first thing that asks whether the agent decides correctly**, and it is the
first thing in the programme that can fail for an interesting reason.

    AGENT_PROVIDER_API_KEY=... uv run python scripts/live_runs.py [--runs 5]

**Scored as pass rates, never as pass/fail.** A model is stochastic; one run
saying yes is not evidence and one run saying no is not a defect. A rate below
one is a finding to investigate, and the report keeps the transcripts of the
runs that failed, because a report without its failures in it is marketing.

The key is read from the environment and never printed, never written, and never
committed.
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

from agenttwin import (  # noqa: E402
    Clock,
    Live,
    load,
    load_scenario,
    perturbed,
    provider_faults,
    run_file,
    timeline_for,
)
from evals.simulation import subject_for, voice_of

from support_agent.config import Settings, resolve
from support_agent.cost import Meter
from support_agent.llm import connect_model

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCENARIOS = sorted((ROOT / "scenarios").glob("*.yaml"))
DEST = ROOT / "docs" / "SIMULATION-REPORT.md"


async def once(path: pathlib.Path, settings: Settings) -> tuple[list, tuple, float, str]:
    """One run of one scenario against the real provider."""
    scenario = load_scenario(path)
    live = Live.start(load(path.parent / scenario.world))
    config = resolve(settings)
    meters: list[Meter] = []
    timeline = timeline_for(scenario)
    # One clock for the agent, the world and the people offstage, composed as the
    # suite composes it. Without it a lapse that waits on time never fires live:
    # `nobody-picks-up-the-escalation` passed offline and failed live (17 Sep)
    # because this runner gave the desk and the agent no shared clock (F-033's
    # lesson, relearned by the one runner that had not taken it).
    clock = Clock(step_s=scenario.step_seconds)
    # Always wrapped, even with nothing to fire: the wrapper is what counts calls,
    # and a scenario asserting that the agent *read* before acting needs them.
    wrap = perturbed(live, timeline, clock)

    client, _ = await connect_model(config, api_key=settings.provider_api_key)
    async with subject_for(
        live,
        llm=client,
        wrap=wrap,
        provider_faults=provider_faults(scenario),
        config=config,
        meters=meters,
        clock=clock,
    ) as subject:
        record, outcomes = await run_file(
            path,
            subject=subject,
            live=live,
            timeline=timeline,
            clock=clock,
            voice=voice_of(client),
        )
    return (
        list(outcomes),
        record.transcript,
        float(sum(m.spend for m in meters)),
        record.determinism_class,
    )


async def main(runs: int) -> int:
    settings = Settings(provider_api_key=os.environ.get("AGENT_PROVIDER_API_KEY", ""))
    if not settings.provider_api_key:
        print("AGENT_PROVIDER_API_KEY is not set", file=sys.stderr)
        return 2

    results: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    failures: dict[str, list[str]] = defaultdict(list)
    spend: dict[str, float] = defaultdict(float)
    crashed: dict[str, str] = {}
    unscripted: dict[str, tuple] = {}

    for path in SCENARIOS:
        for attempt in range(runs):
            try:
                outcomes, transcript, cost, determinism = await once(path, settings)
            except Exception as exc:  # noqa: BLE001 — a crash is a result too
                crashed[path.stem] = f"{type(exc).__name__}: {exc}"
                break
            spend[path.stem] += cost
            if determinism == "model_driven":
                unscripted[f"{path.stem} · run {attempt + 1}"] = transcript
            for outcome in outcomes:
                results[path.stem][outcome.check].append(outcome.passed)
                if not outcome.passed:
                    failures[path.stem].append(
                        f"run {attempt + 1}: {outcome.check} — {outcome.detail}"
                    )
            print(
                f"  {path.stem} run {attempt + 1}/{runs}: "
                f"{sum(o.passed for o in outcomes)}/{len(outcomes)} checks"
            )

    forced = {p.stem: load_scenario(p).forces for p in SCENARIOS if load_scenario(p).forces}
    DEST.write_text(
        render(results, failures, spend, crashed, runs, settings, forced) + _unscripted(unscripted)
    )
    total = sum(spend.values())
    print(f"\n{DEST}  {len(SCENARIOS)} scenarios × {runs} runs, ${total:.4f}")
    return 0


def render(results, failures, spend, crashed, runs, settings, forced=None) -> str:
    forced = forced or {}
    config = resolve(settings)
    lines = [
        "# Simulation report",
        "",
        "*Generated by `scripts/live_runs.py`. Never edit by hand — regenerate.*",
        "",
        f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} · model `{config.model}` · "
        f"{runs} run(s) per scenario",
        "",
        "Every scenario against a **real model**. Everything else in this suite",
        "scripts the agent's choices and therefore proves the wiring, the world and",
        "the guardrails; this asks whether the agent *decides* correctly, which is a",
        "different question and the only one a customer experiences.",
        "",
        "**Pass rates, never pass/fail.** A model is stochastic: one run saying yes is",
        "not evidence, and one run saying no is not a defect. A rate below 1.00 is a",
        "finding to look into, and the failures are kept below — a report without them",
        "in it is marketing.",
        "",
        "| Scenario | Checks | Pass rate | Cost |",
        "|---|---|---|---|",
    ]
    for scenario in sorted(set(results) | set(crashed)):
        if scenario in crashed:
            lines.append(f"| `{scenario}` | — | **crashed** | ${spend.get(scenario, 0):.4f} |")
            continue
        checks = results[scenario]
        passed = sum(sum(v) for v in checks.values())
        total = sum(len(v) for v in checks.values())
        rate = passed / total if total else 0.0
        mark = "" if rate == 1.0 else " · guard" if scenario in forced else " ⚠"
        lines.append(
            f"| `{scenario}` | {len(checks)} | **{rate:.2f}**{mark} | ${spend[scenario]:.4f} |"
        )

    return "\n".join(
        lines + _guards(forced) + _per_check(results) + _crashed(crashed) + _failures(failures)
    )


def _guards(forced) -> list[str]:
    """Scenarios that need the model to misbehave, so their guard fires.

    Marked `· guard`, not `⚠`: below 1.00 there means the live model did not
    commit the misbehaviour and the guard was not needed. The guard itself is
    proven by the scripted suite, where the model is made to commit it (T-050).
    """
    if not forced:
        return []
    lines = ["", "## Guards a live model did not need", ""]
    lines += [
        "Each forces a misbehaviour so its guard fires. Scripted, it is proven; live,",
        "a check waiting on the guard reads below 1.00 when the model behaved.",
        "",
    ]
    return lines + [f"- `{name}` forces {why}" for name, why in sorted(forced.items())] + [""]


def _unscripted(conversations) -> str:
    """What a model-driven customer actually said.

    A run nobody can reproduce is worth something only if a reader can see what
    happened in it. For a scripted scenario the transcript is the scenario file;
    for this one it is the entire finding.
    """
    if not conversations:
        return ""
    lines = [
        "",
        "## Unscripted conversations",
        "",
        "Played by a model under a declared persona, and **not reproducible** — these",
        "are here to be read, never to be regressed against.",
        "",
    ]
    for name, turns in conversations.items():
        lines += [f"### {name}", ""]
        for said, heard in turns:
            lines += [f"- **customer** {said}", f"  - **agent** {heard}"]
        lines.append("")
    return "\n".join(lines)


def _per_check(results) -> list[str]:
    lines = ["", "## Per check", ""]
    for scenario in sorted(results):
        lines += [f"### {scenario}", ""]
        for check, outcomes in results[scenario].items():
            lines.append(f"- `{sum(outcomes) / len(outcomes):.2f}` {check}")
        lines.append("")
    return lines


def _crashed(crashed) -> list[str]:
    if not crashed:
        return []
    lines = ["## Crashed", "", "A run that raised is a result, and this is it.", ""]
    return lines + [f"- `{name}` — {why}" for name, why in sorted(crashed.items())] + [""]


def _failures(failures) -> list[str]:
    if not failures:
        return [
            "## What failed",
            "",
            "Nothing, in this run. That is a weaker statement than it looks at these",
            "run counts, and the honest reading is *no failure was observed* rather",
            "than *no failure exists*.",
            "",
        ]
    lines = ["## What failed", ""]
    for scenario, entries in sorted(failures.items()):
        lines += [f"### {scenario}", ""] + [f"- {entry}" for entry in entries] + [""]
    return lines


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="runs per scenario")
    raise SystemExit(asyncio.run(main(parser.parse_args().runs)))

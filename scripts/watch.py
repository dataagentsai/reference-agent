"""Run the online rules over production turns, on a schedule (T-057).

    uv run python scripts/watch.py --every 60            # forever
    uv run python scripts/watch.py --once --lookback 3600

Reads turns from Langfuse, runs `support_agent.watch.rules`, infers outcomes,
writes every verdict back to Langfuse as a score and counts it as a metric. The
counts go to `AGENT_METRICS_ENDPOINT`, where Prometheus's rules decide what pages
(deploy/prometheus/rules/agent.yml).

Nothing here is on a customer's request path. A watch that stops is paged on by
the absence of its counts, not by anything it says.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_harness import telemetry as tel  # noqa: E402
from agent_harness.watch.langfuse import Langfuse  # noqa: E402
from support_agent.config import Settings  # noqa: E402
from support_agent.watch import Watch  # noqa: E402


def main(every: float, once: bool, lookback: float, settle: float) -> None:
    settings = Settings()
    tel.configure(
        service_name="support-agent-watch",
        environment=settings.deployment,
        metrics_endpoint=settings.metrics_endpoint or None,
    )
    langfuse = Langfuse(
        os.environ.get("LANGFUSE_URL", "http://localhost:3000"),
        os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local-dev-only"),
        os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local-dev-only"),
    )
    watch = Watch(langfuse, langfuse, settle_s=settle, cursor=time.time() - settle - lookback)
    while True:
        report = watch.once(time.time())
        pages = [f for f in report.findings if f.severity == "page"]
        print(
            f"{time.strftime('%H:%M:%S')}  evaluated {len(report.evaluated)}"
            f"  findings {len(report.findings)} ({len(pages)} page)"
            f"  outcomes {len(report.outcomes)}"
        )
        for found in report.findings:
            print(f"    {found.rule} {found.severity:6} {found.trace_id}  {found.detail}")
        for outcome in report.outcomes:
            print(f"    outcome {outcome.kind} on {outcome.trace_id}  {outcome.detail}")
        tel.flush_metrics()
        if once:
            return
        time.sleep(every)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--every", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--lookback", type=float, default=3600.0, help="seconds, on the first pass")
    parser.add_argument("--settle", type=float, default=60.0, help="ingestion delay to allow")
    args = parser.parse_args()
    main(args.every, args.once, args.lookback, args.settle)

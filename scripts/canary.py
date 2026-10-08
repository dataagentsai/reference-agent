"""The canary: a synthetic customer through the deployed edge, on a schedule (T-056).

    uv run python scripts/canary.py --every 600          # forever
    uv run python scripts/canary.py --once

Signs in as C-7001 — a realm user whose orders nobody else has — through the
same public client a customer's chat uses, sends the four cases in
`support_agent.watch.canary` to `/chat`, and counts each as passed or failed.
Prometheus pages on a failure and on silence (deploy/prometheus/rules/agent.yml).

With no realm configured (`AGENT_ISSUER_URL` empty) the token comes from the
local test issuer, which only a server started without a realm accepts.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evals import issuer as local_issuer  # noqa: E402

from agent_harness import telemetry as tel
from support_agent.config import Settings  # noqa: E402
from support_agent.watch import canary  # noqa: E402

CUSTOMER = "C-7001"


def token_source(settings: Settings):
    if not settings.issuer_url:
        return lambda: local_issuer.mint(CUSTOMER, now=int(time.time()))
    return lambda: canary.password_token(
        settings.issuer_url,
        os.environ.get("CANARY_CLIENT_ID", "support-chat"),
        os.environ.get("CANARY_USERNAME", "c-7001"),
        os.environ.get("CANARY_PASSWORD", "local-dev-only"),
    )


def main(url: str, every: float, once: bool) -> int:
    settings = Settings()
    tel.configure(
        service_name="support-agent-canary",
        environment=settings.deployment,
        metrics_endpoint=settings.metrics_endpoint or None,
    )
    probe = canary.Canary(canary.over_http(url), token_source(settings))
    while True:
        results = probe.once()
        failed = [r for r in results if not r.passed]
        print(f"{time.strftime('%H:%M:%S')}  {len(results) - len(failed)}/{len(results)} passed")
        for r in results:
            mark = "ok  " if r.passed else "FAIL"
            print(f"    {mark} {r.case:28} {r.seconds:5.1f}s  {r.reason}")
        tel.flush_metrics()
        if once:
            return 1 if failed else 0
        time.sleep(every)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8077")
    parser.add_argument("--every", type=float, default=600.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    sys.exit(main(args.url, args.every, args.once))

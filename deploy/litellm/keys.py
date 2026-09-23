"""The support agent's key at the gateway: which models, how fast, how much (T-029).

Run once by compose after the proxy is healthy, and safe to run again: a key
that exists is updated to match, so changing a limit here and re-running
converges rather than failing. Standard library only, because it runs inside
the LiteLLM image with nothing installed.

A gateway key per caller is what makes the limits mean something. The provider
credential stays in the proxy and never reaches the agent, and spend is
attributed to the caller that made it (AAC-0104), not to one shared key.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

PROXY = os.environ.get("LITELLM_URL", "http://litellm:4000")
MASTER = os.environ["LITELLM_MASTER_KEY"]

MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
"""The agent's own allowlist, restated: the gateway refuses what the agent would
have refused at startup, for any caller holding one of these keys."""

KEYS = [
    {
        "key": os.environ["AGENT_GATEWAY_KEY"],
        "key_alias": "support-agent",
        "models": MODELS,
        "rpm_limit": int(os.environ.get("AGENT_GATEWAY_RPM", "30")),
        "max_budget": float(os.environ.get("AGENT_GATEWAY_BUDGET_USD", "5")),
        "budget_duration": os.environ.get("AGENT_GATEWAY_BUDGET_PERIOD", "30d"),
        "metadata": {"caller": "support-agent"},
    },
    {
        # Measurement is not customer traffic, and borrowing the agent's key
        # made the reliability run measure the rate limiter: every retry spent
        # another of the thirty requests a minute the agent is held to (T-007).
        # Its own key, so its spend is attributable and the agent's limit is
        # never relaxed to make a report finish.
        "key": os.environ.get("EVAL_GATEWAY_KEY", "sk-support-eval-local-dev-only"),
        "key_alias": "support-eval",
        "models": MODELS,
        "rpm_limit": int(os.environ.get("EVAL_GATEWAY_RPM", "600")),
        "max_budget": float(os.environ.get("EVAL_GATEWAY_BUDGET_USD", "10")),
        "budget_duration": os.environ.get("EVAL_GATEWAY_BUDGET_PERIOD", "30d"),
        "metadata": {"caller": "support-eval"},
    },
]


def call(path: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        f"{PROXY}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {MASTER}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def provision(key: dict[str, object]) -> int:
    alias = key["key_alias"]
    limits = f"{key['rpm_limit']} rpm, ${key['max_budget']} per {key['budget_duration']}"
    status, created = call("/key/generate", key)
    if status == 200:
        print(f"created the {alias} key: {limits}")
        return 0
    status, updated = call("/key/update", key)
    if status == 200:
        print(f"updated the {alias} key: {limits}")
        return 0
    print(f"could not create or update the {alias} key: {created} / {updated}", file=sys.stderr)
    return 1


def main() -> int:
    """Every key, converging: created if new, updated if it exists."""
    return 1 if any(provision(key) for key in KEYS) else 0


if __name__ == "__main__":
    raise SystemExit(main())

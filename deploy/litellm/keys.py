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

KEY = {
    "key": os.environ["AGENT_GATEWAY_KEY"],
    "key_alias": "support-agent",
    # The agent's own allowlist, restated: the gateway refuses what the agent
    # would have refused at startup, for any caller holding this key.
    "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
    "rpm_limit": int(os.environ.get("AGENT_GATEWAY_RPM", "30")),
    "max_budget": float(os.environ.get("AGENT_GATEWAY_BUDGET_USD", "5")),
    "budget_duration": os.environ.get("AGENT_GATEWAY_BUDGET_PERIOD", "30d"),
    "metadata": {"caller": "support-agent"},
}


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


def main() -> int:
    limits = f"{KEY['rpm_limit']} rpm, ${KEY['max_budget']} per {KEY['budget_duration']}"
    status, created = call("/key/generate", KEY)
    if status == 200:
        print(f"created the support-agent key: {limits}")
        return 0
    status, updated = call("/key/update", KEY)
    if status == 200:
        print(f"updated the support-agent key: {limits}")
        return 0
    print(f"could not create or update the key: {created} / {updated}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

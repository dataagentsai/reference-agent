"""Tell Langfuse what this agent's models cost, from the agent's own price table.

    uv run python deploy/langfuse/models.py

Langfuse prices a model call from its token counts when it knows the model, and
it does not know `openai/gpt-oss-120b`. So every trace arrived with tokens and no
cost (T-052). The agent already has a price table — the one its budgets are
enforced with — and this registers that table, rather than a second copy of the
numbers somebody would have to keep in step.

Idempotent: a model already registered with the same prices is left alone.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

from support_agent.cost import PRICES

URL = os.environ.get("LANGFUSE_URL", "http://localhost:3000")
PUBLIC = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local-dev-only")
SECRET = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local-dev-only")


def call(method: str, path: str, body: dict | None = None) -> dict:
    auth = base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
    request = urllib.request.Request(
        f"{URL}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"authorization": f"Basic {auth}", "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def registered() -> set[str]:
    """Every model Langfuse can price. Paged: it ships hundreds of its own."""
    names: set[str] = set()
    page = 1
    while True:
        found = call("GET", f"/api/public/models?limit=100&page={page}")
        names |= {m["modelName"] for m in found["data"]}
        if page >= found["meta"]["totalPages"]:
            return names
        page += 1


def main() -> int:
    known = registered()
    for name, price in PRICES.items():
        if name in known:
            print(f"  {name}: already registered")
            continue
        call(
            "POST",
            "/api/public/models",
            {
                "modelName": name,
                # Anchored, so a name that merely contains this one is not priced as it.
                "matchPattern": f"(?i)^{re.escape(name)}$",
                "unit": "TOKENS",
                "inputPrice": float(price.input_per_mtok) / 1_000_000,
                "outputPrice": float(price.output_per_mtok) / 1_000_000,
            },
        )
        print(f"  {name}: registered at {price.input_per_mtok}/{price.output_per_mtok} per Mtok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as exc:
        print(f"no Langfuse at {URL}: {exc}", file=sys.stderr)
        sys.exit(1)

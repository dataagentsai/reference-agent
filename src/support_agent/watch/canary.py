"""The canary: a synthetic customer's cases through the deployed edge (AHC-0113).

    uv run python scripts/canary.py --every 600

Monitoring built on traffic reads a broken deployment as a quiet hour. This
supplies the traffic: every period, customer C-7001 — a real realm user whose
orders nobody else has — signs in and sends four messages through `/chat`,
exactly as a customer's browser would, and each answer is checked. A failed case
is counted, and Prometheus pages on it; a canary that stops reporting at all is
paged on too, by the absence of its count (AAC-0116, AAC-0079).

The cases are chosen to cross every hop at least once and to write nothing:

- **status** crosses the edge, identity, the store's MCP server and Saleor,
  with no model: its delivered order must be reported delivered.
- **orders** adds the gateway and the model: its orders must be named.
- **return outside the window** adds a refusal the store owns: the reply must
  not claim a return was opened.
- **someone else's order** tests ownership at the far end: nothing about
  another customer's order may come back, and the reply may not describe it
  as though it were found (F-062).

Its turns are marked synthetic by the agent (`AGENT_SYNTHETIC_CUSTOMERS`) and
excluded from every rate and from the online rules.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from support_agent import telemetry as tel
from support_agent.watch.checks import plain
from support_agent.watch.rules import CLAIMS

Answer = tuple[int, dict[str, Any]]
Post = Callable[[str, str], Answer]
"""(text, token) → (status, body). The deployed edge, or a test's stand-in."""


@dataclass(frozen=True)
class Case:
    name: str
    text: str
    expect: Callable[[int, dict[str, Any]], str | None]
    """None when the answer is right; otherwise what was wrong with it."""


@dataclass(frozen=True)
class Result:
    case: str
    passed: bool
    reason: str
    seconds: float


def _answered(status: int, body: dict[str, Any]) -> str | None:
    if status >= 500:
        return f"HTTP {status}: {body.get('error') or body.get('reply', '')}"[:200]
    if status >= 400:
        return f"HTTP {status}: {body.get('error', '')}"[:200]
    return None


def _reply(body: dict[str, Any]) -> str:
    return plain(str(body.get("reply", "")))


def _status(status: int, body: dict[str, Any]) -> str | None:
    wrong = _answered(status, body)
    if wrong:
        return wrong
    if "delivered" not in _reply(body).lower():
        return f"CN-70002 is delivered and the reply did not say so: {_reply(body)[:120]}"
    return None


def _orders(status: int, body: dict[str, Any]) -> str | None:
    wrong = _answered(status, body)
    if wrong:
        return wrong
    if "CN-7000" not in _reply(body):
        return f"none of its orders were named: {_reply(body)[:120]}"
    return None


def _no_return(status: int, body: dict[str, Any]) -> str | None:
    wrong = _answered(status, body)
    if wrong:
        return wrong
    if CLAIMS["open_return_request"].search(_reply(body)):
        return f"claimed a return it cannot open: {_reply(body)[:120]}"
    return None


def _not_theirs(status: int, body: dict[str, Any]) -> str | None:
    # Any 4xx or 5xx fails it: a canary that could not get through has not
    # shown that nothing leaks (found live: a 401 passed this case).
    wrong = _answered(status, body)
    if wrong:
        return wrong
    if "Park Street" in _reply(body) or "4999" in _reply(body):
        return "another customer's order came back"
    # Nothing leaking is not the whole of a right answer: the store said the
    # order is not there, and a reply giving it a status or a refund state
    # describes an order that does not exist for this customer (F-062).
    if _DESCRIBED.search(_reply(body)):
        return f"described an order the store did not find: {_reply(body)[:120]}"
    return None


_DESCRIBED = re.compile(r"\bis currently\b|\bno refund\b|\brefund has been\b", re.I)


CASES: tuple[Case, ...] = (
    Case("status", "Where is my order CN-70002?", _status),
    Case("orders", "What orders do I have with you?", _orders),
    Case("return outside the window", "I would like to return CN-70002 please", _no_return),
    Case("someone else's order", "Where is my order AB-10003?", _not_theirs),
)


@dataclass
class Canary:
    post: Post
    token: Callable[[], str]
    cases: tuple[Case, ...] = CASES

    def once(self) -> list[Result]:
        token = self.token()
        out = []
        for case in self.cases:
            started = time.monotonic()
            try:
                status, body = self.post(case.text, token)
                reason = case.expect(status, body)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                reason = f"unreachable: {exc}"[:200]
            passed = reason is None
            out.append(Result(case.name, passed, reason or "", time.monotonic() - started))
            tel.counters.canary.add(1, {"case": case.name, "outcome": "pass" if passed else "fail"})
        return out


def over_http(base_url: str, timeout_s: float = 60.0) -> Post:
    """POST /chat as a browser would, each message its own conversation, each
    with a fresh delivery id so a retry of the canary is not a duplicate."""

    def post(text: str, token: str) -> Answer:
        request = urllib.request.Request(  # noqa: S310 — the URL is configuration
            base_url.rstrip("/") + "/chat",
            method="POST",
            data=json.dumps({"text": text}).encode(),
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
                "idempotency-key": f"canary:{uuid.uuid4()}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    return post


def password_token(issuer_url: str, client_id: str, username: str, password: str) -> str:
    """A customer's token from the realm, as the chat client obtains one."""
    request = urllib.request.Request(  # noqa: S310 — the URL is configuration
        issuer_url.rstrip("/") + "/protocol/openid-connect/token",
        method="POST",
        data=urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": client_id,
                "username": username,
                "password": password,
                "scope": "openid",
            }
        ).encode(),
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        return str(json.load(response)["access_token"])


__all__ = ["CASES", "Canary", "Case", "Result", "over_http", "password_token"]

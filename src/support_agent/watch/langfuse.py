"""Where the watch reads turns from and writes its verdicts to: Langfuse.

Read through `/api/public/v2/observations` — the only read Langfuse v4 serves in
its events-only mode — filtered by name, user and time; written through
`/api/public/scores`, with an id derived from the trace, the name and the
version, so writing the same verdict twice is one score and re-running the watch
over a window it has seen changes nothing in Langfuse.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from support_agent.watch.outcomes import OUTCOME_VERSION, Outcome
from support_agent.watch.rules import Finding, Verdict

FIELDS = "core,basic,metadata"
SCORE_FIELDS = "details,subject"


@dataclass
class Langfuse:
    url: str
    public_key: str
    secret_key: str = field(repr=False)
    timeout_s: float = 20.0

    # -- reading -----------------------------------------------------------

    def observations(self, **query: str) -> Iterator[Mapping[str, Any]]:
        """Every observation matching `query`, across pages."""
        cursor: str | None = None
        while True:
            params = {"limit": "200", "fields": FIELDS, **query}
            if cursor:
                params["cursor"] = cursor
            page = self._call(
                "GET", "/api/public/v2/observations?" + urllib.parse.urlencode(params)
            )
            yield from page.get("data", [])
            cursor = (page.get("meta") or {}).get("cursor")
            if not cursor or not page.get("data"):
                return

    def traces_between(self, start: float, end: float) -> list[Mapping[str, Any]]:
        """Every observation of every trace holding a turn that began in [start, end)."""
        turns = self.observations(name="agent.turn", **_window(start, end))
        ids = sorted({str(o["traceId"]) for o in turns})
        return [o for trace in ids for o in self.observations(traceId=trace)]

    def turns_of(self, user: str, start: float, end: float) -> list[Mapping[str, Any]]:
        """A customer's turn spans only — enough to tell one conversation from the next."""
        return list(self.observations(name="agent.turn", userId=user, **_window(start, end)))

    def feedback_between(self, start: float, end: float) -> list[Mapping[str, Any]]:
        return list(self.observations(name="agent.feedback", **_window(start, end)))

    def score_pages(self, **query: str) -> Iterator[dict[str, Any]]:
        """Every page of the project's scores, as the v3 API answers them.

        `GET /api/public/v3/scores?fields=details,subject` — v2 is deprecated on
        Langfuse Cloud from 16 Nov 2026, and without `details` a v3 page has no
        `metadata`, so no `aac` declaration would survive the export. Pages are
        kept whole, cursor and all, because the adapter that reads them warns
        when the last one still has a cursor: an export that stopped early must
        not read as the whole history."""
        cursor: str | None = None
        while True:
            params = {"limit": "100", "fields": SCORE_FIELDS, **query}
            if cursor:
                params["cursor"] = cursor
            page = self._call("GET", "/api/public/v3/scores?" + urllib.parse.urlencode(params))
            yield page
            cursor = (page.get("meta") or {}).get("cursor")
            if not cursor or not page.get("data"):
                return

    # -- writing -----------------------------------------------------------

    def finding(self, found: Finding) -> None:
        self.score(
            trace_id=found.trace_id,
            name=f"watch.{found.rule}",
            value=0,
            data_type="BOOLEAN",
            comment=found.detail,
            metadata={
                "version": found.version,
                "severity": found.severity,
                **declared(found.aac, found.mechanism, _trend(found.severity)),
            },
        )

    def passed(self, verdict: Verdict) -> None:
        self.score(
            trace_id=verdict.trace_id,
            name=f"watch.{verdict.rule}",
            value=1,
            data_type="BOOLEAN",
            comment="held",
            metadata={
                "version": verdict.version,
                "severity": verdict.severity,
                **declared(verdict.aac, verdict.mechanism, verdict.outcome),
            },
        )

    def evaluated(self, trace_id: str, findings: int, version: str) -> None:
        # AAC-0014 asks that sampled production traces be scored; this score on
        # the trace is that scoring having happened, so its verdict is a pass
        # whatever the count. What the count found is the rules' own scores'.
        self.score(
            trace_id=trace_id,
            name="watch.findings",
            value=findings,
            data_type="NUMERIC",
            comment="findings from the online rules",
            metadata={"version": version, **declared(("AAC-0014",), "M5", "pass")},
        )

    def outcome(self, found: Outcome) -> None:
        # A later outcome attached to the run that produced it (AAC-0115). The
        # score existing on that trace is the join, so its verdict is a pass;
        # whether the outcome was good news is the value, not the verdict.
        self.score(
            trace_id=found.trace_id,
            name="outcome",
            value=found.kind,
            data_type="CATEGORICAL",
            comment=found.detail,
            metadata={
                "source": found.source,
                "version": OUTCOME_VERSION,
                **declared(("AAC-0115",), "M5", "pass"),
            },
        )

    def score(
        self,
        *,
        trace_id: str,
        name: str,
        value: Any,
        data_type: str,
        comment: str,
        metadata: Mapping[str, Any],
    ) -> None:
        basis = f"{trace_id}|{name}|{metadata.get('version', '')}|{value}"
        body = {
            "id": "w" + hashlib.sha256(basis.encode()).hexdigest()[:24],
            "traceId": trace_id,
            "name": name,
            "value": value,
            "dataType": data_type,
            "comment": comment,
            "metadata": dict(metadata),
        }
        self._call("POST", "/api/public/scores", body)

    # ---------------------------------------------------------------------

    def _call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        token = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode()
        request = urllib.request.Request(  # noqa: S310 — the URL is configuration
            self.url.rstrip("/") + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"authorization": f"Basic {token}", "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
            found: dict[str, Any] = json.load(response)
            return found


def declared(aac: tuple[str, ...], mechanism: str, outcome: str | None) -> dict[str, Any]:
    """What a coverage report reads off a score (AAC adapters/langfuse.js).

    `aac` names the obligations, `aac.mechanism` how the check decided — never
    left for the reader to infer from Langfuse's `source`, which does not say —
    and `aac.outcome` the verdict where the score's value is not one. A score
    that evidences nothing carries none of it and the adapter passes it by."""
    if not aac:
        return {}
    stated: dict[str, Any] = {"aac": list(aac), "aac.mechanism": mechanism}
    if outcome is not None:
        stated["aac.outcome"] = outcome
    return stated


def _trend(severity: str) -> str | None:
    return "unknown" if severity == "trend" else None


def _window(start: float, end: float) -> dict[str, str]:
    return {"fromStartTime": _iso(start), "toStartTime": _iso(end)}


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


__all__ = ["Langfuse", "declared"]

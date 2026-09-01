"""Whether yesterday can be reproduced.

L9 · P3. Record and replay at the L2 choke point.

This is AgentTwin's `replay` resolution mode, and the first piece of it that
exists. `ScriptedClient` is a hand-written script; a cassette is a *recording of
something that actually happened*, which is a different and more useful thing:
it is what lets a failing production run become a permanent regression case
without anybody re-keying it (AHC-0029).

**Recording at the choke point, not at HTTP.** VCR-style tools record HTTP
frames, which is universal and blind — a cassette of raw frames cannot tell you
which call was which, and breaks when a header or an SDK version moves. We own
`LLMClient`, so recording there captures the exchange *semantically*: the request
we meant and the response we got.

### The matching problem

A replay must decide which recorded exchange answers the request in front of it.
Two strategies, and the choice is a real one:

`ORDERED` — the nth call gets the nth recording. Strict: any change in the
sequence is a mismatch, which is exactly what you want from a regression case.

`BY_REQUEST` — match on a hash of the request. Tolerant of reordering, and it
quietly hides the very drift a regression case exists to catch, so it is not the
default.

A miss is an error either way. A replay that falls through to the network is not
a replay, and it is the single easiest way to make a "free, offline" suite quietly
cost money.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from support_agent import telemetry as tel
from support_agent.contracts import LLMClient, ModelRequest, ModelResponse, ModelUnavailable

FORMAT_VERSION = 1


class Match(StrEnum):
    ORDERED = "ordered"
    BY_REQUEST = "by_request"


class CassetteMiss(Exception):
    """Nothing recorded answers this request.

    Raised rather than falling through to the provider. A replay that reaches
    the network is not a replay — it is an offline suite that silently costs
    money and stops being reproducible on the day it matters.
    """


def fingerprint(request: ModelRequest) -> str:
    """A stable identity for a request.

    Only what the provider is actually told: messages, tool names, and the
    sampling parameters that change the answer. Tool *schemas* are excluded —
    a description reworded between runs is not a different question, and
    including it would invalidate every cassette on a docstring edit.
    """
    material = {
        "messages": [
            {"role": m.role, "content": m.content, "tool_call_id": m.tool_call_id}
            for m in request.messages
        ],
        "tools": sorted(
            str(t.get("function", {}).get("name", ""))
            for t in request.tools  # type: ignore[union-attr]
        ),
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass
class Exchange:
    fingerprint: str
    response: ModelResponse

    def to_json(self) -> dict[str, object]:
        return {"fingerprint": self.fingerprint, "response": self.response.model_dump(mode="json")}

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> Exchange:
        return cls(
            fingerprint=str(raw["fingerprint"]),
            response=ModelResponse.model_validate(raw["response"]),
        )


class Cassette:
    """A recording. Reads and writes one JSON file."""

    def __init__(self, exchanges: list[Exchange] | None = None) -> None:
        self.exchanges: list[Exchange] = list(exchanges or [])

    def __len__(self) -> int:
        return len(self.exchanges)

    def __iter__(self) -> Iterator[Exchange]:
        return iter(self.exchanges)

    def save(self, path: Path | str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "format": FORMAT_VERSION,
                    "exchanges": [e.to_json() for e in self.exchanges],
                },
                indent=2,
            )
        )

    @classmethod
    def load(cls, path: Path | str) -> Cassette:
        raw = json.loads(Path(path).read_text())
        version = raw.get("format")
        if version != FORMAT_VERSION:
            raise ValueError(
                f"cassette format {version!r} is not {FORMAT_VERSION} — "
                "re-record rather than reinterpreting an old recording"
            )
        return cls([Exchange.from_json(e) for e in raw["exchanges"]])


class Recorder:
    """Wraps a live client and keeps what passed through.

    Transparent: the wrapped client sees the same requests it would have seen,
    and the caller gets the same responses. Recording never changes behaviour,
    which is the only way a recording is worth anything.
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.cassette = Cassette()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self._inner.complete(request)
        self.cassette.exchanges.append(Exchange(fingerprint(request), response))
        return response


class Player:
    """Replays a cassette. Never touches the network.

    Deliberately has no fallback client. A `Player` cannot reach a provider even
    if a recording is missing, so "this suite makes no calls" is a property of
    the type rather than a promise in a docstring.
    """

    def __init__(self, cassette: Cassette, *, match: Match = Match.ORDERED) -> None:
        self._cassette = cassette
        self._match = match
        self._position = 0
        self.plays = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        with tel.span(
            "gen_ai.chat",
            **{tel.GEN_AI_SYSTEM: "cassette", tel.RESOLUTION: "replay"},
        ) as span:
            wanted = fingerprint(request)
            exchange = self._next(wanted)
            self.plays += 1
            span.set_attribute("agent.cassette.match", self._match.value)
            tel.set_usage(
                span,
                input_tokens=exchange.response.usage.input_tokens,
                output_tokens=exchange.response.usage.output_tokens,
            )
            return exchange.response

    def _next(self, wanted: str) -> Exchange:
        if self._match is Match.ORDERED:
            if self._position >= len(self._cassette.exchanges):
                raise CassetteMiss(
                    f"cassette exhausted after {self._position} plays — "
                    "the run is longer than the recording"
                )
            exchange = self._cassette.exchanges[self._position]
            self._position += 1
            if exchange.fingerprint != wanted:
                raise CassetteMiss(
                    f"call {self._position} does not match the recording "
                    f"(recorded {exchange.fingerprint}, got {wanted}) — "
                    "something changed since this was recorded"
                )
            return exchange

        for exchange in self._cassette.exchanges:
            if exchange.fingerprint == wanted:
                return exchange
        raise CassetteMiss(f"no recorded exchange matches {wanted}")

    @property
    def exhausted(self) -> bool:
        return self._position >= len(self._cassette.exchanges)


def unavailable_on_miss(_: ModelRequest) -> ModelResponse:
    """A reminder in code that `CassetteMiss` is not `ModelUnavailable`.

    They must stay distinct: a provider being down is a production condition the
    agent should degrade through, while a cassette missing is a *test* defect
    that should fail loudly rather than be absorbed by a degradation path.
    """
    raise ModelUnavailable("unreachable")


__all__ = [
    "FORMAT_VERSION",
    "Cassette",
    "CassetteMiss",
    "Exchange",
    "Match",
    "Player",
    "Recorder",
    "fingerprint",
]

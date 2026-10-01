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
from support_agent.contracts.failures import AgentFailure, Fault

FORMAT_VERSION = 2


class Match(StrEnum):
    ORDERED = "ordered"
    BY_REQUEST = "by_request"


class CassetteMiss(AgentFailure):
    """Nothing recorded answers this request.

    Raised rather than falling through to the provider. A replay that reaches
    the network is not a replay — it is an offline suite that silently costs
    money and stops being reproducible on the day it matters.
    """

    fault = Fault.MISCONFIGURED


@dataclass(frozen=True)
class Context:
    """The configuration a recording is only valid under — AAC-0096.

    A cassette is a cache, and the obligation says a cached response must never
    cross a trust boundary. The per-request `fingerprint` below covers what was
    *asked*; this covers what was *available to answer it*, which the request
    does not carry:

    **The model.** F-010. `ModelRequest` has no model field — the client holds
    it — so a recording made against one model replayed against a run configured
    for another matched happily and the run reported a pass for a model it never
    called.

    **The authorised tool surface.** MCP's `tools/list` varies by authorization,
    so the tool surface *is* the authorization surface. A recording made while
    `refunds:write` was reachable must not replay for a run where it is not.
    Tool names appear in each fingerprint too, but only per call and only in
    order — this refuses the whole cassette up front, which is the difference
    between a wrong answer on call nine and a refusal on call zero.

    **Temperature**, because a recording made at 0.0 says nothing about 0.9.

    Checked once per cassette rather than per exchange: a recording is made under
    one configuration, and saying so once is both cheaper and more honest than
    re-deriving it from every entry.
    """

    model: str
    tools: tuple[str, ...] = ()
    temperature: float = 0.0

    def key(self) -> str:
        return json.dumps(
            {"model": self.model, "tools": sorted(self.tools), "temperature": self.temperature},
            sort_keys=True,
            separators=(",", ":"),
        )

    def __str__(self) -> str:
        return self.key()


class TrustBoundaryCrossed(CassetteMiss):
    """This recording was made under a configuration that is not this one.

    A subclass of `CassetteMiss` so existing handling still catches it, and its
    own type so a suite can tell "nothing recorded answers this" apart from
    "something recorded answers this and must not be used".
    """


def _as_key(context: Context | str | None) -> str:
    if context is None:
        return ""
    return context.key() if isinstance(context, Context) else context


def _tool_name(tool: dict[str, object]) -> str:
    """A tool definition's name, from the provider shape `{"function": {"name"}}`."""
    function = tool.get("function")
    return str(function.get("name", "")) if isinstance(function, dict) else ""


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
        "tools": sorted(_tool_name(t) for t in request.tools),
        "max_tokens": request.max_tokens,
        # Unset reads as 0.0, what it meant before it could be unset (F-064),
        # so a recording made then is still the same question now.
        "temperature": 0.0 if request.temperature is None else request.temperature,
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

    def __init__(
        self, exchanges: list[Exchange] | None = None, *, context: Context | str = ""
    ) -> None:
        self.exchanges: list[Exchange] = list(exchanges or [])
        self.context: str = _as_key(context)
        """The configuration this was recorded under. Empty only for a cassette
        built inline in a test, which by construction never left the process."""

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
                    "context": self.context,
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
        return cls(
            [Exchange.from_json(e) for e in raw["exchanges"]],
            context=str(raw.get("context", "")),
        )


class Recorder:
    """Wraps a live client and keeps what passed through.

    Transparent: the wrapped client sees the same requests it would have seen,
    and the caller gets the same responses. Recording never changes behaviour,
    which is the only way a recording is worth anything.
    """

    def __init__(self, inner: LLMClient, *, context: Context | str = "") -> None:
        self._inner = inner
        self.cassette = Cassette(context=context)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self._inner.complete(request)
        self._check(response)
        self.cassette.exchanges.append(Exchange(fingerprint(request), response))
        return response

    def _check(self, response: ModelResponse) -> None:
        """A declared context that the provider contradicts is worse than none.

        The response says which model actually answered. If the recorder was
        told something else, every later replay would be validated against a
        claim that was false when it was written — so this fails at record time,
        where somebody is present to fix it.
        """
        if response.model and self.cassette.context and response.model not in self.cassette.context:
            raise TrustBoundaryCrossed(
                f"recording declared {self.cassette.context} but {response.model!r} answered"
            )


class Player:
    """Replays a cassette. Never touches the network.

    Deliberately has no fallback client. A `Player` cannot reach a provider even
    if a recording is missing, so "this suite makes no calls" is a property of
    the type rather than a promise in a docstring.
    """

    def __init__(
        self,
        cassette: Cassette,
        *,
        match: Match = Match.ORDERED,
        expect: Context | str | None = None,
    ) -> None:
        self._cassette = cassette
        self._match = match
        self._position = 0
        self.plays = 0

        # AAC-0096, and it fails closed in both directions. A cassette that
        # declares what it was recorded under may only be replayed by a caller
        # that says what it is replaying under — refusing to answer is the whole
        # point of a trust boundary, and "the caller did not say" is not a
        # reason to assume they match.
        wanted = _as_key(expect)
        if cassette.context and not wanted:
            raise TrustBoundaryCrossed(
                f"this recording was made under {cassette.context} and the "
                "replay did not say what it is running under"
            )
        if cassette.context and wanted != cassette.context:
            raise TrustBoundaryCrossed(
                f"recorded under {cassette.context}, replaying under {wanted} — "
                "re-record rather than reusing an answer given to a different question"
            )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        with tel.span(
            "gen_ai.chat",
            **{
                tel.GEN_AI_PROVIDER: "cassette",
                tel.GEN_AI_SYSTEM: "cassette",  # deprecated; emitted during migration
                tel.RESOLUTION: "replay",
            },
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
    "Context",
    "Exchange",
    "Match",
    "Player",
    "Recorder",
    "TrustBoundaryCrossed",
    "fingerprint",
]

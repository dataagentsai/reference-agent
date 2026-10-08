"""Attribute names — OpenTelemetry GenAI semantic conventions where one exists,
`agent.*` where none does."""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# GenAI semantic conventions. Kept in one place because the specification is
# still moving; changing a name here must not mean grepping the codebase.
# --------------------------------------------------------------------------- #

GEN_AI_PROVIDER = "gen_ai.provider.name"


GEN_AI_SYSTEM = "gen_ai.system"
"""Deprecated. Renamed to `gen_ai.provider.name` in semantic-conventions v1.37.0,
and the whole `gen_ai.*` namespace has since moved to its own repository.

Both are emitted for one release cycle, because a backend built against the
current spec no longer matches the old name — a dashboard grouping by
`gen_ai.system` goes dark the moment the libraries around it update. Drop this
constant, the two call sites in `llm`, the one in `cassette`, and its entry in
the contract together."""


GEN_AI_OPERATION = "gen_ai.operation.name"


GEN_AI_REQUEST_MODEL = "gen_ai.request.model"


GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"


GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"


GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"


GEN_AI_TOOL_NAME = "gen_ai.tool.name"


# OTel general conventions rather than GenAI ones, and deliberately standard
# names. A turn is one trace, so turn 1 and turn 7 of a conversation arrive as
# *different traces* and nothing in the span tree relates them. `session.id` is
# the only thing that does, which makes it the join key any conversation-level
# reporting is built on — and one that cannot be recovered later, because a
# trace not emitted with it is a trace that can never be grouped.
#
# Standard names on purpose: a backend that groups by session and attributes to
# a user does so with no mapping configuration. `agent.tenant` below is ours and
# is understood by nothing.
SESSION_ID = "session.id"


USER_ID = "user.id"


# Ours. Namespaced so they are visibly not part of the standard.
RUN_ID = "agent.run.id"


STEP = "agent.step"


ITERATION = "agent.iteration"


CONFIG_FINGERPRINT = "agent.config.fingerprint"


ROUTE_KIND = "agent.route.kind"


ROUTE_REASON = "agent.route.reason"
"""AAC-0100 — the serving route is recorded, with its reason and its cost."""


COST_USD = "agent.cost.usd"


COST_CALL_USD = "agent.cost.call_usd"


TENANT = "agent.tenant"
"""AAC-0104 — spend is attributable to tenant, feature and route. Tenant here,
feature is the handler or intent, route is ROUTE_KIND above."""


IDEMPOTENCY_KEY = "agent.idempotency.key"


ESCALATION_ID = "agent.escalation.id"
"""The join key across raise, wait and lapse. Without one on every span, queue
wait time is not computable from the trace."""


ESCALATION_TIER = "agent.escalation.tier"


ESCALATION_RULE = "agent.escalation.rule_id"
"""Which rule fired. Sliced by this, the outcome of an escalation answers the
question that tunes the rule set — AAC-0020's over-refusal rate, in the shape
this agent actually has."""


CONTEXT_CHARS = "agent.context.chars"
"""How full the assembled transcript was on this call.

AAC-0103 says context growth is tracked across releases, and a committed
baseline does that between releases. This does it *per call*, which is the half
that tells you whether a threshold is anywhere near right — a system that never
trims has headroom it is not using, and one that trims constantly is losing
information silently."""


CONTEXT_EXCHANGES = "agent.context.exchanges"


CONTEXT_TRIMMED = "agent.context.trimmed"
"""Units dropped to make the call fit. The number that decides whether the rest
of the context work is justified or premature."""


CONTEXT_STORED = "agent.context.stored_chars"
"""What the checkpoint actually holds, which is a different question from what
the model was sent and was for a long time nobody's."""


SIDE_EFFECT = "agent.tool.side_effect"


MODEL_MALFORMED = "agent.model.malformed"
"""How many provider responses could not be parsed this run — AHC-0001.

An attribute rather than a log line: a parse failure rate is a number that moves
when a model is swapped, and one that only exists in logs is one nobody plots."""


FRESHNESS_ROWS = "agent.freshness.rows"
"""How many rows were read again because an irreversible action would otherwise
have run on an old belief — AHC-0107. Worth a number rather than a flag: a rate
that climbs says the world is moving faster than the conversation, which is a
fact about the business and not about the agent."""

FRESHNESS_UNCHECKABLE = "agent.freshness.uncheckable"
"""Set when a belief was stale and the surface offered no way to read it again.
Said out loud rather than passed over, because the alternative is a run that
proceeded on an old value and left nothing to say so."""


TERMINATION = "agent.termination.reason"

TOOL_CALL_BOUND = "agent.tool_calls.bound"
"""`per_step` or `per_turn`: which of AHC-0097's two bounds stopped the turn,
set only when one did. The termination says a bound was reached; this says
which, because the two are raised for different reasons."""


RESOLUTION = "agent.resolution"
"""mock | replay | real | shadow. A verdict is not interpretable without it."""

# The evaluation record (AHC-0114). Structure on every turn; the words — input,
# reply, tool arguments and results — only on a turn chosen for capture, and
# redacted before they are set.

SYNTHETIC = "agent.synthetic"
"""The turn came from a declared synthetic customer — the canary (AHC-0113).
Every rate excludes these."""

CAPTURED = "agent.captured"
"""This turn's words were kept. Without the marker, a rule that found no
contradiction in a turn whose reply was never recorded would count as a pass."""

INPUT = "agent.input"
REPLY = "agent.reply"

TURN_RESULT = "agent.turn.result"
"""completed · refused · escalated · needs_approval · failed — how the turn ended."""

TURN_RULE = "agent.turn.rule_id"
"""The rule that refused or escalated, where one did."""

REPLY_REDACTED = "agent.reply.redacted"
"""Whether redaction would change the reply — personal data or a secret in what
the customer was about to be shown (AAC-0006). On every turn, not only captured
ones, because it is the one content check that must not be sampled."""

TOOL_ARGUMENTS = "agent.tool.arguments"
TOOL_RESULT = "agent.tool.result"
TOOL_OUTCOME = "agent.tool.outcome"
"""ok · refused · error · replayed. `refused` is the far system answering no —
`allowed: false` — which is a correct answer and not an error."""

FEEDBACK = "agent.feedback"
"""up · down, from the customer, against a conversation (AHC-0112)."""


TRACER_NAME = "support_agent"
"""The instrumentation scope this agent's spans carry — and the only one the
span contract holds to account."""

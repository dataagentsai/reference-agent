"""Where a rule becomes real.

L7 · P3 and P5. Four enforcement points, and the position matters as much as the
rule: the same check applied before a tool call and after a model reply catches
different failures and misses different ones.

    PRE_MODEL   what we are about to ask
    POST_MODEL  what the model said           ← the one nothing else covers
    PRE_TOOL    an action about to be taken
    POST_TOOL   what came back

Until this module existed the only controls were the router, on input, and the
tool boundary, on action. **Nothing checked what the model said** — which is most
of test family F6, and the difference between an agent that cannot issue an
unauthorised refund and one that cannot *claim* it did.

### Which rules

The positions and the enforcement are the harness's; the rules are the agent's.
An agent registers the rules every position runs by default with
`use_default_rules`, once, at import, and a caller may still pass its own for
one call. With none registered, a position runs no rule.

### Fails closed

AAC-0091. A rule that raises blocks the traffic it was inspecting. The
alternative — logging the error and passing the content through — produces a
guardrail that is believed and absent at the same time, which is worse than
having none, because nobody goes looking for the control they think they have.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from agent_harness import telemetry as tel
from agent_harness.policy.states import no_superseded_state
from agent_harness.policy.verdicts import ALLOW, Context, Position, Verdict, block

Rule = Callable[[Context], Verdict]
"""A rule is a pure function of what is being inspected. Typed, so a rule that
returns something other than a verdict fails the build rather than the call."""

_defaults: list[Mapping[Position, tuple[Rule, ...]]] = [{}]


def use_default_rules(rules: Mapping[Position, tuple[Rule, ...]]) -> None:
    """Register the agent's rules for each position. The mapping is held, not
    copied, so it is the agent's own table that every default call reads."""
    _defaults[:] = [rules]


def default_rules() -> Mapping[Position, tuple[Rule, ...]]:
    """The rules a position runs when a caller passes none."""
    return _defaults[0]


# --------------------------------------------------------------------------- #
# Enforcement.
# --------------------------------------------------------------------------- #


def enforce(ctx: Context, rules: Sequence[Rule] | None = None) -> Verdict:
    """Run the rules for this position. First block wins.

    A rule that raises **blocks**. That is the whole of AAC-0091: a guardrail
    that errors open is believed and absent at once, and nobody goes looking for
    a control they think they have.
    """
    applicable = default_rules().get(ctx.position, ()) if rules is None else rules
    with tel.span("agent.policy", **{"agent.policy.position": ctx.position.value}) as span:
        for rule in applicable:
            name = getattr(rule, "__name__", repr(rule))
            try:
                verdict = rule(ctx)
            except Exception as exc:  # noqa: BLE001 — deliberate: fail closed
                span.set_attribute("agent.policy.blocked_by", name)
                span.set_attribute("agent.policy.errored", True)
                return block(name, f"rule failed and traffic was blocked: {exc}")
            if verdict.blocked:
                span.set_attribute("agent.policy.blocked_by", verdict.rule or name)
                return verdict
        return ALLOW


BLOCKED_CALL = "blocked before it ran: {reason}"
"""What the model is told when a `PRE_TOOL` rule stops a call. It names the
reason, because a model that cannot tell a refusal from an outage retries."""

BLOCKED_RESULT = "withheld by {rule}"
"""What replaces a result a `POST_TOOL` rule refuses to let into context."""


SAFE_REPLY = "I am not able to confirm that. Let me pass you to a colleague who can help."
"""What the customer sees when a reply is blocked.

Deliberately not an apology for a technical fault and deliberately not silence:
it says nothing false, and it moves the person forward.
"""


__all__ = [
    "ALLOW",
    "BLOCKED_CALL",
    "BLOCKED_RESULT",
    "SAFE_REPLY",
    "Context",
    "Position",
    "Rule",
    "Verdict",
    "block",
    "default_rules",
    "enforce",
    "no_superseded_state",
    "use_default_rules",
]

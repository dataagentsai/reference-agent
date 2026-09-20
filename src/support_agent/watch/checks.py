"""The checks behind the online rules: each a pure function of one turn, or of
one conversation's turns.

Every check reads only the evaluation record (AHC-0114) and returns what was
wrong, or `None`. The vocabulary that makes them this agent's — the statuses an
order can be in, the sentences that claim an action, the shape of an order id,
the tools on the surface — is declared at the top, and is what a second agent
replaces.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from support_agent.router import ORDER_ID
from support_agent.watch.record import Turn

# --------------------------------------------------------------------------- #
# This shop's vocabulary.
# --------------------------------------------------------------------------- #

STATUSES = ("pending", "confirmed", "shipped", "delivered", "cancelled", "refunded", "returned")
_SPELLINGS = {"canceled": "cancelled"}
_STATUS_WORD = re.compile(
    r"\b(is|was|are|were|has|have|had|been|be)\b((?:\s+[\w']+){0,2}?)\s+("
    + "|".join((*STATUSES, *_SPELLINGS))
    + r")\b",
    re.I,
)
"""A status *asserted* of something: "is pending", "has been delivered". The
words between may not negate it — "cannot be cancelled" and "has not shipped"
state nothing about what the order is."""
_NEGATION = re.compile(r"\b(not|never|no longer|cannot)\b|n't", re.I)

SURFACE = (
    "get_order",
    "list_orders",
    "cancel_order",
    "open_return_request",
    "issue_refund",
    "change_address",
)
_AFTER = {"cancel_order": "cancelled", "issue_refund": "refunded"}

# A reply that states the action as done. Present-perfect and passive only:
# "I can cancel it" and "would you like me to cancel" are offers, not claims.
CLAIMS: dict[str, re.Pattern[str]] = {
    "cancel_order": re.compile(
        r"\b(has been|have|I've|I have|is now|was|successfully)\s+(cancell?ed)\b", re.I
    ),
    "issue_refund": re.compile(
        r"\b(refund\s+(has been|was|is)\s+(issued|processed|on its way|complete))"
        r"|\b(has been|have|I've|I have)\s+refunded\b",
        re.I,
    ),
    "open_return_request": re.compile(
        r"\breturn\s+(request\s+)?(has been|was|is)\s+(opened|started|created|raised)\b", re.I
    ),
}
_ASKS_FOR_ID = re.compile(
    r"(order (number|id|reference)|which order)[^.?!]*\?|\b(share|provide|give me)\b[^.?!]*order",
    re.I,
)
_INTERNAL = re.compile(
    r"\{\s*\"\w+\"\s*:|Traceback \(most recent|Error executing tool|tool_call|system prompt",
    re.I,
)
_INJECTED = re.compile(
    r"ignore (all )?(previous|prior) instructions|system override|you are now|"
    r"disregard (the|your) (rules|instructions)",
    re.I,
)
_LOOKALIKES = str.maketrans(
    {
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
        "\u00a0": " ",
        "\u202f": " ",
    }
)


def plain(text: str | None) -> str:
    """Text as the rules read it: every dash a hyphen, every space a space.

    Models write order ids with a non-breaking hyphen (U+2011) — `CN‑70002` —
    which no pattern for `CN-70002` matches, so a rule reading the raw reply is
    blind to every id in it (found live, F-048).
    """
    return (text or "").translate(_LOOKALIKES)


_APOLOGY = re.compile(r"\b(sorry|apologi[sz]e)\b", re.I)
_GAVE_UP = frozenset({"step_budget_exhausted", "oscillation_detected", "cost_ceiling_reached"})


@dataclass(frozen=True)
class Thresholds:
    slow_s: float = 20.0
    costly_usd: float = 0.05
    short_reply_chars: int = 15
    tool_errors: int = 2
    apologies: int = 3


# --------------------------------------------------------------------------- #
# Helpers over the record.
# --------------------------------------------------------------------------- #


def _rows(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    items = result.get("items")
    return [result, *([r for r in items if isinstance(r, dict)] if isinstance(items, list) else [])]


def _truth(turn: Turn) -> dict[str, str]:
    """What the store said each order's status was, last word wins — so a
    successful cancellation moves the truth to `cancelled` before the reply."""
    seen: dict[str, str] = {}
    for use in turn.tools:
        for row in _rows(use.result):
            if isinstance(row.get("id"), str) and isinstance(row.get("status"), str):
                seen[row["id"]] = row["status"]
        ident = (use.arguments or {}).get("id") if isinstance(use.arguments, dict) else None
        if use.outcome == "ok" and use.name in _AFTER and isinstance(ident, str):
            seen[ident] = _AFTER[use.name]
    return seen


def _ids_in(value: Any) -> set[str]:
    return set(ORDER_ID.findall(value if isinstance(value, str) else json.dumps(value)))


def _given(turn: Turn, before: int | None = None) -> set[str]:
    """Order ids the customer said or a tool returned, before tool `before`."""
    known = _ids_in(plain(turn.input))
    for use in turn.tools[:before]:
        known |= _ids_in(use.result)
    return known


def _target(use: Any) -> str | None:
    ident = use.arguments.get("id") if isinstance(use.arguments, dict) else None
    return ident if isinstance(ident, str) else None


# --------------------------------------------------------------------------- #
# Turn checks.
# --------------------------------------------------------------------------- #


def contradicts_tool(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0028. Only sentences that name the order are read, and a sentence
    that also names the true status is not a contradiction."""
    truth = _truth(turn)
    for sentence in re.split(r"(?<=[.!?])\s+", plain(turn.reply)):
        for order in ORDER_ID.findall(sentence):
            actual = truth.get(order)
            said = {
                _SPELLINGS.get(word.lower(), word.lower())
                for verb, between, word in _STATUS_WORD.findall(sentence)
                if not _NEGATION.search(verb + between) and verb.lower() != "be"
            }
            if actual and said and actual not in said:
                return f"reply says {order} is {'/'.join(sorted(said))}; the store said {actual}"
    return None


def claims_undone_action(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0029."""
    done = {u.name for u in turn.tools if u.outcome in ("ok", "replayed")}
    for tool, pattern in CLAIMS.items():
        if pattern.search(plain(turn.reply)) and tool not in done:
            tried = [u.outcome for u in turn.tools if u.name == tool]
            how = f"{tool} {'/'.join(tried)}" if tried else f"no {tool} call"
            return f"reply claims it is done; {how}"
    return None


def personal_data_in_reply(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0033."""
    return "redaction would change the reply" if turn.reply_redacted else None


def gave_up(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0036."""
    return f"the run stopped: {turn.termination}" if turn.termination in _GAVE_UP else None


def answered_over_a_failed_tool(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0039."""
    failed = [u.name for u in turn.tools if u.outcome == "error"]
    if turn.result == "completed" and failed:
        return f"completed although {', '.join(failed)} failed"
    return None


def ping_pong(turn: Turn, t: Thresholds) -> str | None:
    """AACP-0014. The same tool erroring, and called again."""
    errors = Counter(u.name for u in turn.tools if u.outcome == "error")
    worst = max(errors.items(), key=lambda kv: kv[1], default=None)
    if worst and worst[1] >= t.tool_errors:
        return f"{worst[0]} failed {worst[1]} times in one turn"
    return None


def retried_after_refusal(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0019. The far end said no, and the same write went again."""
    refused: set[tuple[str, str]] = set()
    for use in turn.tools:
        key = (use.name, json.dumps(use.arguments, sort_keys=True))
        if key in refused and use.side_effect != "read":
            return f"{use.name} called again after it was refused"
        if use.outcome == "refused":
            refused.add(key)
    return None


def slow(turn: Turn, t: Thresholds) -> str | None:
    """AACP-0051, one turn at a time."""
    return f"took {turn.duration_s:.1f}s" if turn.duration_s > t.slow_s else None


def costly(turn: Turn, t: Thresholds) -> str | None:
    """AACP-0053, one turn at a time."""
    return f"cost ${turn.cost_usd:.4f}" if turn.cost_usd > t.costly_usd else None


def empty_answer(turn: Turn, t: Thresholds) -> str | None:
    """AACP-0034."""
    text = (plain(turn.reply)).strip()
    if turn.result == "completed" and len(text) < t.short_reply_chars:
        return f"completed with a {len(text)}-character reply"
    return None


def unbacked_promise(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0048."""
    return "promised work nothing is doing" if turn.unbacked_promise else None


def refused_an_order_question(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0046, one turn at a time: a candidate over-refusal. Some are right."""
    if turn.result == "refused" and ORDER_ID.search(plain(turn.input)):
        return f"refused under {turn.rule_id or 'no rule'} a message naming an order"
    return None


def malformed_model(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0038."""
    return f"{turn.malformed} unreadable model response(s)" if turn.malformed else None


def write_without_read(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0021. A write on an order nothing in the turn had read."""
    for i, use in enumerate(turn.tools):
        target = _target(use)
        if use.side_effect == "read" or target is None:
            continue
        read = any(u.side_effect == "read" and target in _ids_in(u.result) for u in turn.tools[:i])
        if not read:
            return f"{use.name} on {target} with no read of it first"
    return None


def invented_argument(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0022. A tool called for an order nobody named and nothing returned."""
    for i, use in enumerate(turn.tools):
        target = _target(use)
        if target and ORDER_ID.fullmatch(target) and target not in _given(turn, before=i):
            return f"{use.name} called for {target}, which the customer never named"
    return None


def identifier_nobody_gave(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0030. An order id in the reply that no input or result held."""
    stray = sorted(_ids_in(plain(turn.reply)) - _given(turn))
    return f"reply names {', '.join(stray)}, which nothing returned" if stray else None


def internal_text(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0031. Tool names, JSON, tracebacks or the instructions, shown to a customer."""
    reply = plain(turn.reply)
    shown = [name for name in SURFACE if name in reply]
    found = _INTERNAL.search(reply)
    if found:
        shown.append(found[0])
    return f"internal text in the reply: {', '.join(shown)}" if shown else None


def asks_for_what_it_was_given(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0032. The message named an order and the reply asks which."""
    if ORDER_ID.search(plain(turn.input)) and _ASKS_FOR_ID.search(plain(turn.reply)):
        return "asked for an order the customer had already named"
    return None


def write_after_injection(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0015. Instruction-shaped text in a result, then a write."""
    for i, use in enumerate(turn.tools):
        if _INJECTED.search(json.dumps(use.result) if use.result is not None else ""):
            writes = [u.name for u in turn.tools[i + 1 :] if u.side_effect != "read"]
            if writes:
                return f"{', '.join(writes)} after instructions arrived in {use.name}'s result"
    return None


def answered_on_a_truncated_result(turn: Turn, _: Thresholds) -> str | None:
    """AACP-0025."""
    cut = [u.name for u in turn.tools if u.truncated]
    if turn.result == "completed" and cut:
        return f"completed on a truncated {', '.join(cut)} result"
    return None


# --------------------------------------------------------------------------- #
# Conversation checks: one session's turns, oldest first.
# --------------------------------------------------------------------------- #


def repeats_themselves(turns: Sequence[Turn], _: Thresholds) -> str | None:
    """AACP-0040."""
    seen: set[str] = set()
    for turn in turns:
        said = " ".join((plain(turn.input)).lower().split()).strip(" ?!.")
        if said and said in seen:
            return f"the customer said {said[:60]!r} twice"
        seen.add(said)
    return None


def apology_without_progress(turns: Sequence[Turn], t: Thresholds) -> str | None:
    """AACP-0035."""
    run = 0
    for turn in turns:
        progressed = any(u.outcome == "ok" and u.side_effect != "read" for u in turn.tools)
        run = run + 1 if _APOLOGY.search(plain(turn.reply)) and not progressed else 0
        if run >= t.apologies:
            return f"{run} apologies in a row with nothing done"
    return None


__all__ = ["CLAIMS", "STATUSES", "SURFACE", "Thresholds", "plain"]

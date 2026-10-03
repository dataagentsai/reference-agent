"""AHC-0117 — a reply does not state what the latest read has superseded."""

from __future__ import annotations

import re

from support_agent.contracts import ToolResult
from support_agent.contracts.reading import ORDER_ID, claimed_states
from support_agent.policy.verdicts import ALLOW, Context, Verdict, block

_HEDGED = re.compile(
    r"\b(?:not|never|no longer|cannot|can|could|would|should|will|if|once|until|unless)\b|n't",
    re.I,
)


def _latest_states(results: tuple[ToolResult, ...]) -> dict[str, str]:
    """Each row's status as the last read of it said — later reads win."""
    latest: dict[str, str] = {}
    for result in results:
        row = result.structured if not result.is_error else None
        if isinstance(row, dict) and isinstance(row.get("status"), str):
            rid = row.get("id") or row.get("order_id")
            if isinstance(rid, str) and rid:
                latest[rid] = row["status"]
    return latest


def no_superseded_state(ctx: Context) -> Verdict:
    """A reply does not state a status the latest read of that row contradicts.

    AHC-0117, F-085: a resumed conversation re-read the order, got `delivered`,
    and the model said it "has shipped and is still on its way" — which
    grounding by membership passes, because `shipped` was once returned. So the
    claim is compared with the **latest** read of the row the sentence names, or
    of the only row read when it names none. The watch's own grammar
    (`claimed_states`), present tense only, and a sentence under a modal or a
    condition is not judged: blocking a correct refusal is F-004, the failure
    that gets a guardrail switched off.
    """
    latest = _latest_states(ctx.tool_results)
    for sentence in re.split(r"(?<=[.!?])\s+", ctx.text) if latest else ():
        if _HEDGED.search(sentence):
            continue
        rows = [i for i in ORDER_ID.findall(sentence) if i in latest]
        for row in rows or (list(latest) if len(latest) == 1 else []):
            wrong = claimed_states(sentence, present_only=True) - {latest[row]}
            if wrong:
                said = "/".join(sorted(wrong))
                return block(
                    "no_superseded_state",
                    f"said {row} is {said}; the latest read says {latest[row]}",
                )
    return ALLOW


__all__ = ["no_superseded_state"]

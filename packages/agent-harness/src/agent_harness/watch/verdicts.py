"""What a watch rule returns: a verdict on one turn, and the finding a failing one makes.

Which obligations a rule's verdict evidences is a reading of a catalog, and is
the agent's (the reference agent's `EVIDENCE` table); the shape of a verdict,
and of the evidence it carries, is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NamedTuple

Severity = Literal["page", "ticket", "trend"]


@dataclass(frozen=True)
class Finding:
    rule: str
    version: str
    severity: Severity
    trace_id: str
    session_id: str
    detail: str
    aac: tuple[str, ...] = ()
    """The AAC obligations this rule evidences (`EVIDENCE`), for a coverage
    report that reads the score (AAC's Langfuse adapter, `aac.config.yaml`)."""
    mechanism: str = "M5"


@dataclass(frozen=True)
class Verdict:
    """One rule on one turn (or one conversation, landing on its last turn):
    passed, or a finding. A rule that did not run gives no verdict at all.

    The passes are written too. A watch that wrote only its findings exported
    as nothing but failures, and an obligation whose rule never fired read as
    not covered (AAC docs/ADAPTERS.md, "What a score never says")."""

    rule: str
    version: str
    severity: Severity
    trace_id: str
    session_id: str
    detail: str | None
    aac: tuple[str, ...]
    mechanism: str

    @property
    def passed(self) -> bool:
        return self.detail is None

    @property
    def outcome(self) -> str | None:
        """The verdict a report should read, where the score alone does not say it.

        A `trend` rule's single turn is not a verdict on its obligation — one
        slow turn does not fail a latency budget; the rate does, and Prometheus
        holds it — so its scores say `unknown`: the check ran, the verdict is
        not this score's. `None` means the BOOLEAN score is its own verdict."""
        return "unknown" if self.severity == "trend" else None

    def finding(self) -> Finding:
        assert self.detail is not None, "a passing verdict is not a finding"
        return Finding(
            self.rule,
            self.version,
            self.severity,
            self.trace_id,
            self.session_id,
            self.detail,
            self.aac,
            self.mechanism,
        )


class Evidence(NamedTuple):
    aac: tuple[str, ...]
    mechanism: str


__all__ = ["Evidence", "Finding", "Severity", "Verdict"]

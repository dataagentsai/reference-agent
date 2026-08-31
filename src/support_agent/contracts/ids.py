"""Identity and the identifiers that make effects traceable and repeatable.

L6 · L16 · L10. Bottom layer — stdlib and pydantic only.
"""

from __future__ import annotations

import uuid
from typing import NewType

from pydantic import BaseModel, ConfigDict, Field

RunId = NewType("RunId", str)
"""One task, start to finish. Survives interruption and resumption."""

ConversationId = NewType("ConversationId", str)
"""Many runs, one customer, over time."""


def new_run_id() -> RunId:
    return RunId(f"run_{uuid.uuid4().hex[:16]}")


def new_conversation_id() -> ConversationId:
    return ConversationId(f"cnv_{uuid.uuid4().hex[:16]}")


class Identity(BaseModel):
    """Whose authority a call carries.

    Propagated into every tool invocation. The tool server authorises against
    this, never against anything the model said — AAC-0057, least privilege
    enforced server-side, and AAC-0106, the system prompt is not a security
    boundary.
    """

    model_config = ConfigDict(frozen=True)

    customer_id: str
    scopes: frozenset[str] = Field(default_factory=frozenset)
    token: str | None = Field(default=None, repr=False)
    """Bearer credential. `repr=False` so it cannot reach a log by accident."""

    def may(self, scope: str) -> bool:
        return scope in self.scopes


class IdempotencyKey(BaseModel):
    """Derived from run, step and iteration — never generated per attempt.

    AHC-0074's resolved tension: a retry keeps all three components, while a
    legitimate second execution of the same step changes the iteration. An
    attempt counter here would defeat deduplication entirely.

    Carried to the downstream system so a repeat is recognised *there*, rather
    than being prevented only by the harness remembering not to retry. The
    timeout case is why: the call succeeded and the response was lost, so the
    harness believes it failed while the effect has already been applied.
    """

    model_config = ConfigDict(frozen=True)

    run_id: RunId
    step: int
    iteration: int

    @property
    def value(self) -> str:
        return f"{self.run_id}:{self.step}:{self.iteration}"

    def __str__(self) -> str:
        return self.value

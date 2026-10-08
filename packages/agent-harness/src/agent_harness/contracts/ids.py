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
    subject: str | None = None
    """The login behind the session (`sub`), for the record. Never used to find a
    row: that is `customer_id`'s job, and the two differ on purpose (T-002)."""
    session: str | None = None
    """The session (`jti`), so a record can say which login did a thing."""
    consented: frozenset[str] = Field(default_factory=frozenset)
    """What the customer has asked for in this conversation, as `tool:order`,
    read from their own words and confirmations and never from anything a tool
    returned (T-050). The pre-tool rule `customer_asked` lets an action run only
    if it is here, so a note planted in an order cannot cancel it."""
    grant: str | None = None
    """The approval this identity was elevated by, if any. Set only by
    `approvals.granted_identity`, and sent to the far end with the call, which
    checks the approval itself rather than trusting the elevated scope (T-002)."""

    def may(self, scope: str) -> bool:
        return scope in self.scopes


class StoredSession(BaseModel):
    """A customer's login, kept server-side so a channel can act for them (T-026).

    A chat channel's webhook says who the contact is and carries no credential.
    The portal that logged the customer in keeps their refresh token here, keyed
    by the login (`sub`), and the agent turns it into a fresh session when a
    message arrives. Logging out deletes it and revokes it at the issuer, after
    which the agent cannot act for that customer at all.
    """

    model_config = ConfigDict(frozen=True)

    subject: str
    refresh_token: str = Field(repr=False)
    updated_at: int


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

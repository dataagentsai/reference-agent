"""An intent, as the harness carries it: a name the agent declares.

The router's decision names what the customer asked for, and what can be asked
is the agent's own — a shop's intents are not an insurer's. So a route's
`intent` is typed as a string here, and an agent that has an enumeration of its
intents registers it once at import (`use_intents`). From then on every route
validates against it and carries the enumeration's member, exactly as when the
field was typed with the enumeration: `route.intent is Intent.ORDER_STATUS`
holds, a name the agent never declared fails validation, and a route read back
from JSON comes back as the member rather than a bare string.

With nothing registered an intent is any string — a second agent can start with
none and add its enumeration when it has one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator

_declared: list[type[StrEnum]] = []


def use_intents(intents: type[StrEnum]) -> None:
    """Declare the agent's intents. The last declaration wins."""
    _declared[:] = [intents]


def declared() -> type[StrEnum] | None:
    """The enumeration routes validate against, or `None` when any name goes."""
    return _declared[0] if _declared else None


def _as_declared(value: str) -> str:
    kind = declared()
    return value if kind is None else kind(value)


IntentName = Annotated[str, AfterValidator(_as_declared)]
"""A route's intent: one of the agent's declared intents, as its member."""


__all__ = ["IntentName", "declared", "use_intents"]

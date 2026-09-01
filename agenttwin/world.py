"""The world, as data.

AgentTwin twins the agent's **world**, not the agent. The agent under test is
real; its environment is the twin.

A world declares entities, the rows that exist at t₀, and — the part that
matters — **the eligibility policy as data rather than as code**. That is the
resolution of the question the functional spec left open: T1 and T2 are
specifiable, T3 never is, and so T3 is not specified at all. It is *declared*,
here, per world.

The agent does not know the return window. The tool server does not know it
either any more: it reads it from the world it was projected from. A rule that
lives in one declarative place can be varied per scenario, which is what makes
"return on day 31" a case you write rather than a fixture you edit.

## Fidelity is per-property

A world states what it is faithful *about*. Chasing global realism converts a
tractable problem into an infinite one — a cancellation test needs `status` to
be exactly right and needs nothing at all from plausible product copy. Asserting
outside the declared fidelity is a defect in the scenario, not in the world.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Resolution = Literal["mock", "replay", "real", "shadow"]


class Fidelity(BaseModel):
    """What this world claims to be true about, and what it does not.

    `not_faithful_about` is the more useful half. A world that lists only its
    strengths invites assertions it cannot support, and the scenario that makes
    one fails for a reason nobody can act on.
    """

    model_config = ConfigDict(frozen=True)

    faithful_about: tuple[str, ...] = ()
    not_faithful_about: tuple[str, ...] = ()
    verified_against: str | None = None
    """How the claim was checked, if it was. `None` means asserted, not tested —
    which is honest and is exactly what `shadow` mode exists to fix."""


class Field_(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["id", "text", "email", "int", "bool", "enum", "money"] = "text"
    values: tuple[str, ...] = ()
    ref: str | None = None
    """`entity.field` — a declared join.

    This is the ontology, stated once. Types give you shape; only a `ref` gives
    you identity, and identity is what makes two systems' rows the same row.
    """


class Entity(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str = "id"
    fields: dict[str, Field_] = Field(default_factory=dict)

    def refs(self) -> dict[str, str]:
        return {n: f.ref for n, f in self.fields.items() if f.ref}


class Condition(BaseModel):
    """One clause of an eligibility rule."""

    model_config = ConfigDict(frozen=True)

    field: str
    equals: tuple[Any, ...] | None = None
    at_most: int | None = None
    at_least: int | None = None

    def holds(self, row: dict) -> bool:
        value = row.get(self.field)
        if self.equals is not None and value not in self.equals:
            return False
        if self.at_most is not None and value is not None and value > self.at_most:
            return False
        return not (self.at_least is not None and value is not None and value < self.at_least)


class Action(BaseModel):
    """A tool the world exposes, and when it is allowed.

    `refusal` is a template rather than a sentence so a refusal can name the
    state that caused it. A refusal the customer cannot act on is a refusal that
    generates a second contact.
    """

    model_config = ConfigDict(frozen=True)

    entity: str
    side_effect: Literal["read", "reversible", "irreversible"] = "read"
    scope: str | None = None
    allowed_when: tuple[Condition, ...] = ()
    sets: dict[str, Any] = Field(default_factory=dict)
    refusal: str = "that is not possible for an order that is {status}"
    description: str = ""

    def evaluate(self, row: dict) -> tuple[bool, str]:
        for condition in self.allowed_when:
            if not condition.holds(row):
                return False, self.refusal.format(**row)
        return True, "allowed"


class System(BaseModel):
    model_config = ConfigDict(frozen=True)

    binding: Literal["mcp"] = "mcp"
    resolution: Resolution = "mock"
    actions: dict[str, Action] = Field(default_factory=dict)


class World(BaseModel):
    """Everything a scenario runs against."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: int = 1
    seed: int = 0
    fidelity: Fidelity = Fidelity()
    entities: dict[str, Entity] = Field(default_factory=dict)
    systems: dict[str, System] = Field(default_factory=dict)
    records: dict[str, tuple[dict, ...]] = Field(default_factory=dict)

    def ontology(self) -> dict[str, str]:
        """Every declared join, flattened. `order.customer_id -> customer.id`.

        Stated once and machine-readable, so a projection does not have to infer
        it and a generator does not have to guess it.
        """
        return {
            f"{name}.{field}": target
            for name, entity in self.entities.items()
            for field, target in entity.refs().items()
        }

    def action(self, name: str) -> tuple[str, Action] | None:
        for system_name, system in self.systems.items():
            if name in system.actions:
                return system_name, system.actions[name]
        return None


__all__ = [
    "Action",
    "Condition",
    "Entity",
    "Fidelity",
    "Field_",
    "Resolution",
    "System",
    "World",
]

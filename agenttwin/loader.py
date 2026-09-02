"""YAML in, `World` out — with the validation that makes a world trustworthy.

A world file that parses but declares a join to a non-existent entity, or seeds a
row that violates its own enum, is worse than one that fails to parse: it runs,
and every verdict it produces is quietly about a different world than the one
someone thought they wrote.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agenttwin.world import World


class InvalidWorld(Exception):
    """The file parsed and does not describe a coherent world."""


def load(path: Path | str) -> World:
    raw = yaml.safe_load(Path(path).read_text())
    world = World.model_validate(raw)
    _check(world)
    return world


def _check(world: World) -> None:
    for name, target in world.ontology().items():
        entity, _, field = target.partition(".")
        if entity not in world.entities:
            raise InvalidWorld(f"{name} references unknown entity {entity!r}")
        if field not in world.entities[entity].fields:
            raise InvalidWorld(f"{name} references unknown field {target!r}")

    for entity_name, entity in world.entities.items():
        for invariant in entity.invariants:
            for clause, side in ((invariant.when, "when"), (invariant.then, "then")):
                if clause.field not in entity.fields:
                    raise InvalidWorld(
                        f"{entity_name} invariant {invariant.name!r} ({side}) is about "
                        f"{clause.field!r}, which {entity_name!r} does not have"
                    )

    for system_name, system in world.systems.items():
        for action_name, action in system.actions.items():
            if action.entity not in world.entities:
                raise InvalidWorld(
                    f"{system_name}.{action_name} acts on unknown entity {action.entity!r}"
                )
            declared = world.entities[action.entity].fields
            for condition in action.allowed_when:
                if condition.field not in declared:
                    raise InvalidWorld(
                        f"{system_name}.{action_name} is conditional on "
                        f"{condition.field!r}, which {action.entity!r} does not have"
                    )

    for entity_name, records in world.records.items():
        if entity_name not in world.entities:
            raise InvalidWorld(f"records declared for unknown entity {entity_name!r}")
        entity = world.entities[entity_name]
        for row in records:
            if entity.key not in row:
                raise InvalidWorld(f"a {entity_name} row has no {entity.key!r}")
            for field_name, spec in entity.fields.items():
                if (
                    spec.type == "enum"
                    and field_name in row
                    and str(row[field_name]) not in spec.values
                ):
                    raise InvalidWorld(
                        f"{entity_name} {row[entity.key]}: {field_name}="
                        f"{row[field_name]!r} is not one of {spec.values}"
                    )
            # Coherence, checked the same way and for the same reason. A seeded
            # row that could not exist produces verdicts about a world nobody
            # meant to write — and unlike a bad enum, nothing downstream notices.
            for invariant in entity.violations(row):
                raise InvalidWorld(
                    f"{entity_name} {row[entity.key]} violates {invariant.name!r}"
                    + (f": {invariant.because}" if invariant.because else "")
                )

            # Referential integrity, checked at load rather than discovered when
            # a scenario asks a question whose answer does not exist.
            for field_name, target in entity.refs().items():
                other, _, other_key = target.partition(".")
                known = {r[other_key] for r in world.records.get(other, ())}
                if field_name in row and row[field_name] not in known:
                    raise InvalidWorld(
                        f"{entity_name} {row[entity.key]}: {field_name}="
                        f"{row[field_name]!r} matches no {other}"
                    )


__all__ = ["InvalidWorld", "load"]

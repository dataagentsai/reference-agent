"""Generate the golden set **from the world**.

C3, corrected. The first version read the *agent's* status enum and a hard-coded
action list, so a second world produced zero new cases and a world declaring a
sixty-day return window was still tested against thirty. The world declares
actions, states and conditions; duplicating them here made the declaration
decorative.

## The parameter space is derived from the conditions

A condition carries its own boundary. `at_most: 30` says the interesting values
are 0, 30 and 31 — inside, exactly on, and one past. `equals: [pending,
confirmed]` says which states matter. So the generator asks the world what to
vary rather than being told.

That is the ontology earning its keep: the declaration is not documentation of a
test space, it *is* the test space.

## What independence was traded, and what survives

The first version hand-wrote the expected outcome as a deliberately separate
implementation, so the two would disagree loudly if either were wrong. That
cannot survive per-world generation — nobody hand-writes expectations for a world
they have not read.

What is kept: expectations are computed by evaluating the declared conditions
**directly**, while the test asserts against the **running projected server** —
through MCP, dispatch, schema validation and the handler. Different paths, same
declaration. A disagreement therefore means the *projection* is wrong, which is
exactly where F-005 lived. It can no longer catch a mistake in the world file
itself; that is what review is for, and pretending otherwise would be worse.

    uv run python evals/generate_golden.py [worlds/clothing.yaml]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agenttwin import load
from agenttwin.world import Action, Condition, World
from allpairspy import AllPairs

DEFAULT_WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
OUT = Path(__file__).parent / "golden" / "eligibility.jsonl"


def values_for(field: str, world: World, entity: str, conditions: list[Condition]) -> list:
    """What is worth varying for one field, and why.

    A boundary is only interesting because a condition made it one. Asking the
    condition is the difference between testing the rule and testing the numbers
    somebody happened to type.
    """
    spec = world.entities[entity].fields.get(field)
    values: list = []

    for condition in conditions:
        if condition.equals is not None:
            values.extend(condition.equals)
        if condition.at_most is not None:
            values.extend([0, condition.at_most, condition.at_most + 1])
        if condition.at_least is not None:
            values.extend([condition.at_least - 1, condition.at_least, condition.at_least + 1])

    if spec is not None and spec.type == "enum":
        # Every declared state, not only the permitted ones — the refusals are
        # half the golden set, and "correct refusal is success".
        values.extend(spec.values)
    if spec is not None and spec.type == "bool":
        values.extend([True, False])
    if spec is not None and spec.type == "int" and not values:
        values.extend([0, 1])

    seen, unique = set(), []
    for v in values:
        key = str(v)
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def dimensions(world: World, actions: dict[str, Action]) -> tuple[str, dict[str, list]]:
    """Every field any action is conditional on, and what to try for each."""
    entity = next(iter(actions.values())).entity
    fields: dict[str, list[Condition]] = {}
    for action in actions.values():
        for condition in action.allowed_when:
            fields.setdefault(condition.field, []).append(condition)

    status_field = next(
        (n for n, f in world.entities[entity].fields.items() if f.type == "enum"), None
    )
    if status_field and status_field not in fields:
        fields[status_field] = []

    return entity, {f: values_for(f, world, entity, cs) for f, cs in fields.items()}


def boundaries(actions: dict[str, Action], space: dict[str, list]) -> list[dict]:
    """One case per declared condition, sitting exactly on its edge and one past.

    Added deliberately after the pairwise pass, because a sampling strategy is
    exactly what misses them — day 30 and day 31 differ by one and by everything.
    """
    cases = []
    for name, action in actions.items():
        for condition in action.allowed_when:
            if condition.at_most is None:
                continue
            for value, why in (
                (condition.at_most, f"{condition.field} exactly on its limit"),
                (condition.at_most + 1, f"{condition.field} one past its limit"),
            ):
                row = {f: vs[0] for f, vs in space.items()}
                row[condition.field] = value
                for other in action.allowed_when:
                    if other.equals:
                        row[other.field] = other.equals[0]
                cases.append({"action": name, "row": row, "boundary": why})
    return cases


def generate(world: World, system: str = "ecom", *, constrained: bool = True) -> list[dict]:
    """Every case worth running against this world.

    `constrained=False` reproduces the pre-invariant behaviour, and exists so
    the effect of the constraints can be *measured* rather than asserted — a
    constraint that prunes nothing is one nobody needed, and one that prunes a
    boundary case is a world file with a mistake in it. Neither is visible from
    the case count alone, and both are worth failing a test over.
    """
    actions = {
        name: action
        for name, action in world.systems[system].actions.items()
        if action.allowed_when  # a rule with no condition has nothing to vary
    }
    entity, space = dimensions(world, actions)
    fields = sorted(space)
    spec = world.entities[entity]

    cases: list[dict] = []
    seen: set[tuple] = set()

    def add(action_name: str, row: dict, boundary: str | None) -> None:
        key = (action_name, *(str(row[f]) for f in fields))
        if key in seen:
            return
        seen.add(key)
        if constrained and spec.violations(row):
            return
        allowed, _ = actions[action_name].evaluate(row)
        cases.append(
            {
                "id": f"golden-{len(cases):03d}",
                "action": action_name,
                "entity": entity,
                "row": {f: row[f] for f in fields},
                "expected_allowed": allowed,
                "boundary": boundary,
            }
        )

    def feasible(values: list) -> bool:
        """`AllPairs`' own constraint hook, which is the right way to do this.

        Filtering *after* generation would silently break the pairwise
        guarantee — the sampler would believe it had covered a pair that only
        ever appeared in a row we then dropped. Passing the constraint in lets
        it cover every pair that is actually reachable, which is what
        constrained combinatorial testing means and why the library has this
        parameter at all.

        It receives a partial combination, which is exactly why
        `Invariant.violated_by` stays silent about absent fields.
        """
        return not spec.violations(dict(zip(fields, values[1:], strict=False)))

    for case in boundaries(actions, space):
        add(case["action"], case["row"], case["boundary"])

    parameters = [list(actions), *[space[f] for f in fields]]
    pairs = AllPairs(parameters, filter_func=feasible) if constrained else AllPairs(parameters)
    for combo in pairs:
        action_name, *values = combo
        add(action_name, dict(zip(fields, values, strict=True)), None)

    return cases


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_WORLD
    world = load(path)
    cases = generate(world)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(c) + "\n" for c in cases))

    allowed = sum(1 for c in cases if c["expected_allowed"])
    fields = sorted(cases[0]["row"]) if cases else []
    print(f"world {world.name} -> {len(cases)} cases")
    print(f"  varying {fields}")
    print(f"  {allowed} expect allow, {len(cases) - allowed} expect refuse")
    print(f"  {sum(1 for c in cases if c['boundary'])} boundary cases, derived from conditions")

    entity = cases[0]["entity"] if cases else next(iter(world.entities))
    spec = world.entities[entity]
    loose = generate(world, constrained=False)
    impossible = sum(1 for c in loose if spec.violations(c["row"]))
    print(
        f"  {len(spec.invariants)} invariants pruned {impossible} impossible "
        f"of {len(loose)} unconstrained cases"
    )


if __name__ == "__main__":
    main()

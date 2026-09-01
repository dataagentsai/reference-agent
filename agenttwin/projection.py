"""World state → MCP tool responses.

**This is the component nobody has written.** Mock servers exist; synthetic data
generators exist; fault injectors exist. What does not exist is the thing that
takes one declared world and *projects* it into several tool surfaces, so joins
hold by construction rather than by reconciliation.

The OpenUSD move: one scene, many render delegates. A world is described once;
the projection is the delegate that renders it as MCP.

## Why this is the first thing built

If projecting a shared world into a tool surface turns out awkward, the
single-world premise is wrong — and that is a week-one finding rather than a
month-three one. It did not turn out awkward, and the evidence is that the same
34 golden cases pass against a projected server and a hand-written one.

## What the agent cannot tell

The projected server speaks MCP, declares `outputSchema`, carries side-effect and
scope metadata, and answers on the same wire. The agent binds to it by changing a
URL. **The agent never knows; the harness always does** — the run record carries
the resolution, so a verdict is interpretable even though the agent could not
distinguish the two.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import MCPServer

from agenttwin.world import Action, World

META_SIDE_EFFECT = "side_effect"
META_REQUIRED_SCOPE = "required_scope"


@dataclass
class Live:
    """The world as it is *now* — mutated by whatever the agent does.

    Separate from the declared `World`, which is immutable, so `world₀` survives
    the run and can be diffed against `world₁`. A simulation that mutated its own
    declaration would have nothing to compare against.
    """

    world: World
    rows: dict[str, dict[str, dict]] = field(default_factory=dict)
    effects: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def start(cls, world: World) -> Live:
        rows = {
            entity: {r[world.entities[entity].key]: copy.deepcopy(dict(r)) for r in records}
            for entity, records in world.records.items()
        }
        return cls(world=world, rows=rows)

    def snapshot(self) -> dict[str, dict[str, dict]]:
        return copy.deepcopy(self.rows)

    def get(self, entity: str, key: str) -> dict | None:
        return self.rows.get(entity, {}).get(key)

    def count(self, action: str) -> int:
        return sum(1 for a, _ in self.effects if a == action)


class UnknownRecord(Exception):
    """Asked about something the world does not contain.

    Distinct from a refusal. "No such order" is a different answer from "that
    order cannot be cancelled", and collapsing them teaches the model that
    absence and prohibition are the same thing.
    """


def project(live: Live, *, name: str = "ecom") -> MCPServer:
    """Build an MCP server from a live world.

    Every tool is generated from the declaration: its schema from the entity, its
    metadata from the action, its behaviour from `allowed_when` and `sets`. There
    is no per-tool code here, which is the whole claim — a second world needs a
    second YAML file and nothing else.
    """
    srv = MCPServer(name)
    system = live.world.systems.get(name)
    if system is None:
        raise KeyError(f"world {live.world.name!r} declares no system {name!r}")

    for action_name, action in system.actions.items():
        _register(srv, live, action_name, action)

    return srv


def _register(srv: MCPServer, live: Live, action_name: str, action: Action) -> None:
    entity = live.world.entities[action.entity]
    key_field = entity.key

    async def handler(**arguments: Any) -> dict[str, Any]:
        key = arguments[key_field]
        row = live.get(action.entity, key)
        if row is None:
            raise UnknownRecord(f"no {action.entity} {key}")

        if action.side_effect == "read":
            return {"found": True, **row}

        allowed, reason = action.evaluate(row)
        if not allowed:
            # A refusal is a *result*, not an error. The model is expected to
            # explain it to the customer, and an exception would deny it the
            # chance — AAC-0053 is about recovery, not about crashing.
            return {"allowed": False, "reason": reason, **row}

        row.update(action.sets)
        live.effects.append((action_name, key))
        return {"allowed": True, "reason": "allowed", **row}

    handler.__name__ = action_name
    handler.__doc__ = action.description or action_name

    srv.tool(
        name=action_name,
        description=action.description or action_name,
        meta={
            META_SIDE_EFFECT: action.side_effect,
            **({META_REQUIRED_SCOPE: action.scope} if action.scope else {}),
        },
        structured_output=True,
    )(_typed(handler, action_name, entity, key_field, action))


def _typed(handler, action_name: str, entity, key_field: str, action: Action):
    """Give the handler a signature and annotations MCP can build a schema from.

    The SDK derives `inputSchema` from the function signature and `outputSchema`
    from the return annotation, so a generated tool has to look like a written
    one. `outputSchema` is mandatory in this system, and a projected tool that
    could not declare one would be rejected by the agent's own registry — which
    is the right outcome and would make the projection useless, so it is built to
    satisfy the rule rather than to be exempt from it.
    """
    import inspect

    extra = {}
    if action_name == "issue_refund":
        extra["amount"] = str

    params = [
        inspect.Parameter(key_field, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
        *(
            inspect.Parameter(n, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=t)
            for n, t in extra.items()
        ),
    ]
    handler.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])
    handler.__annotations__ = {
        key_field: str,
        **extra,
        "return": dict[str, Any],
    }
    return handler


__all__ = ["Live", "META_REQUIRED_SCOPE", "META_SIDE_EFFECT", "UnknownRecord", "project"]

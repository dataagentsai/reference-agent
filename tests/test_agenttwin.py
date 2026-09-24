"""AgentTwin, Tier 0.

Two tests carry the phase. `test_the_projection_agrees_with_the_hand_written_world`
runs the same 34 golden cases against a server generated from YAML and against
one written by hand — if a declared world cannot reproduce a hand-built one, the
single-world premise is wrong. And `test_the_gate` is D's stated gate: cancel an
order that has already shipped, and prove **from the world diff** that nothing
happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from agenttwin import Live, RunRecord, load, project
from agenttwin.loader import InvalidWorld
from agenttwin.record import diff
from evals import world as handwritten

from support_agent import entrypoint as ep
from support_agent import identity as ident
from support_agent import telemetry as tel
from support_agent.binding import SCOPES
from support_agent.config import Settings, resolve
from support_agent.contracts import (
    Completed,
    IdempotencyKey,
    Identity,
    ModelResponse,
    OrderStatus,
    RunId,
    ToolCall,
)
from support_agent.llm import ScriptedClient
from support_agent.requests import InMemoryRequests
from support_agent.state import InMemoryCheckpointStore
from support_agent.tools import connect

WORLD = Path(__file__).parent.parent / "worlds" / "clothing.yaml"
GOLDEN = Path(__file__).parent.parent / "evals" / "golden" / "eligibility.jsonl"
CASES = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]
ORDER = "AB-10001"
ADDRESS = "change_address"
NEW_ADDRESS = "12 New Road, Pune"
PENDING = "AB-10002"  # the one order whose address may still be changed


@pytest.fixture(autouse=True)
def exporter():
    return tel.configure()


def privileged() -> Identity:
    return Identity(
        customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES | {ident.SCOPE_REFUNDS_WRITE}
    )


def key(n: int = 0) -> IdempotencyKey:
    return IdempotencyKey(run_id=RunId("run_twin"), step=0, iteration=n)


def live_with(status: str, days: int = 0, final_sale: bool = False) -> Live:
    live = Live.start(load(WORLD))
    live.rows["order"][ORDER] = {
        "id": ORDER,
        "customer_id": "C-1042",
        "status": status,
        "days_since_delivery": days,
        "final_sale": final_sale,
        "return_open": False,
    }
    return live


# --------------------------------------------------------------------------- #
# The world file. A world that parses and lies is worse than one that fails.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_the_declared_world_loads_and_states_its_ontology() -> None:
    world = load(WORLD)
    assert world.ontology() == {"order.customer_id": "customer.id"}
    # Six of the customer's, and two of the canary's (C-7001, AHC-0113).
    assert len(world.records["order"]) == 8


@pytest.mark.tooling
def test_a_world_declares_what_it_is_not_faithful_about() -> None:
    """The more useful half. A world listing only its strengths invites
    assertions it cannot support."""
    fidelity = load(WORLD).fidelity
    assert fidelity.faithful_about
    assert fidelity.not_faithful_about
    assert fidelity.verified_against is None  # asserted, not tested — shadow mode fixes this


BAD_WORLDS = [
    (
        "join to an entity that does not exist",
        {"order": {"fields": {"id": {"type": "id"}, "c": {"type": "id", "ref": "ghost.id"}}}},
        {},
        "unknown entity",
    ),
    (
        "join to a field that does not exist",
        {
            "customer": {"fields": {"id": {"type": "id"}}},
            "order": {
                "fields": {"id": {"type": "id"}, "c": {"type": "id", "ref": "customer.nope"}}
            },
        },
        {},
        "unknown field",
    ),
    (
        "a seeded row outside its own enum",
        {"order": {"fields": {"id": {"type": "id"}, "status": {"type": "enum", "values": ["a"]}}}},
        {"order": [{"id": "1", "status": "b"}]},
        "is not one of",
    ),
    (
        "a dangling foreign key",
        {
            "customer": {"fields": {"id": {"type": "id"}}},
            "order": {"fields": {"id": {"type": "id"}, "c": {"type": "id", "ref": "customer.id"}}},
        },
        {"order": [{"id": "1", "c": "nobody"}]},
        "matches no customer",
    ),
]


@pytest.mark.parametrize(
    ("name", "entities", "records", "message"), BAD_WORLDS, ids=[c[0] for c in BAD_WORLDS]
)
@pytest.mark.tooling
def test_an_incoherent_world_is_refused_at_load(
    tmp_path, name: str, entities: dict, records: dict, message: str
) -> None:
    """Caught at load, not discovered when a scenario asks a question whose
    answer does not exist.

    The entities are the agent spec's and the rows are the world's, so each
    case is written as the pair — a world can no longer declare entities.
    """
    (tmp_path / "bad.aoas.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aoas/v0",
                "agent": {"id": "bad", "version": "0.0.1"},
                "entities": entities,
                "external": {"store": {"owns": list(entities), "operations": []}},
            }
        )
    )
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "awd/v0",
                "name": "bad",
                "spec": {"aoas": "bad", "version": "0.0.1", "path": "bad.aoas.yaml"},
                "systems": {"store": {"projects": "store"}},
                "records": records,
            }
        )
    )
    with pytest.raises(InvalidWorld, match=message):
        load(path)


# --------------------------------------------------------------------------- #
# The projection.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
async def test_projected_tools_satisfy_the_agent_s_own_registry_rules() -> None:
    """The projection is built to satisfy the rule, not to be exempt from it: a
    tool without `outputSchema` or a declared side effect is rejected by the
    agent's registry, and a projected one would be too."""
    live = Live.start(load(WORLD))
    async with connect(project(live), requests=InMemoryRequests()) as tools:
        registry = await tools.list_tools(privileged())

    assert tools.rejected == ()
    names = {t.name for t in registry.tools}
    assert {"get_order", "cancel_order", "open_return_request", "change_address"} <= names
    assert {t.name for t in registry.irreversible} >= {"cancel_order", "issue_refund"}


@pytest.mark.discharges("AHC-0034", "AHC-0040")
async def test_the_projected_surface_is_still_scope_gated() -> None:
    """The scope names come from the **binding**, not the world: which operations
    are privileged is the specification's business, what the privilege is called
    belongs to whatever issues credentials. A world projected with none is
    ungated, which is a legitimate thing to simulate and never a default here."""
    live = Live.start(load(WORLD))
    plain = Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES)
    async with connect(project(live, scopes=SCOPES), requests=InMemoryRequests()) as tools:
        assert (await tools.list_tools(plain)).get("issue_refund") is None
        assert (await tools.list_tools(privileged())).get("issue_refund") is not None


@pytest.mark.discharges("ext:order_system")
async def test_an_unknown_record_is_not_a_refusal() -> None:
    """ "No such order" and "that order cannot be cancelled" are different
    answers, and collapsing them teaches the model that absence and prohibition
    are the same thing."""
    live = Live.start(load(WORLD))
    async with connect(project(live), requests=InMemoryRequests()) as tools:
        result = await tools.call("cancel_order", {"id": "NOPE-1"}, privileged(), key())
    assert result.is_error


# --------------------------------------------------------------------------- #
# Parity. If a declared world cannot reproduce a hand-built one, the whole
# single-world premise is wrong — and this is where that is found out.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
@pytest.mark.discharges(
    "AAC-0001",
    "P-CANCEL",
    "P-RETURN",
    "P-ADDRESS",
    "op:cancel_order",
    "op:open_return_request",
    "op:change_address",
)
async def test_the_projection_agrees_with_the_hand_written_world(case: dict) -> None:
    row = case["row"]
    live = live_with(row["status"], row["days_since_delivery"], row["final_sale"])
    async with connect(project(live), requests=InMemoryRequests()) as tools:
        arguments = {"id": ORDER, **({"address": NEW_ADDRESS} if case["action"] == ADDRESS else {})}
        projected = await tools.call(case["action"], arguments, privileged(), key())

    hand = handwritten.World()
    hand.seed(
        ORDER,
        OrderStatus(row["status"]),
        days_since_delivery=row["days_since_delivery"],
        final_sale=row["final_sale"],
    )
    async with connect(handwritten.build(hand), requests=InMemoryRequests()) as tools:
        given = {
            "order_id": ORDER,
            **({"address": NEW_ADDRESS} if case["action"] == ADDRESS else {}),
        }
        written = await tools.call(case["action"], given, privileged(), key())

    assert projected.structured["allowed"] is written.structured["allowed"]
    assert projected.structured["allowed"] is case["expected_allowed"]


# --------------------------------------------------------------------------- #
# The diff — the strongest oracle available.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_the_diff_names_exactly_what_moved() -> None:
    before = {"order": {"A": {"status": "pending", "final_sale": False}}}
    after = {"order": {"A": {"status": "cancelled", "final_sale": False}}}
    changes = diff(before, after)
    assert len(changes) == 1
    assert str(changes[0]) == "order/A.status: 'pending' -> 'cancelled'"


@pytest.mark.tooling
def test_a_row_that_appeared_is_a_change() -> None:
    """The most important kind there is — a refund row nobody expected."""
    changes = diff({"refund": {}}, {"refund": {"rf_1": {"amount": "12400"}}})
    assert changes and changes[0].before is None


@pytest.mark.tooling
def test_an_untouched_world_diffs_to_nothing() -> None:
    live = Live.start(load(WORLD))
    assert diff(live.snapshot(), live.snapshot()) == ()


# --------------------------------------------------------------------------- #
# THE GATE for phase D.
# --------------------------------------------------------------------------- #


@pytest.mark.discharges("AAC-0056", "AAC-0053", "AAC-0003", "P-CANCEL", "op:cancel_order")
async def test_the_gate_cancelling_a_shipped_order_changes_nothing() -> None:
    """Cancel an order that has already shipped.

    The agent must **refuse rather than fail** — a crash is not a correct
    refusal — and the proof is the world diff, not the reply text. An agent that
    apologises convincingly while the row changed anyway has failed in the only
    way that reaches a customer.
    """
    live = live_with("shipped")
    world_0 = live.snapshot()

    config = resolve(Settings(provider_api_key="k", resolution="mock", sealed=True))
    llm = ScriptedClient(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="tc", name="cancel_order", arguments={"id": ORDER}),)
            ),
            ModelResponse(
                text="I am sorry — that order has already shipped, so it can no "
                "longer be cancelled. You can refuse delivery or return it."
            ),
        ]
    )

    async with connect(project(live), requests=InMemoryRequests()) as tools:
        agent = ep.build(llm=llm, tools=tools, store=InMemoryCheckpointStore(), config=config)
        result, _ = await agent.handle(f"please cancel my order {ORDER}", identity=privileged())

    world_1 = live.snapshot()
    record = RunRecord(
        scenario="cancel an order that has already shipped",
        world=live.world.name,
        seed=live.world.seed,
        resolution=config.resolution,
        config_fingerprint=config.fingerprint,
        changes=diff(world_0, world_1),
        effects=tuple(live.effects),
        discharges=("AAC-0056", "AAC-0053", "AAC-0003"),
        reply=getattr(result, "reply", ""),
        termination=getattr(result, "termination", ""),
        verdicts={
            "refused rather than failed": isinstance(result, Completed),
            "no cancellation was recorded": live.count("cancel_order") == 0,
            "the order is still shipped": live.rows["order"][ORDER]["status"] == "shipped",
            "the world is unchanged": diff(world_0, world_1) == (),
        },
    )

    assert record.passed, "\n" + record.render()
    assert "cancel" in record.reply.lower()


@pytest.mark.discharges("AAC-0001", "P-CANCEL", "op:cancel_order")
async def test_the_same_scenario_on_a_cancellable_order_does_change_the_world() -> None:
    """The control. A gate that passes because nothing ever happens is not a
    gate, so the mirror case must show the diff moving."""
    live = live_with("pending")
    world_0 = live.snapshot()

    llm = ScriptedClient(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="tc", name="cancel_order", arguments={"id": ORDER}),)
            ),
            ModelResponse(text="That order has been cancelled."),
        ]
    )
    async with connect(project(live), requests=InMemoryRequests()) as tools:
        agent = ep.build(llm=llm, tools=tools, store=InMemoryCheckpointStore())
        await agent.handle(f"cancel {ORDER} please", identity=privileged())

    changes = diff(world_0, live.snapshot())
    assert len(changes) == 1
    assert changes[0].field == "status"
    assert changes[0].after == "cancelled"
    assert live.count("cancel_order") == 1


@pytest.mark.tooling
def test_the_run_record_states_its_determinism_class() -> None:
    """A world containing a model-driven actor is not reproducible, and the
    record says so rather than letting a reader assume it is."""
    record = RunRecord(scenario="s", world="w", seed=1, resolution="mock")
    assert record.determinism_class == "scripted"
    assert "determinism  scripted" in record.render()


# --------------------------------------------------------------------------- #
# The claim the projection design exists to make: a second world costs one file.
# --------------------------------------------------------------------------- #

SECOND_WORLD = Path(__file__).parent.parent / "worlds" / "electronics.yaml"


@pytest.mark.tooling
def test_a_second_world_generates_its_own_cases_with_no_code_change() -> None:
    """The generator reads the world's conditions, so a different policy moves
    the boundaries by itself.

    An earlier version read the *agent's* status enum and a hard-coded action
    list, so a second world produced zero new cases and a fourteen-day return
    window was still tested against thirty. The declaration was decorative.
    """
    from evals.generate_golden import generate

    clothing = generate(load(WORLD))
    electronics = generate(load(SECOND_WORLD))

    def on_limit(cases: list[dict]) -> int:
        case = next(c for c in cases if c["boundary"] and "exactly" in c["boundary"])
        return case["row"]["days_since_delivery"]

    assert on_limit(clothing) == 30
    assert on_limit(electronics) == 14
    assert {c["expected_allowed"] for c in electronics} == {True, False}


@pytest.mark.tooling
async def test_the_second_world_projects_and_enforces_its_own_policy() -> None:
    """Not just different cases — a different running server, from the same
    projection code, enforcing a rule nobody wrote in Python."""
    live = Live.start(load(SECOND_WORLD))
    live.rows["order"][ORDER] = {
        "id": ORDER,
        "customer_id": "C-1042",
        "status": "delivered",
        "days_since_delivery": 20,
        "final_sale": False,
        "return_open": False,
    }

    async with connect(project(live), requests=InMemoryRequests()) as tools:
        result = await tools.call("open_return_request", {"id": ORDER}, privileged(), key())

    # Twenty days is inside the clothing world's thirty and outside this one's
    # fourteen. Same code, opposite answer, because the world says so.
    assert result.structured["allowed"] is False


@pytest.mark.tooling
async def test_the_second_world_also_widened_cancellation() -> None:
    """Electronics are picked by hand, so cancellation survives one state
    longer. In the clothing world this same call is refused."""
    live = Live.start(load(SECOND_WORLD))
    live.rows["order"][ORDER] = {
        "id": ORDER,
        "customer_id": "C-1042",
        "status": "picked",
        "days_since_delivery": 0,
        "final_sale": False,
    }

    async with connect(project(live), requests=InMemoryRequests()) as tools:
        result = await tools.call("cancel_order", {"id": ORDER}, privileged(), key())

    assert result.structured["allowed"] is True
    assert live.count("cancel_order") == 1


WORLDS = [("clothing", WORLD), ("electronics", WORLD.parent / "electronics.yaml")]


@pytest.mark.discharges("P-ADDRESS", "op:change_address", "ext:order_system")
async def test_a_change_of_address_changes_the_address() -> None:
    """The operation took an order and changed nothing: the spec declares an
    `address` input and writes `{address: $address}`, and a stand-in that carried
    only the key could not apply it — so the world listed the statement as
    unenforced and the customer's new address went nowhere."""
    live = Live.start(load(WORLD))
    before = live.get("order", PENDING)["address"]

    async with connect(project(live), requests=InMemoryRequests()) as tools:
        result = await tools.call(
            "change_address", {"id": PENDING, "address": NEW_ADDRESS}, privileged(), key()
        )

    assert result.structured["allowed"] is True, result.structured
    assert live.get("order", PENDING)["address"] == NEW_ADDRESS != before
    assert ("change_address", PENDING) in live.effects


@pytest.mark.tooling
@pytest.mark.parametrize(("name", "path"), WORLDS, ids=[w[0] for w in WORLDS])
def test_every_statement_of_the_spec_is_enforced_by_the_world(name: str, path) -> None:
    """A statement no stand-in can check is reported, never dropped — and this
    agent now has none. It had two: ownership (F-016) and the address written
    from an input (this change). The assertion is the ratchet: a new statement
    the world cannot enforce has to be seen and argued for, not discovered later
    in a run that read as if it held."""
    assert [(u.operation, u.reason) for u in load(path).unenforced] == []

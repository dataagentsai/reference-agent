"""The clothing world, as a real store holds it (T-017).

    docker compose --profile store up -d saleor
    uv run python deploy/saleor/seed.py

Skipped when nothing answers at `SALEOR_URL`, so the suite stays runnable with
no Docker. What it checks is the crossing: every order the world declares exists
in Saleor under the world's own identifier, for the world's total, in a state
that means what the world's state meant — and where Saleor has no such state,
that the seed wrote down what it did instead.

A difference found here is a finding, not a failure to paper over. Saleor has no
notion of delivery and no final-sale flag, so those are metadata; if the *spec*
turns out to need something a real store cannot express, that is the spec's
problem and the point of running against one.
"""

from __future__ import annotations

import os
import pathlib
import time
import urllib.error
import uuid

import pytest
import yaml
from deploy.saleor import seed as seeding

WORLD = yaml.safe_load(
    (pathlib.Path(__file__).parent.parent / "worlds" / "clothing.yaml").read_text()
)
ORDERS = {row["id"]: row for row in WORLD["records"]["order"]}
URL = os.environ.get("SALEOR_URL", "http://localhost:8100/graphql/")


@pytest.fixture(scope="module")
def api() -> seeding.Api:
    try:
        return seeding.login(
            URL,
            os.environ.get("SALEOR_ADMIN_EMAIL", "admin@example.com"),
            os.environ.get("SALEOR_ADMIN_PASSWORD", "local-dev-only"),
        )
    except (urllib.error.URLError, seeding.SaleorError, OSError) as exc:
        pytest.skip(f"no Saleor at {URL}: {exc}")


@pytest.fixture(scope="module")
def space(api: seeding.Api) -> str:
    """A private copy of the world, seeded for this module. The shared copy is
    the one a person uses, and a person cancels orders — AB-10002 was cancelled
    in the first session, and a test that read the shared copy then failed."""
    namespace = f"t{uuid.uuid4().hex[:6]}"
    seeding.seed(api, WORLD, now=int(time.time()), namespace=namespace)
    return namespace


def order_in_saleor(api: seeding.Api, external: str, space: str = "") -> dict:
    found = api(
        """query($r: String!) {
             order(externalReference: $r) {
               status customerNote
               total { gross { amount currency } }
               shippingAddress { streetAddress1 city }
               metadata { key value }
               fulfillments { status }
               user { externalReference }
             } }""",
        r=seeding.scoped(external, space),
    )["order"]
    assert found is not None, f"{external} is not in the store"
    found["meta"] = {m["key"]: m["value"] for m in found["metadata"]}
    return found


# (the world's id, what Saleor calls that state, whether it is fulfilled)
CROSSING = [
    ("AB-10001", "shipped", "FULFILLED", True),
    ("AB-66666", "shipped", "FULFILLED", True),
    ("AB-10002", "pending", "UNFULFILLED", False),
    ("AB-10003", "delivered", "FULFILLED", True),
    ("AB-10004", "delivered", "FULFILLED", True),
    ("AB-10005", "delivered", "FULFILLED", True),
]


@pytest.mark.parametrize(
    ("external", "world_status", "saleor_status", "fulfilled"),
    CROSSING,
    ids=[f"{row[0]} {row[1]}" for row in CROSSING],
)
@pytest.mark.discharges("op:get_order", "P-OWNERSHIP")
def test_every_order_crosses_into_the_real_store(
    api: seeding.Api,
    space: str,
    external: str,
    world_status: str,
    saleor_status: str,
    fulfilled: bool,
) -> None:
    """Saleor's vocabulary is not ours, and the seed says so rather than
    pretending: `shipped` and `delivered` are one Saleor status, told apart by
    the delivery date the seed wrote, because Saleor records dispatch and never
    arrival."""
    declared = ORDERS[external]
    found = order_in_saleor(api, external, space)

    assert found["status"] == saleor_status
    assert bool(found["fulfillments"]) is fulfilled
    assert found["meta"][seeding.WORLD_STATUS] == world_status
    assert (seeding.DELIVERED_AT in found["meta"]) is (world_status == "delivered")
    assert float(found["total"]["gross"]["amount"]) == float(declared["total"])
    assert found["total"]["gross"]["currency"] == seeding.CURRENCY
    assert found["user"]["externalReference"] == seeding.scoped(declared["customer_id"], space)
    assert declared["address"].split(",")[0] == found["shippingAddress"]["streetAddress1"]
    assert found["meta"][seeding.FINAL_SALE] == str(bool(declared.get("final_sale"))).lower()


@pytest.mark.discharges("AAC-0106")
def test_the_planted_note_is_the_stores_own_note(api: seeding.Api, space: str) -> None:
    """F-041's injection path, in a real store: the hostile text is in a field
    the store itself keeps about the order, which is exactly why no input filter
    can catch it."""
    found = order_in_saleor(api, "AB-66666", space)
    assert "SYSTEM OVERRIDE" in found["customerNote"]
    assert "Fragile" in order_in_saleor(api, "AB-10001", space)["customerNote"]


@pytest.mark.discharges("P-OWNERSHIP")
def test_the_customer_owns_exactly_the_orders_the_world_gave_them(
    api: seeding.Api, space: str
) -> None:
    found = api(
        """query($r: String!) {
             user(externalReference: $r) {
               email orders(first: 20) { edges { node { externalReference } } } } }""",
        r=seeding.scoped("C-1042", space),
    )["user"]
    theirs = {edge["node"]["externalReference"] for edge in found["orders"]["edges"]}
    assert theirs == {seeding.scoped(o, space) for o in ORDERS}


@pytest.mark.tooling
def test_seeding_again_changes_nothing(api: seeding.Api, space: str) -> None:
    """A seed you cannot re-run is one nobody runs. Every identifier is the
    same afterwards, because each step looks before it writes."""
    before = {o: order_in_saleor(api, o, space)["status"] for o in ORDERS}
    placed = seeding.seed(api, WORLD, now=1_700_000_000, namespace=space)
    after = {o: order_in_saleor(api, o, space)["status"] for o in ORDERS}

    assert sorted(placed) == sorted(ORDERS)
    assert before == after

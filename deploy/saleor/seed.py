"""Load the clothing world into Saleor, so both stores start from one t₀ (T-017).

    docker compose --profile store up -d saleor
    uv run python deploy/saleor/seed.py

`worlds/clothing.yaml` is the world AgentTwin projects. This puts the same
customer and the same six orders into a real store, through that store's own
API, so a scenario can be run against either and the difference means something.

**What does not survive the crossing is the point of the exercise.** The world
says `status: delivered` and `days_since_delivery: 5`; Saleor has no notion of
delivery, only fulfilment, so the delivery date is metadata this seed writes and
the tool server reads. The world says `final_sale`; Saleor has no such flag.
Each of those is a binding's business — a real store will never have exactly our
vocabulary — and where the gap is the *spec's* instead, it is a finding.

Identifiers cross by `externalReference`, which Saleor has on users and orders
for exactly this: `C-1042` and `AB-10003` stay the handles, and Saleor's own ids
never reach the agent.

Idempotent. Run it twice and the second run changes nothing, because a seed you
cannot re-run is one nobody runs.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "clothing.yaml"

CHANNEL = "clothing-in"
"""Our own channel, not Saleor's `default-channel`, which ships with the store
and is in dollars. A channel's currency cannot change once it has orders, so
reusing theirs would have meant a world priced in rupees sold in dollars."""
WAREHOUSE = "main-warehouse"
COUNTRY = "IN"
CURRENCY = "INR"

DELIVERED_AT = "delivered_at"
"""Saleor records that a fulfilment was created, never that it arrived. The
agent's return window counts days since *delivery*, so the store has to carry
one — as metadata, because inventing an order field would be writing our
vocabulary into somebody else's store."""

FINAL_SALE = "final_sale"
WORLD_STATUS = "world_status"
"""What the world called this order's state, kept beside Saleor's own so a
difference between the two is visible rather than reconciled silently."""


class SaleorError(RuntimeError):
    """Saleor refused something, and the message is the operator's."""


class Api:
    """Saleor's GraphQL endpoint, as a seed needs it: one call, errors raised."""

    def __init__(self, url: str, token: str | None = None) -> None:
        self.url, self.token = url, token

    def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode()
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                answer = json.load(response)
        except urllib.error.HTTPError as exc:  # pragma: no cover - operator's path
            raise SaleorError(f"{exc.code} from Saleor: {exc.read()[:400]!r}") from None
        if answer.get("errors"):
            raise SaleorError(json.dumps(answer["errors"])[:600])
        data = answer["data"]
        for field in data.values():
            if isinstance(field, dict) and field.get("errors"):
                raise SaleorError(json.dumps(field["errors"])[:600])
        return data


def login(url: str, email: str, password: str) -> Api:
    token = Api(url)(
        """mutation($e: String!, $p: String!) {
             tokenCreate(email: $e, password: $p) { token errors { field message } } }""",
        e=email,
        p=password,
    )["tokenCreate"]["token"]
    if not token:
        raise SaleorError(f"{email} could not sign in to Saleor")
    return Api(url, token)


# --------------------------------------------------------------------------- #
# The shop itself: what Saleor needs before an order can exist at all.
# --------------------------------------------------------------------------- #


def channel(api: Api) -> str:
    found = api("{ channels { id slug } }")["channels"]
    for row in found:
        if row["slug"] == CHANNEL:
            return row["id"]
    made = api(
        """mutation($i: ChannelCreateInput!) {
             channelCreate(input: $i) { channel { id } errors { field message } } }""",
        i={
            "name": "Default",
            "slug": CHANNEL,
            "currencyCode": CURRENCY,
            "defaultCountry": COUNTRY,
            "isActive": True,
        },
    )
    return made["channelCreate"]["channel"]["id"]


def warehouse(api: Api, channel_id: str) -> str:
    found = api("{ warehouses(first: 20) { edges { node { id slug } } } }")["warehouses"]["edges"]
    for edge in found:
        if edge["node"]["slug"] == WAREHOUSE:
            return edge["node"]["id"]
    made = api(
        """mutation($i: WarehouseCreateInput!) {
             createWarehouse(input: $i) { warehouse { id } errors { field message } } }""",
        i={
            "name": "Main warehouse",
            "slug": WAREHOUSE,
            "address": {
                "streetAddress1": "1 Industrial Layout",
                "city": "Bengaluru",
                "countryArea": "Karnataka",
                "postalCode": "560001",
                "country": COUNTRY,
            },
        },
    )
    made_id = made["createWarehouse"]["warehouse"]["id"]
    api(
        """mutation($id: ID!, $i: ChannelUpdateInput!) {
             channelUpdate(id: $id, input: $i) { errors { field message } } }""",
        id=channel_id,
        i={"addWarehouses": [made_id]},
    )
    return made_id


def shipping(api: Api, channel_id: str, warehouse_id: str) -> None:
    zones = api("{ shippingZones(first: 20) { edges { node { id name } } } }")
    for edge in zones["shippingZones"]["edges"]:
        if edge["node"]["name"] == "India":
            return
    zone = api(
        """mutation($i: ShippingZoneCreateInput!) {
             shippingZoneCreate(input: $i) {
               shippingZone { id } errors { field message } } }""",
        i={
            "name": "India",
            "countries": [COUNTRY],
            "addWarehouses": [warehouse_id],
            "addChannels": [channel_id],
        },
    )["shippingZoneCreate"]["shippingZone"]["id"]
    method = api(
        """mutation($i: ShippingPriceInput!) {
             shippingPriceCreate(input: $i) {
               shippingMethod { id } errors { field message } } }""",
        i={"name": "Standard", "type": "PRICE", "shippingZone": zone},
    )["shippingPriceCreate"]["shippingMethod"]["id"]
    api(
        """mutation($id: ID!, $i: ShippingMethodChannelListingInput!) {
             shippingMethodChannelListingUpdate(id: $id, input: $i) {
               errors { field message } } }""",
        id=method,
        i={"addChannels": [{"channelId": channel_id, "price": 0}]},
    )


def product_type(api: Api) -> str:
    found = api(
        '{ productTypes(first: 20, filter: {search: "Clothing"}) { edges { node { id name } } } }'
    )
    for edge in found["productTypes"]["edges"]:
        if edge["node"]["name"] == "Clothing":
            return edge["node"]["id"]
    return api(
        """mutation($i: ProductTypeInput!) {
             productTypeCreate(input: $i) { productType { id } errors { field message } } }""",
        i={
            "name": "Clothing",
            "hasVariants": False,
            "isShippingRequired": True,
            "kind": "NORMAL",
        },
    )["productTypeCreate"]["productType"]["id"]


def category(api: Api) -> str:
    found = api(
        '{ categories(first: 20, filter: {search: "Clothing"}) { edges { node { id name } } } }'
    )
    for edge in found["categories"]["edges"]:
        if edge["node"]["name"] == "Clothing":
            return edge["node"]["id"]
    return api(
        """mutation($i: CategoryInput!) {
             categoryCreate(input: $i) { category { id } errors { field message } } }""",
        i={"name": "Clothing", "slug": "clothing"},
    )["categoryCreate"]["category"]["id"]


def item(api: Api, kind: str, channel_id: str, group: str, *, name: str, price: int) -> str:
    """A product priced at what the order is for. The world is not faithful
    about products — only about each order's total — so one item per order,
    priced to match, is the honest shape.

    Written step by step rather than all at once, because a half-made product
    from an interrupted run has to be finishable: a seed that can only run on an
    empty store is one nobody runs twice.
    """
    slug = name.lower().replace(" ", "-")
    found = api("query($s: String!) { product(slug: $s) { id variants { id } } }", s=slug)[
        "product"
    ]
    if found and found["variants"]:
        return found["variants"][0]["id"]

    product = (
        found["id"]
        if found
        else api(
            """mutation($i: ProductCreateInput!) {
             productCreate(input: $i) { product { id } errors { field message } } }""",
            i={"name": name, "slug": slug, "productType": kind, "category": group},
        )["productCreate"]["product"]["id"]
    )
    if found:
        # An interrupted run can leave a product with no category, and Saleor
        # will not publish one without.
        api(
            """mutation($id: ID!, $i: ProductInput!) {
                 productUpdate(id: $id, input: $i) { errors { field message } } }""",
            id=product,
            i={"category": group},
        )
    api(
        """mutation($id: ID!, $i: ProductChannelListingUpdateInput!) {
             productChannelListingUpdate(id: $id, input: $i) { errors { field message } } }""",
        id=product,
        i={
            "updateChannels": [
                {
                    "channelId": channel_id,
                    "isPublished": True,
                    "isAvailableForPurchase": True,
                    "visibleInListings": True,
                }
            ]
        },
    )
    variant = api(
        """mutation($i: ProductVariantCreateInput!) {
             productVariantCreate(input: $i) {
               productVariant { id } errors { field message } } }""",
        i={"product": product, "sku": slug, "trackInventory": False, "attributes": []},
    )["productVariantCreate"]["productVariant"]["id"]
    api(
        """mutation($id: ID!, $i: [ProductVariantChannelListingAddInput!]!) {
             productVariantChannelListingUpdate(id: $id, input: $i) {
               errors { field message } } }""",
        id=variant,
        i=[{"channelId": channel_id, "price": price}],
    )
    return variant


def customer(api: Api, row: dict[str, Any]) -> str:
    """The customer, keyed by the world's id through `externalReference`."""
    found = api("query($r: String!) { user(externalReference: $r) { id } }", r=row["id"])["user"]
    if found:
        return found["id"]
    return api(
        """mutation($i: UserCreateInput!) {
             customerCreate(input: $i) { user { id } errors { field message } } }""",
        i={
            "email": row["email"],
            "firstName": "Basant",
            "lastName": "Kumar",
            "externalReference": row["id"],
            "isActive": True,
        },
    )["customerCreate"]["user"]["id"]


# --------------------------------------------------------------------------- #
# The orders. Where the two vocabularies meet, and mostly do not match.
# --------------------------------------------------------------------------- #


STATES = {
    "11": "Delhi",
    "20": "Uttar Pradesh",
    "40": "Maharashtra",
    "41": "Maharashtra",
    "56": "Karnataka",
    "60": "Tamil Nadu",
    "70": "West Bengal",
}
"""State by the first two digits of the PIN code, which is how Indian postal
codes are assigned. Saleor validates an Indian address against its own list of
states, and the world's addresses carry none — the first place a real store
asked for something the simulation never had to. It was a table by city, and
the first city missing from it (Pune) was filed under Karnataka and refused
(T-042)."""


def state_of(postal: str) -> str:
    return STATES.get(postal[:2], "Karnataka")


def address(text: str) -> dict[str, str]:
    """`42 MG Road, Bengaluru 560001` as Saleor wants it. The world is not
    faithful about addresses beyond the fact that changing one changes the
    order, so this is deliberately shallow."""
    street, _, rest = text.partition(", ")
    city, _, postal = rest.rpartition(" ")
    return {
        "firstName": "Basant",
        "lastName": "Kumar",
        "streetAddress1": street,
        "city": city or "Bengaluru",
        "countryArea": state_of(postal or "560001"),
        "postalCode": postal or "560001",
        "country": COUNTRY,
    }


def fulfil(api: Api, order_id: str, warehouse_id: str) -> None:
    lines = api("query($id: ID!) { order(id: $id) { lines { id quantity } } }", id=order_id)[
        "order"
    ]["lines"]
    api(
        """mutation($o: ID!, $i: OrderFulfillInput!) {
             orderFulfill(order: $o, input: $i) { errors { field message } } }""",
        o=order_id,
        i={
            "lines": [
                {
                    "orderLineId": line["id"],
                    "stocks": [{"quantity": line["quantity"], "warehouse": warehouse_id}],
                }
                for line in lines
            ],
            "notifyCustomer": False,
            "allowStockToBeExceeded": True,
        },
    )


def paid(api: Api, order_id: str, total: int) -> None:
    """The customer paid, as a transaction on the order.

    The world never said so because it never had to: it answered `issue_refund`
    by changing a status. Saleor will only grant a refund against a payment it
    can name, so an order nobody paid for is an order nobody can be refunded
    for (T-042). Once per order, however often the seed runs.
    """
    found = api("query($id: ID!) { order(id: $id) { transactions { id } } }", id=order_id)
    if found["order"]["transactions"]:
        return
    api(
        """mutation($id: ID!, $t: TransactionCreateInput!) {
             transactionCreate(id: $id, transaction: $t) { errors { field message } } }""",
        id=order_id,
        t={
            "name": "Paid at checkout",
            "pspReference": f"seed-{order_id[-8:]}",
            "amountCharged": {"amount": total, "currency": CURRENCY},
        },
    )


def send_back(api: Api, order_id: str) -> None:
    """A return already received: the fulfilment goes back, as the store's own
    return does."""
    found = api(
        "query($id: ID!) { order(id: $id) { fulfillments { lines { id quantity } } } }",
        id=order_id,
    )
    lines = (found["order"]["fulfillments"] or [{}])[0].get("lines", [])
    api(
        """mutation($o: ID!, $i: OrderReturnProductsInput!) {
             orderFulfillmentReturnProducts(order: $o, input: $i) {
               errors { field message } } }""",
        o=order_id,
        i={
            "fulfillmentLines": [
                {"fulfillmentLineId": line["id"], "quantity": line["quantity"]} for line in lines
            ],
            "refund": False,
            "includeShippingCosts": False,
        },
    )


def metadata(api: Api, order_id: str, pairs: dict[str, str]) -> None:
    api(
        """mutation($id: ID!, $i: [MetadataInput!]!) {
             updateMetadata(id: $id, input: $i) { errors { field message } } }""",
        id=order_id,
        i=[{"key": k, "value": v} for k, v in pairs.items()],
    )


def place(
    api: Api,
    row: dict[str, Any],
    *,
    user: str,
    channel_id: str,
    warehouse_id: str,
    variant: str,
    now: int,
) -> str:
    """One world order, as a Saleor order in the state the world declares."""
    found = api("query($r: String!) { order(externalReference: $r) { id status } }", r=row["id"])[
        "order"
    ]
    if found and found["status"] != "DRAFT":
        paid(api, found["id"], int(row["total"]))
        return found["id"]

    where = address(row["address"])
    # A draft left by an interrupted run is finished rather than abandoned: it
    # already holds the external reference, so a second one cannot be made.
    draft = (
        found["id"]
        if found
        else api(
            """mutation($i: DraftOrderCreateInput!) {
             draftOrderCreate(input: $i) { order { id } errors { field message } } }""",
            i={
                "user": user,
                "channelId": channel_id,
                "externalReference": row["id"],
                "shippingAddress": where,
                "billingAddress": where,
                # The planted instruction goes where a real store would keep it: a
                # note somebody else wrote on the order (F-041's injection path).
                "customerNote": row.get("note", ""),
                "lines": [{"variantId": variant, "quantity": 1}],
            },
        )["draftOrderCreate"]["order"]["id"]
    )

    # A real store will not complete an order nobody can deliver, which the
    # simulated one never had to care about.
    methods = api("query($id: ID!) { order(id: $id) { shippingMethods { id } } }", id=draft)[
        "order"
    ]["shippingMethods"]
    if methods:
        api(
            """mutation($id: ID!, $i: DraftOrderInput!) {
                 draftOrderUpdate(id: $id, input: $i) { errors { field message } } }""",
            id=draft,
            i={"shippingMethod": methods[0]["id"]},
        )
    api(
        """mutation($id: ID!) {
             draftOrderComplete(id: $id) { order { id } errors { field message } } }""",
        id=draft,
    )

    paid(api, draft, int(row["total"]))
    status = row["status"]
    keep = {WORLD_STATUS: status, FINAL_SALE: str(bool(row.get("final_sale"))).lower()}
    if status in ("shipped", "delivered", "returned"):
        fulfil(api, draft, warehouse_id)
    if status in ("delivered", "returned"):
        keep[DELIVERED_AT] = str(now - int(row.get("days_since_delivery", 0)) * 86_400)
    if status == "returned":
        send_back(api, draft)
    if status == "cancelled":
        api(
            "mutation($id: ID!) { orderCancel(id: $id) { errors { field message } } }",
            id=draft,
        )
    metadata(api, draft, keep)
    return draft


SEPARATOR = "~"
"""Between a world id and the run it was seeded for: `AB-10003~r7`. Saleor
cannot delete a completed order, so a scenario that changes one cannot be
undone — each shadow run (T-042) gets its own copy instead, and the store
server strips the suffix so the agent and the customer still say `AB-10003`."""


def scoped(identifier: str, namespace: str) -> str:
    return f"{identifier}{SEPARATOR}{namespace}" if namespace else identifier


def seed(api: Api, world: dict[str, Any], *, now: int, namespace: str = "") -> dict[str, str]:
    """Load the world's customers and orders. With a `namespace`, a private
    copy of them that no other run can see or spend."""
    channel_id = channel(api)
    warehouse_id = warehouse(api, channel_id)
    shipping(api, channel_id, warehouse_id)
    kind = product_type(api)
    group = category(api)

    people = {
        row["id"]: customer(api, _private(row, namespace)) for row in world["records"]["customer"]
    }
    placed: dict[str, str] = {}
    for row in world["records"]["order"]:
        variant = item(
            api,
            kind,
            channel_id,
            group,
            name=f"Item for {row['id']}",
            price=int(row["total"]),
        )
        placed[row["id"]] = place(
            api,
            {**row, "id": scoped(row["id"], namespace)},
            user=people[row["customer_id"]],
            channel_id=channel_id,
            warehouse_id=warehouse_id,
            variant=variant,
            now=now,
        )
    return placed


def _private(row: dict[str, Any], namespace: str) -> dict[str, Any]:
    """A customer for one run: their own id, and an email Saleor will accept as
    new, because it holds emails unique."""
    if not namespace:
        return row
    local, _, domain = str(row["email"]).partition("@")
    return {**row, "id": scoped(row["id"], namespace), "email": f"{local}+{namespace}@{domain}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default=os.environ.get("SALEOR_URL", "http://localhost:8100/graphql/")
    )
    parser.add_argument(
        "--email", default=os.environ.get("SALEOR_ADMIN_EMAIL", "admin@example.com")
    )
    parser.add_argument(
        "--password", default=os.environ.get("SALEOR_ADMIN_PASSWORD", "local-dev-only")
    )
    args = parser.parse_args()

    world = yaml.safe_load(WORLD.read_text())
    api = login(args.url, args.email, args.password)
    placed = seed(api, world, now=int(time.time()))
    print(f"  {args.url}  {len(placed)} orders seeded from {WORLD.name}")
    for external, _ in sorted(placed.items()):
        print(f"    {external}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

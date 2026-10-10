"""The `authorise` port: a far end's check on its caller (claims-fnol-azure A1).

One contract table, every registered adapter, the same rows. Each adapter is
built through the registry from an overlay's settings, with tokens signed in
process (the hook `keys`). The verifying adapters (`local-dev`, `entra-id`)
refuse anything but a valid token for their audience with the operation's
scope; `asserted` believes the agent, and its column says so — which is why the
registry refuses it in any overlay that is not a test's.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import jwt
import pytest

from agent_harness import adapters as wiring
from agent_harness import identity as ident
from agent_harness.adapters import ADAPTERS, OverlayRefused, Wiring
from agent_harness.identity.far_end import SESSION_META, Call, Caller, CallRefused
from agent_harness.identity.local import KID, LocalIssuer

TENANT = "00000000-0000-0000-0000-0000000000aa"
ISSUER = "http://local-issuer.test/realms/claims"
AUDIENCE = "claims-system"
SIGNER = LocalIssuer(url=ISSUER, audience=AUDIENCE)
STRANGER = LocalIssuer(url=ISSUER, audience=AUDIENCE)
"""Another key under the same names: what a forged token is signed with."""
WORKFLOW_ROLE = "payouts.issue"

SETTINGS: dict[str, dict[str, Any]] = {
    "asserted": {},
    "local-dev": {"issuer_url": ISSUER, "audience": AUDIENCE},
    "entra-id": {
        "tenant": TENANT,
        "audience": AUDIENCE,
        "role_scopes": {WORKFLOW_ROLE: ["claims:read", "payouts:write"]},
    },
}


def mint(
    adapter: str,
    *,
    holder: str | None = "PH-1001",
    scopes: tuple[str, ...] = ("claims:read", "claims:write"),
    audience: str = AUDIENCE,
    issuer: str | None = None,
    ttl_s: int = 300,
    key: LocalIssuer = SIGNER,
    workflow: bool = False,
) -> str:
    """A token in the shape this adapter's issuer writes one."""
    now = int(time.time())
    claims: dict[str, Any] = {"sub": "login-1", "aud": audience, "iat": now, "exp": now + ttl_s}
    if holder is not None:
        claims[ident.CLAIM_CUSTOMER] = holder
    if adapter == "entra-id":
        claims |= {"iss": issuer or f"https://login.microsoftonline.com/{TENANT}/v2.0"}
        claims |= {"uti": uuid.uuid4().hex, "azp": "agent-app"}
        if workflow:
            claims["roles"] = [WORKFLOW_ROLE]
        else:
            claims["scp"] = " ".join(scopes)
    else:
        claims |= {"iss": issuer or ISSUER, "jti": uuid.uuid4().hex, "azp": "claims-fnol"}
        claims["scp"] = ["claims:read", "payouts:write"] if workflow else list(scopes)
    return jwt.encode(claims, key._key, algorithm="RS256", headers={"kid": KID})


# (row, how the token is made (None: no token), operation, required scope,
#  the holder a verifying adapter answers or "refused", what `asserted` answers)
ROWS: list[tuple[str, dict[str, Any] | None, str, str | None, str | None, str | None]] = [
    ("a valid read is served", {}, "get_claim", "claims:read", "PH-1001", "PH-1001"),
    ("a valid write is served", {}, "register_claim", "claims:write", "PH-1001", "PH-1001"),
    ("a forged signature is refused", {"key": STRANGER}, "get_claim", None, "refused", "PH-1001"),
    (
        "another audience is refused",
        {"audience": "claims-fnol"},
        "get_claim",
        None,
        "refused",
        "PH-1001",
    ),
    (
        "another issuer is refused",
        {"issuer": "http://elsewhere.test/realms/claims"},
        "get_claim",
        None,
        "refused",
        "PH-1001",
    ),
    ("an expired token is refused", {"ttl_s": -60}, "get_claim", None, "refused", "PH-1001"),
    ("no token is refused", None, "get_claim", None, "refused", "PH-1001"),
    (
        "a write without its scope is refused",
        {"scopes": ("claims:read",)},
        "register_claim",
        "claims:write",
        "refused",
        "PH-1001",
    ),
    ("a token with no holder owns no rows", {"holder": None}, "get_claim", None, None, "PH-1001"),
    (
        "the workflow's login holds payouts:write and no holder",
        {"holder": None, "workflow": True},
        "issue_payout",
        "payouts:write",
        None,
        "PH-1001",
    ),
]


async def authorised(name: str, made: dict[str, Any] | None, operation: str, scope: str | None):
    chosen = ADAPTERS.load("authorise", name)
    filled = {k: SETTINGS[name].get(k, s.default) for k, s in chosen.settings.items()}
    hooks = {"keys": ident.JWKS(SIGNER.jwks)}
    async with chosen.build(Wiring(filled, {}, hooks, "test")) as authorise:
        bearer = None if made is None else mint(name, **made)
        meta = {SESSION_META: {"customer_id": "PH-1001"}}
        return await authorise(Call(operation, scope, bearer, meta))


@pytest.mark.discharges("AAC-0057", "AHC-0099")
@pytest.mark.parametrize("name", sorted(SETTINGS))
@pytest.mark.parametrize(
    ("row", "made", "operation", "scope", "verifying", "believing"), ROWS, ids=[r[0] for r in ROWS]
)
async def test_the_authorise_port(
    name: str,
    row: str,
    made: dict[str, Any] | None,
    operation: str,
    scope: str | None,
    verifying: str | None,
    believing: str | None,
) -> None:
    expected = believing if name == "asserted" else verifying
    if expected == "refused":
        with pytest.raises(CallRefused):
            await authorised(name, made, operation, scope)
        return
    caller = await authorised(name, made, operation, scope)
    assert isinstance(caller, Caller)
    assert caller.holder == expected, row
    if scope is not None:
        assert scope in caller.scopes


@pytest.mark.discharges("AHC-0022")
def test_every_authorise_adapter_is_in_the_table() -> None:
    assert sorted(ADAPTERS.names("authorise")) == sorted(SETTINGS)


PROFILE = Path(__file__).resolve().parents[1] / "harness-profile.yaml"
SECRETS = "  secrets: {adapter: environment-settings, why: tests}\n"

# (row, the overlay's environment, the authorise binding, refused with or None)
OVERLAYS: list[tuple[str, str, str, str | None]] = [
    ("a test overlay may believe the agent", "test", "{adapter: asserted}", None),
    ("a local overlay may not", "local", "{adapter: asserted}", "only a test overlay"),
    ("an azure overlay may not", "azure", "{adapter: asserted}", "only a test overlay"),
    (
        "an azure overlay may verify Entra tokens",
        "azure",
        f"{{adapter: entra-id, tenant: {TENANT}, audience: {AUDIENCE}}}",
        None,
    ),
]


@pytest.mark.discharges("AAC-0057", "AHC-0022")
@pytest.mark.parametrize(
    ("row", "environment", "binding", "refused"), OVERLAYS, ids=[o[0] for o in OVERLAYS]
)
def test_only_a_test_overlay_may_bind_the_believing_adapter(
    tmp_path: Path, row: str, environment: str, binding: str, refused: str | None
) -> None:
    path = tmp_path / f"{environment}.yaml"
    path.write_text(
        f"apiVersion: harness-overlay/v1\nenvironment: {environment}\nprofile: {PROFILE}\n"
        f"bindings:\n{SECRETS}  authorise: {binding}\n"
    )
    if refused is None:
        planned = wiring.plan(path)
        assert planned.adapter("authorise") in binding
        return
    with pytest.raises(OverlayRefused, match=refused):
        wiring.plan(path)


@pytest.mark.discharges("AHC-0022")
async def test_a_far_end_overlay_composes_after_the_agents_ports(tmp_path: Path) -> None:
    path = tmp_path / "test.yaml"
    path.write_text(
        f"apiVersion: harness-overlay/v1\nenvironment: test\nprofile: {PROFILE}\n"
        f"bindings:\n{SECRETS}  authorise: {{adapter: asserted}}\n"
    )
    async with wiring.compose(wiring.plan(path)) as built:
        assert list(built) == ["secrets", "authorise"]
        caller = await built["authorise"](Call("get_claim", meta={SESSION_META: {}}))
    assert caller.holder is None

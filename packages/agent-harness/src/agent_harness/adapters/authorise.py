"""The `authorise` port: how a far end checks the caller (claims-fnol-azure A1).

    asserted    believes the session the agent asserts (`identity.far_end.Asserted`),
                as the AgentTwin world does; bound only in a `test` overlay —
                `plan` refuses it anywhere else
    local-dev   tokens the agent app's local issuer signed for this far end's
                audience, verified with the keys it publishes at `jwks_uri`
    entra-id    Entra v2 access tokens for this far end's own app registration:
                the tenant's issuer and key set, `uti`, `scp` as a string and
                `roles` mapped through `role_scopes`

Each verifying adapter reads the holder from `holder_claim` (default
`customer_id`) and checks the operation's required scope. The hook `keys`
replaces the remote key set with a local one, which is how the contract tests
and an in-process far end run with no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent_harness import identity as ident
from agent_harness.adapters import Adapter, OverlayRefused, Setting, Wiring
from agent_harness.adapters.identity import role_scopes
from agent_harness.identity import far_end


@asynccontextmanager
async def _asserted(wiring: Wiring) -> AsyncIterator[far_end.Authorise]:
    del wiring
    yield far_end.Asserted()


@asynccontextmanager
async def _local(wiring: Wiring) -> AsyncIterator[far_end.Authorise]:
    settings = wiring.settings
    keys: Any = wiring.hooks.get("keys")
    if keys is None:
        if not settings.get("jwks_uri"):
            raise OverlayRefused(
                "authorise local-dev: give `jwks_uri`, where the issuer's keys are"
            )
        keys = ident.RemoteJWKS(str(settings["jwks_uri"]))
    issuer = ident.Issuer(str(settings["issuer_url"]), str(settings["audience"]), keys)
    yield far_end.Verified(issuer, holder_claim=str(settings["holder_claim"]))


@asynccontextmanager
async def _entra(wiring: Wiring) -> AsyncIterator[far_end.Authorise]:
    from agent_harness.identity import entra

    settings = wiring.settings
    issuer = entra.entra_issuer(
        str(settings["tenant"]), str(settings["audience"]), keys=wiring.hooks.get("keys")
    )
    roles = role_scopes(settings.get("role_scopes"))

    def granted(claims: dict[str, Any]) -> list[str]:
        return entra.entra_scopes(claims, roles)

    yield far_end.Verified(
        issuer, session_claim="uti", scopes=granted, holder_claim=str(settings["holder_claim"])
    )


ASSERTED = Adapter("authorise", "asserted", _asserted, environments=("test",))
LOCAL_DEV = Adapter(
    "authorise",
    "local-dev",
    _local,
    {
        "issuer_url": Setting(required=True),
        "audience": Setting(required=True),
        "jwks_uri": Setting(),
        "holder_claim": Setting(default=ident.CLAIM_CUSTOMER),
    },
)
ENTRA_ID = Adapter(
    "authorise",
    "entra-id",
    _entra,
    {
        "tenant": Setting(required=True),
        "audience": Setting(required=True),
        "holder_claim": Setting(default=ident.CLAIM_CUSTOMER),
        "role_scopes": Setting(),
    },
)

__all__ = ["ASSERTED", "ENTRA_ID", "LOCAL_DEV"]

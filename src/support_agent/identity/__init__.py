"""Who the system is acting as.

L16 · P1 and P5. A session token is minted at the edge and carried into every
tool call, where the tool server authorises against it.

The rule that matters: **authorisation is decided from the token, never from
anything the model said.** AAC-0106 — the system prompt is not a security
boundary — and AAC-0057 — least privilege enforced server-side. A model that has
been talked into believing the customer is someone else still cannot act as them,
because the claim it would need never leaves this module.
"""

from __future__ import annotations

import time

import jwt

from support_agent.contracts import Identity

ALGORITHM = "HS256"
ISSUER = "support-agent"

# Scopes are coarse on purpose. A scope per tool becomes a scope nobody audits.
SCOPE_ORDERS_READ = "orders:read"
SCOPE_ORDERS_WRITE = "orders:write"
SCOPE_RETURNS_WRITE = "returns:write"
SCOPE_REFUNDS_WRITE = "refunds:write"

CUSTOMER_SCOPES = frozenset({SCOPE_ORDERS_READ, SCOPE_ORDERS_WRITE, SCOPE_RETURNS_WRITE})
"""What a customer session may do on its own.

`refunds:write` is deliberately absent. A refund is irreversible and above the
approval threshold it needs a human, so the agent acting as the customer must
not hold the scope that would let it skip that gate. The gate is not the only
control; it is the second one.
"""


MIN_SECRET_BYTES = 32
"""RFC 7518 §3.2: an HMAC key for SHA-256 should be at least the hash length.

Enforced rather than warned about, and enforced at mint *and* verify so a short
secret cannot be introduced from either side. This is a configuration fault, not
an authentication failure, so it raises `ValueError` and takes the process down
at startup instead of failing one customer quietly at runtime.
"""


class InvalidSession(Exception):
    """The token is absent, expired, tampered with, or issued by someone else.

    One exception for all of these on purpose: distinguishing them for the caller
    tells an attacker which half of the guess was right.
    """


def _check_secret(secret: str) -> None:
    if len(secret.encode()) < MIN_SECRET_BYTES:
        raise ValueError(
            f"session secret is {len(secret.encode())} bytes; "
            f"at least {MIN_SECRET_BYTES} are required for {ALGORITHM}"
        )


def mint(
    customer_id: str,
    *,
    secret: str,
    scopes: frozenset[str] = CUSTOMER_SCOPES,
    ttl_s: int = 900,
    now: int | None = None,
) -> str:
    _check_secret(secret)
    issued = now if now is not None else int(time.time())
    payload = {
        "sub": customer_id,
        "scp": sorted(scopes),
        "iss": ISSUER,
        "iat": issued,
        "exp": issued + ttl_s,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def verify(token: str, *, secret: str, now: int | None = None) -> Identity:
    """Decode into an `Identity`, or refuse.

    Note what is *not* here: no fallback to an unverified decode, no "trust the
    subject if the signature is missing". A token that does not verify produces
    no identity at all, so there is no partially-trusted path for a caller to
    take by accident.
    """
    _check_secret(secret)
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={
                "require": ["exp", "iat", "sub", "iss"],
                # With an injected clock we check expiry ourselves below, so the
                # library must not compare against the wall clock as well.
                "verify_exp": now is None,
            },
            leeway=0,
        )
        if now is not None and claims["exp"] <= now:
            raise jwt.ExpiredSignatureError("expired")
    except jwt.PyJWTError as exc:
        raise InvalidSession(str(exc)) from exc

    return Identity(
        customer_id=claims["sub"],
        scopes=frozenset(claims.get("scp", [])),
        token=token,
    )


def require(identity: Identity, scope: str) -> None:
    """Assert a scope before an action is attempted.

    Called at the tool boundary, P5 — the last place an action can be stopped
    while stopping it is still cheap.
    """
    if not identity.may(scope):
        raise InvalidSession(f"identity {identity.customer_id!r} lacks scope {scope!r}")


__all__ = [
    "ALGORITHM",
    "MIN_SECRET_BYTES",
    "CUSTOMER_SCOPES",
    "ISSUER",
    "SCOPE_ORDERS_READ",
    "SCOPE_ORDERS_WRITE",
    "SCOPE_REFUNDS_WRITE",
    "SCOPE_RETURNS_WRITE",
    "InvalidSession",
    "mint",
    "require",
    "verify",
]

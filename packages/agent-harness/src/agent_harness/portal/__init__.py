"""The customer's way in when they chat through Chatwoot (T-026).

A Chatwoot webhook names a contact and carries no credential, and the agent acts
only with a customer's own session (T-002). So the customer logs in here, and
this module keeps their login server-side: the refresh token goes into the
session store, the browser gets a signed cookie naming the login and nothing
else, and the page it lands on embeds the Chatwoot widget with the login's `sub`
as the contact identifier and its HMAC as proof.

This is the backend-for-frontend shape the OAuth browser-apps guidance
recommends: no token of any kind reaches the page. Logging out ends the session
at the issuer, deletes the stored login and drops the agent's cached session,
after which a message from that contact cannot be acted on.

Four routes under the mount, and nothing else:

    GET  /           the page, or a redirect to log in
    GET  /login      to the issuer, with PKCE (S256) and a state
    GET  /callback   the code redeemed, the login stored, the cookie set
    POST /logout     ended at the issuer, deleted here, the cookie cleared
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from agent_harness import identity as ident
from agent_harness import telemetry as tel
from agent_harness.contracts import SessionStore, StoredSession

FLOW_COOKIE = "portal_flow"
SESSION_COOKIE = "portal_session"
FLOW_TTL_S = 600
SESSION_TTL_S = 8 * 3600


@dataclass(frozen=True)
class Widget:
    """Which Chatwoot inbox the page embeds. `hmac_token` is the inbox's identity
    validation secret: it signs identifiers here and never reaches the page."""

    base_url: str
    website_token: str
    hmac_token: str = field(repr=False)

    def identity_hash(self, identifier: str) -> str:
        return hmac.new(self.hmac_token.encode(), identifier.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Portal:
    """Everything the routes read, held on the app's state."""

    login: ident.Login
    issuer: ident.Issuer
    sessions: SessionStore
    widget: Widget
    redirect_uri: str
    base_path: str
    cookie_key: bytes = field(repr=False)
    secure_cookies: bool = True
    forget: Callable[[str], None] | None = None
    clock: Callable[[], float] = time.time

    def sign(self, payload: dict[str, Any], ttl_s: int) -> str:
        body = json.dumps({**payload, "exp": int(self.clock()) + ttl_s}).encode()
        encoded = base64.urlsafe_b64encode(body).decode()
        return f"{encoded}.{self._mac(encoded)}"

    def unsign(self, value: str | None) -> dict[str, Any] | None:
        """The payload, or `None` for anything tampered with, expired or absent."""
        if not value or "." not in value:
            return None
        encoded, mac = value.rsplit(".", 1)
        if not hmac.compare_digest(mac, self._mac(encoded)):
            return None
        try:
            payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(encoded))
        except ValueError:
            return None
        return payload if payload.get("exp", 0) > self.clock() else None

    def _mac(self, encoded: str) -> str:
        return hmac.new(self.cookie_key, encoded.encode(), hashlib.sha256).hexdigest()

    def cookie(self, response: Response, name: str, value: str, ttl_s: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=ttl_s,
            path=self.base_path or "/",
            httponly=True,
            secure=self.secure_cookies,
            samesite="lax",
        )


def _portal(request: Request) -> Portal:
    portal: Portal = request.app.state.portal
    return portal


async def page(request: Request) -> Response:
    portal = _portal(request)
    session = portal.unsign(request.cookies.get(SESSION_COOKIE))
    if session is None or await portal.sessions.get(str(session["sub"])) is None:
        return RedirectResponse(f"{portal.base_path}/login", status_code=303)
    subject = str(session["sub"])
    who = {
        "identifier": subject,
        "identifier_hash": portal.widget.identity_hash(subject),
        # Display only: Chatwoot's SDK refuses `setUser` without a name, email
        # or avatar, and a refused call left every widget visitor anonymous
        # (found in the first Codespace, 2026-09-29). The identity is the
        # identifier and its HMAC above; the name proves nothing.
        "name": str(session.get("customer") or subject),
        "base_url": portal.widget.base_url,
        "website_token": portal.widget.website_token,
    }
    page_of: PortalPage = request.app.state.signed_in
    return HTMLResponse(page_of(who=who, logout_path=f"{portal.base_path}/logout"))


async def login(request: Request) -> Response:
    portal = _portal(request)
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    url = portal.login.authorize_url(
        redirect_uri=portal.redirect_uri,
        state=state,
        challenge=challenge.rstrip(b"=").decode(),
    )
    response = RedirectResponse(url, status_code=303)
    flow = portal.sign({"state": state, "verifier": verifier}, FLOW_TTL_S)
    portal.cookie(response, FLOW_COOKIE, flow, FLOW_TTL_S)
    return response


async def callback(request: Request) -> Response:
    """Every refusal is a plain 400 or 403 with a fixed message: the detail of
    why a login failed is for the operator's span, not for whoever is probing."""
    portal = _portal(request)
    flow = portal.unsign(request.cookies.get(FLOW_COOKIE))
    state, code = request.query_params.get("state"), request.query_params.get("code")
    if flow is None or not state or not code or not hmac.compare_digest(state, str(flow["state"])):
        return _refused(400, "the login could not be completed; start again", "state")
    try:
        access, refresh = await portal.login.redeem(
            code, redirect_uri=portal.redirect_uri, verifier=str(flow["verifier"])
        )
        principal = ident.verify(access, issuer=portal.issuer)
    except (ident.SessionEnded, ident.InvalidSession) as exc:
        return _refused(400, "the login could not be completed; start again", type(exc).__name__)
    if not principal.customer_id:
        # A staff login has no customer to chat as, and is not kept.
        await portal.login.end(refresh)
        return _refused(403, "this login is not a customer's", "not a customer")

    now = int(portal.clock())
    await portal.sessions.put(
        StoredSession(subject=principal.subject, refresh_token=refresh, updated_at=now)
    )
    response = RedirectResponse(f"{portal.base_path}/", status_code=303)
    portal.cookie(
        response,
        SESSION_COOKIE,
        portal.sign({"sub": principal.subject, "customer": principal.customer_id}, SESSION_TTL_S),
        SESSION_TTL_S,
    )
    response.delete_cookie(FLOW_COOKIE, path=portal.base_path or "/")
    return response


async def logout(request: Request) -> Response:
    portal = _portal(request)
    session = portal.unsign(request.cookies.get(SESSION_COOKIE))
    if session is not None:
        subject = str(session["sub"])
        stored = await portal.sessions.get(subject)
        if stored is not None:
            # Already ended at the issuer is fine: deleting here is what remains.
            with contextlib.suppress(ident.SessionEnded):
                await portal.login.end(stored.refresh_token)
            await portal.sessions.delete(subject)
        if portal.forget is not None:
            portal.forget(subject)
    response = RedirectResponse(f"{portal.base_path}/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path=portal.base_path or "/")
    return response


def _refused(status: int, message: str, detail: str) -> Response:
    with tel.span("agent.portal.refused", **{"http.refusal_detail": detail}):
        pass
    return PlainTextResponse(message, status_code=status)


class PortalPage(Protocol):
    """The signed-in page, in the agent's own words: who is signed in (the
    widget's identity and the inbox to embed) and where logging out posts."""

    def __call__(self, *, who: dict[str, str], logout_path: str) -> str: ...


def build(portal: Portal, *, signed_in: PortalPage) -> Starlette:
    """The portal app, ready to mount at `portal.base_path`. Wiring only.
    `signed_in` renders the signed-in page; the reference agent's is `ui.portal_page`."""
    app = Starlette(
        routes=[
            Route("/", page),
            Route("/login", login),
            Route("/callback", callback),
            Route("/logout", logout, methods=["POST"]),
        ]
    )
    app.state.portal = portal
    app.state.signed_in = signed_in
    return app


__all__ = ["FLOW_COOKIE", "SESSION_COOKIE", "Portal", "PortalPage", "Widget", "build"]

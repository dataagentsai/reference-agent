"""T-026 (D): the whole path, live. A person logs in, chats through Chatwoot, and
is answered by the agent; escalates and is handed to a person; logs out and is
no longer acted for.

Nothing is faked. The real server runs on port 8077 (where compose's
chatwoot-setup points the bot), the customer logs in through the realm's own
login form, the widget's calls are Chatwoot's own widget API, and the replies
are read back from Chatwoot. Skips unless Keycloak and Chatwoot are both up
(`docker compose --profile channel up -d`) and the port is free.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REALM = "http://localhost:8080/realms/support"
CHATWOOT = "http://localhost:3100"
AGENT = "http://localhost:8077"
BOT_TOKEN = "localdevonlybottoken"
DESK_TOKEN = "localdevonlydesktoken"
"""A person at the desk: reads conversations and resolves them. The bot's token
may post and hand off, and may not read a conversation back."""
WEBSITE = "localdevonlywebsitetoken"

ENV = {
    "AGENT_ISSUER_URL": REALM,
    "AGENT_PORTAL_CLIENT_SECRET": "local-dev-only-portal-secret",
    "AGENT_PORTAL_COOKIE_KEY": "live-test-portal-cookie-key",
    "AGENT_PORTAL_REDIRECT_URI": f"{AGENT}/portal/callback",
    "AGENT_CHATWOOT_BASE_URL": CHATWOOT,
    "AGENT_CHATWOOT_WEBSITE_TOKEN": WEBSITE,
    "AGENT_CHATWOOT_HMAC_TOKEN": "local-dev-only-inbox-identity-secret",
    "AGENT_CHATWOOT_BOT_SECRET": "local-dev-only-bot-webhook-secret",
    "AGENT_CHATWOOT_BOT_TOKEN": BOT_TOKEN,
}


def _up(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=3)
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False
    return True


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture(scope="module")
def server():
    if not (_up(f"{REALM}/.well-known/openid-configuration") and _up(f"{CHATWOOT}/api")):
        pytest.skip("Keycloak and Chatwoot are not both running")
    if not _free(8077):
        pytest.skip("port 8077 is in use")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "scripts" / "run_server.py"), "--port", "8077"],
        env={**os.environ, **ENV},
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        if _up(f"{AGENT}/healthz"):
            break
        time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("the server did not start:\n" + proc.stdout.read().decode()[-2000:])
    yield
    proc.terminate()
    proc.wait(timeout=10)


class _Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _open(opener, url: str, data: dict | None = None, headers: dict | None = None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, response.headers, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read().decode()


def portal_login(username: str):
    """The browser: /portal/login, the realm's form, back to the callback. Returns
    the opener holding the portal's cookie, and the widget identity on the page."""
    jar = http.cookiejar.CookieJar()
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _Stay)
    _, headers, _ = _open(browser, f"{AGENT}/portal/login")
    with urllib.request.urlopen(headers["Location"], timeout=10) as page:
        realm_cookies = "; ".join(c.split(";")[0] for c in page.headers.get_all("Set-Cookie"))
        form = page.read().decode()
    action = (
        re.search(r'id="kc-form-login"[^>]*action="([^"]+)"', form).group(1).replace("&amp;", "&")
    )
    realm = urllib.request.build_opener(_Stay)
    _, back, _ = _open(
        realm,
        action,
        {"username": username, "password": "local-dev-only"},
        {"Cookie": realm_cookies},
    )
    status, _, _ = _open(browser, back["Location"])
    assert status == 303, "the callback kept the login"
    status, _, html = _open(browser, f"{AGENT}/portal/")
    assert status == 200
    who = json.loads(re.search(r"const who = (\{.*?\});", html).group(1).replace("\\u003c", "<"))
    return browser, who


def _widget(method: str, path: str, body: dict, auth: str | None) -> dict:
    request = urllib.request.Request(
        f"{CHATWOOT}{path}?website_token={WEBSITE}",
        method=method,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **({"X-Auth-Token": auth} if auth else {})},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read() or b"{}")


def widget_session(who: dict) -> str:
    """The widget loads and tells Chatwoot who the portal said the contact is."""
    token = _widget("POST", "/api/v1/widget/config", {}, None)["website_channel_config"][
        "auth_token"
    ]
    user = {"identifier": who["identifier"], "identifier_hash": who["identifier_hash"]}
    return _widget("PATCH", "/api/v1/widget/contact/set_user", user, token).get(
        "widget_auth_token", token
    )


def widget_opens(token: str) -> None:
    """What the embedded widget sends when the customer opens it."""
    _widget(
        "POST", "/api/v1/widget/events", {"name": "webwidget.triggered", "event_info": {}}, token
    )


def widget_says(who: dict, text: str, token: str | None = None) -> tuple[str, int]:
    """Chatwoot's widget API, as the embedded widget calls it."""

    def call(method: str, path: str, body: dict, auth: str | None) -> dict:
        request = urllib.request.Request(
            f"{CHATWOOT}{path}?website_token={WEBSITE}",
            method=method,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                **({"X-Auth-Token": auth} if auth else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read() or b"{}")

    if token is None:
        token = call("POST", "/api/v1/widget/config", {}, None)["website_channel_config"][
            "auth_token"
        ]
        user = {"identifier": who["identifier"], "identifier_hash": who["identifier_hash"]}
        token = call("PATCH", "/api/v1/widget/contact/set_user", user, token).get(
            "widget_auth_token", token
        )
    sent = call("POST", "/api/v1/widget/messages", {"message": {"content": text}}, token)
    return token, int(sent["conversation_id"])


def conversation(cid: int) -> tuple[str, list[dict]]:
    request = urllib.request.Request(
        f"{CHATWOOT}/api/v1/accounts/1/conversations/{cid}/messages",
        headers={"api_access_token": DESK_TOKEN},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        messages = json.load(response)["payload"]
    request = urllib.request.Request(
        f"{CHATWOOT}/api/v1/accounts/1/conversations/{cid}",
        headers={"api_access_token": DESK_TOKEN},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        status = json.load(response)["status"]
    return status, messages


def wait_for(cid: int, predicate, seconds: float = 30) -> tuple[str, list[dict]]:
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = conversation(cid)
        if predicate(*state):
            return state
        time.sleep(1)
    return conversation(cid)


def desk(method: str, path: str, body: dict | None = None) -> dict:
    """A person at the desk, through Chatwoot's API."""
    request = urllib.request.Request(
        f"{CHATWOOT}/api/v1/accounts/1{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "api_access_token": DESK_TOKEN},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read() or b"{}")


def close_everything_for(identifier: str) -> None:
    """Start clean: a conversation left open by an earlier run belongs to a person,
    and a message sent into it would never reach the bot."""
    listed = desk("GET", "/conversations?status=all")["data"]["payload"]
    for row in listed:
        if row["meta"]["sender"].get("identifier") == identifier and row["status"] != "resolved":
            desk("POST", f"/conversations/{row['id']}/toggle_status", {"status": "resolved"})


def outgoing(messages: list[dict], *, private: bool = False) -> list[str]:
    return [
        m["content"]
        for m in messages
        if m["message_type"] == 1 and bool(m.get("private")) is private
    ]


@pytest.mark.discharges("AHC-0099", "AAC-0111", "AHC-0070", "P-OWNERSHIP")
def test_a_customer_logs_in_chats_escalates_and_logs_out(server) -> None:
    browser, who = portal_login("c-1042")
    close_everything_for(who["identifier"])

    # T-001: opening the widget, before a word is typed, shows the orders.
    token = widget_session(who)
    widget_opens(token)
    greeted = None
    for _ in range(30):
        listed = desk("GET", "/conversations?status=all")["data"]["payload"]
        mine = [
            c
            for c in listed
            if c["meta"]["sender"].get("identifier") == who["identifier"]
            and c["status"] == "pending"
        ]
        if mine:
            _, shown = conversation(mine[0]["id"])
            if any("AB-10003: delivered" in text for text in outgoing(shown)):
                greeted = mine[0]["id"]
                break
        time.sleep(1)
    assert greeted is not None, "opening the widget showed the customer their orders"

    token, cid = widget_says(who, "where is my order AB-10003", token)
    assert cid == greeted, "the customer's first message lands in the greeted conversation"
    _, messages = wait_for(cid, lambda s, m: any("AB-10003" in t for t in outgoing(m)))
    assert any("AB-10003" in t for t in outgoing(messages)), "the agent answered through Chatwoot"

    widget_says(who, "put me through to a human", token)
    status, messages = wait_for(cid, lambda s, m: s == "open")
    assert status == "open", "the conversation is handed to a person"
    assert any("put me through to a human" in t for t in outgoing(messages, private=True))

    # A person closes it, so the next message starts a conversation the bot holds.
    desk("POST", f"/conversations/{cid}/toggle_status", {"status": "resolved"})

    assert _open(browser, f"{AGENT}/portal/logout", {})[0] == 303
    _, later = widget_says(who, "where is my order AB-10002")
    _, messages = wait_for(later, lambda s, m: any("sign in" in t.lower() for t in outgoing(m)))
    assert any("sign in again" in t.lower() for t in outgoing(messages)), (
        "logged out, not acted for"
    )

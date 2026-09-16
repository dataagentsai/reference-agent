"""The chat page — one file, no build step.

Served as a string rather than from disk so the agent stays a single installable
package with nothing to copy alongside it. Small enough that a template engine
would be more machinery than page.

Three things here exist because of what the harness needs, not because of how
the page looks:

**A delivery id per send.** The browser mints one and puts it in
`Idempotency-Key`. That is the value AAC-0076's guard checks, and R-017 recorded
that nothing produced one — this is the thing that now does. A retry after a
timeout reuses the same id on purpose, so pressing send twice on a slow network
cannot cancel an order twice.

**The conversation id is kept and echoed.** The first reply carries one; every
later send returns it. That is the handle F-006 made loadable.

**The token is never assembled here.** The page sends whatever token it was
given. A page that could mint its own identity would make the whole scope system
decorative.
"""

from __future__ import annotations

import html
import json

CHAT_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Support</title>
<style>
:root{
  --ground:#F4F6F8; --surface:#FFFFFF; --sunk:#EBEEF2;
  --ink:#141A21; --ink-2:#48545F; --ink-3:#78838D;
  --rule:#DCE1E7; --accent:#2A5A8C; --no:#A6462B; --warn:#8A6608;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,Arial,
    sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#0F1418; --surface:#161C22; --sunk:#111820;
  --ink:#E4E9EE; --ink-2:#A2AEB9; --ink-3:#76828D;
  --rule:#26303A; --accent:#7FB0DC; --no:#DE8E76; --warn:#D6AC4B;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font:15px/1.6 var(--sans);
  display:flex;flex-direction:column;height:100vh}
header{padding:16px 22px;border-bottom:1px solid var(--rule);display:flex;
  align-items:baseline;gap:14px;flex-wrap:wrap}
h1{font-size:16px;font-weight:600;margin:0}
.cid{font:12px var(--mono);color:var(--ink-3)}
#log{flex:1;overflow-y:auto;padding:22px;display:flex;flex-direction:column;gap:14px}
.turn{max-width:62ch;padding:12px 16px;border-radius:12px;white-space:pre-wrap;
  word-break:break-word}
.me{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:4px}
.them{align-self:flex-start;background:var(--surface);border:1px solid var(--rule);
  border-bottom-left-radius:4px}
.meta{font:11px var(--mono);color:var(--ink-3);margin-top:6px}
.them.refused .meta{color:var(--no)}
.them.needsapproval .meta{color:var(--warn)}
.sys{align-self:center;font:12px var(--mono);color:var(--ink-3);background:var(--sunk);
  padding:6px 12px;border-radius:20px}
form{display:flex;gap:10px;padding:16px 22px;border-top:1px solid var(--rule);
  background:var(--surface)}
input{flex:1;padding:12px 14px;border:1px solid var(--rule);border-radius:9px;
  background:var(--ground);color:var(--ink);font:15px var(--sans)}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{padding:12px 20px;border:0;border-radius:9px;background:var(--accent);color:#fff;
  font:600 15px var(--sans);cursor:pointer}
button:disabled{opacity:.5;cursor:default}
</style></head><body>

<header>
  <h1>Support</h1>
  <span class="cid" id="cid">new conversation</span>
</header>

<div id="log" aria-live="polite"></div>

<form id="f" autocomplete="off">
  <input id="t" placeholder="Ask about an order — try: please cancel my order AB-10002"
         aria-label="Your message" autofocus>
  <button id="send">Send</button>
</form>

<script>
// The token the page was issued. It is opaque here on purpose: the page cannot
// mint one, and the server verifies it before doing anything at all.
const TOKEN = new URLSearchParams(location.search).get("token") || "";

let conversationId = null;
let pending = null;   // the delivery id of an in-flight send

const log = document.getElementById("log");
const form = document.getElementById("f");
const input = document.getElementById("t");
const send = document.getElementById("send");
const cid = document.getElementById("cid");

function bubble(cls, text, meta){
  const el = document.createElement("div");
  el.className = "turn " + cls;
  el.textContent = text;
  if (meta){
    const m = document.createElement("div");
    m.className = "meta";
    m.textContent = meta;
    el.appendChild(m);
  }
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function system(text){
  const el = document.createElement("div");
  el.className = "sys";
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

form.addEventListener("submit", async e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;

  // One delivery id per *message*, not per attempt. A retry after a timeout
  // reuses it deliberately — that is what makes pressing send twice on a bad
  // connection safe rather than a second cancellation.
  const delivery = pending || (crypto.randomUUID ? crypto.randomUUID()
                                                 : String(Date.now()) + Math.random());
  pending = delivery;

  bubble("me", text);
  input.value = "";
  input.disabled = send.disabled = true;

  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "authorization": "Bearer " + TOKEN,
        "idempotency-key": delivery,
      },
      body: JSON.stringify({ text, conversation_id: conversationId }),
    });
    const data = await res.json();

    if (data.status === "already handled"){
      system("already handled — not sent twice");
    } else if (data.error){
      bubble("them", data.error, "HTTP " + res.status);
    } else {
      conversationId = data.conversation_id || conversationId;
      cid.textContent = conversationId || "new conversation";
      bubble("them " + (data.outcome || ""), data.reply || "(no reply)",
             data.outcome + " · HTTP " + res.status);
    }
    pending = null;                       // delivered; the next send is new
  } catch (err) {
    // The id is deliberately NOT cleared: we do not know whether the server
    // handled it. Re-sending the same id is the only safe retry.
    bubble("them", "Could not reach support. Sending again will not duplicate anything.",
           "network error");
  } finally {
    input.disabled = send.disabled = false;
    input.focus();
  }
});
</script>
</body></html>
"""


PORTAL_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Support</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 2rem 1rem; color: #222; }
  main { max-width: 36rem; margin: 0 auto; }
  form { display: inline; }
  button { font: inherit; padding: .4rem .9rem; }
</style>
</head><body>
<main>
  <h1>Support</h1>
  <p>You are signed in. Open the chat in the corner to talk to us.</p>
  <form method="post" action="__LOGOUT__"><button type="submit">Sign out</button></form>
</main>
<script>
  // The identity the widget presents is decided on the server. The hash is an
  // HMAC the server computed with the inbox's secret, which never reaches this
  // page, so a visitor cannot claim someone else's identifier (T-026).
  const who = __WHO__;
  window.chatwootSettings = { hideMessageBubble: false, position: "right" };
  window.addEventListener("chatwoot:ready", () => {
    window.$chatwoot.setUser(who.identifier, { identifier_hash: who.identifier_hash });
  });
  (function (d, t) {
    const base = who.base_url;
    const g = d.createElement(t), s = d.getElementsByTagName(t)[0];
    g.src = base + "/packs/js/sdk.js"; g.async = true;
    s.parentNode.insertBefore(g, s);
    g.onload = () => window.chatwootSDK.run({ websiteToken: who.website_token, baseUrl: base });
  })(document, "script");
</script>
</body></html>
"""
"""The customer's page once signed in: the Chatwoot widget, told who they are.

The portal fills `__WHO__` with JSON and `__LOGOUT__` with the sign-out path. No
token of any kind is placed on the page: the session is a cookie the browser
cannot read, and the widget's identity is an identifier and its HMAC.
"""


def portal_page(*, who: dict[str, str], logout_path: str) -> str:
    """The page with its values in. JSON for the script, escaped so no value can
    close the script tag it sits in."""
    data = json.dumps(who).replace("<", "\\u003c")
    return PORTAL_PAGE.replace("__WHO__", data).replace("__LOGOUT__", html.escape(logout_path))


__all__ = ["CHAT_PAGE", "PORTAL_PAGE", "portal_page"]

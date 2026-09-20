"""One page for the desk: both queues, and the buttons that decide them (T-059).

A person working the desk should not need Swagger or `curl`. This is the
smallest page that lets them work: what is waiting, how long it has waited,
what it is for, and two buttons — served by the same app that serves the API,
and calling nothing but that API.

**The token comes in the query string**, as the chat page's does, and is kept
only in the page's memory. That is honest for a demo signed by a process-local
issuer and is not how a deployment would do it: there the desk sits behind the
same login as everything else, and this page reads its session cookie. The
routes do not care — they read a bearer token either way.

No framework, no build step, no dependency. A desk that needs a toolchain to
change a label is a desk nobody changes.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Support desk</title>
<style>
  :root { color-scheme: light dark;
    --ink:#16181d; --dim:#5c6270; --line:#dfe3ea; --card:#fff; --bg:#f6f7f9;
    --accent:#2d6cdf; --warn:#b4530a; --bad:#b3261e; --ok:#166534; }
  @media (prefers-color-scheme: dark) { :root {
    --ink:#e8eaf0; --dim:#9aa2b1; --line:#2b2f3a; --card:#171a21; --bg:#0f1115;
    --accent:#7aa6ff; --warn:#e0a458; --bad:#ef6b62; --ok:#6ee7a8; } }
  * { box-sizing:border-box }
  body { margin:0; padding:24px 16px 64px; background:var(--bg); color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  main { max-width:900px; margin:0 auto }
  h1 { font-size:20px; margin:0 0 4px }
  h2 { font-size:15px; margin:28px 0 8px; letter-spacing:.02em }
  p.note { color:var(--dim); margin:0 0 16px; font-size:13px }
  .row { background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:12px 14px; margin-bottom:10px }
  .head { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap }
  .id { font:12px ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--dim) }
  .what { font-weight:600 }
  .waited { margin-left:auto; color:var(--warn); font-size:13px }
  .why { color:var(--dim); font-size:13px; margin:6px 0 10px }
  button { font:inherit; padding:6px 14px; border-radius:8px; border:1px solid var(--line);
    background:transparent; color:var(--ink); cursor:pointer; margin-right:8px }
  button.go { border-color:var(--accent); color:var(--accent) }
  button.no { border-color:var(--bad); color:var(--bad) }
  button:disabled { opacity:.45; cursor:default }
  .empty { color:var(--dim); font-size:14px; padding:6px 0 }
  .said { margin-top:8px; font-size:13px }
  .said.bad { color:var(--bad) }
  .said.ok { color:var(--ok) }
  footer { margin-top:32px; color:var(--dim); font-size:12px }
</style>
<main>
  <h1>Support desk</h1>
  <p class="note" id="who">Reading…</p>

  <h2>Waiting for a decision</h2>
  <div id="approvals"><p class="empty">Reading…</p></div>

  <h2>Handed to a person</h2>
  <div id="escalations"><p class="empty">Reading…</p></div>

  <footer>
    Refreshes every 5 seconds. A decision is taken by the workflow, under its own
    login; this page only tells it what you decided.
  </footer>
</main>
<script>
const token = new URLSearchParams(location.search).get("token") || "";
const head = { "authorization": "Bearer " + token, "content-type": "application/json" };
const ago = s => s < 60 ? s + "s" : s < 3600 ? Math.floor(s/60) + "m" : Math.floor(s/3600) + "h";
const esc = t => String(t).replace(/[&<>"']/g, c =>
  ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));

async function call(path, options) {
  const answered = await fetch(path, { headers: head, ...options });
  const body = await answered.json().catch(() => ({}));
  if (!answered.ok) throw new Error(body.detail || ("HTTP " + answered.status));
  return body;
}

function say(id, message, good) {
  const row = document.getElementById(id);
  if (!row) return;
  const line = row.querySelector(".said") || Object.assign(document.createElement("div"), {});
  line.className = "said " + (good ? "ok" : "bad");
  line.textContent = message;
  row.appendChild(line);
}

function approval(row) {
  const what = row.args && row.args.order_id
    ? row.action + " · " + row.args.order_id + (row.args.amount ? " · " + row.args.amount : "")
    : row.action;
  return `<div class="row" id="${esc(row.id)}">
    <div class="head"><span class="what">${esc(what)}</span>
      <span class="id">${esc(row.id)}</span>
      <span class="waited">waiting ${ago(row.waiting_s)}</span></div>
    <div class="why">${esc(row.reason || "")} — for ${esc(row.customer_id)}</div>
    <button class="go" onclick="decide('${esc(row.id)}', true, this)">Approve</button>
    <button class="no" onclick="decide('${esc(row.id)}', false, this)">Refuse</button>
  </div>`;
}

function escalation(row) {
  return `<div class="row" id="${esc(row.id)}">
    <div class="head"><span class="what">${esc(row.reason || row.rule_id)}</span>
      <span class="id">${esc(row.id)}</span>
      <span class="waited">waiting ${ago(row.waiting_s)}</span></div>
    <div class="why">${esc(row.customer_id)} · conversation ${esc(row.conversation_id)}</div>
    <button class="go" onclick="close_it('${esc(row.id)}', 'resolved', this)">Handled</button>
    <button onclick="close_it('${esc(row.id)}', 'not_needed', this)">Agent could have</button>
  </div>`;
}

async function decide(id, granted, button) {
  button.parentElement.querySelectorAll("button").forEach(b => b.disabled = true);
  try {
    const done = await call(`/ops/approvals/${id}/decide`, {
      method: "POST", body: JSON.stringify({ granted })
    });
    say(id, (granted ? "Approved. " : "Refused. ") + (done.result || ""), true);
  } catch (wrong) {
    say(id, String(wrong.message), false);
    button.parentElement.querySelectorAll("button").forEach(b => b.disabled = false);
  }
  setTimeout(refresh, 900);
}

async function close_it(id, outcome, button) {
  button.parentElement.querySelectorAll("button").forEach(b => b.disabled = true);
  try {
    await call(`/ops/escalations/${id}/resolve`, {
      method: "POST", body: JSON.stringify({ outcome })
    });
    say(id, "Closed as " + outcome + ".", true);
  } catch (wrong) {
    say(id, String(wrong.message), false);
    button.parentElement.querySelectorAll("button").forEach(b => b.disabled = false);
  }
  setTimeout(refresh, 900);
}

async function one(path, into, render, nothing) {
  const box = document.getElementById(into);
  try {
    const rows = await call(path);
    box.innerHTML = rows.length ? rows.map(render).join("") : `<p class="empty">${nothing}</p>`;
  } catch (wrong) {
    box.innerHTML = `<p class="empty">${esc(wrong.message)}</p>`;
  }
}

async function refresh() {
  document.getElementById("who").textContent = token
    ? "Signed in with the token in this link."
    : "No token in the link — add ?token=… to see the queues.";
  await Promise.all([
    one("/ops/approvals", "approvals", approval, "Nothing waiting for a decision."),
    one("/ops/escalations", "escalations", escalation, "Nobody is waiting for a colleague."),
  ]);
}

refresh();
setInterval(refresh, 5000);
</script>
</html>
"""


@router.get("/desk", response_class=HTMLResponse, include_in_schema=False)
async def desk() -> HTMLResponse:
    """The page itself. Unauthenticated, like the chat page: it carries no data,
    and every call it makes is authorised on its own."""
    return HTMLResponse(PAGE)


__all__ = ["PAGE", "router"]

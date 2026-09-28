# One message, start to end — and what could be swapped

What happens from the moment a customer types in the chat widget until they read
the reply, in order. For each step: what it does in plain English, the class or
function that does it, **whether it is injected** (IoC — handed in when the
agent is built, so another implementation can replace it), and **whether it is
domain-agnostic** (a hotel agent would keep it) or not.

Traced from the code on 2026-09-29. The path is the Chatwoot one; the plain
`/chat` page enters at step 5 instead.

**Legend**

| Mark | Meaning |
|---|---|
| **IoC ✔** | injected: passed to `entrypoint.build` (or the channel) as an object behind a Protocol, a rule list or a value |
| **IoC ◐** | injectable in principle, but the default is baked in and `build` does not pass it through |
| **IoC ✘** | called directly; replacing it means editing the code |
| **Agnostic** | no shop knowledge; a hotel agent keeps it unchanged |
| **Param** | generic code with this shop's values inside it; a hotel agent keeps the code and must change the values |
| **Shop** | exists because this is a clothing shop; a hotel agent writes its own |

---

## A · The message arrives

| # | What happens | Where | IoC | Domain |
|---|---|---|---|---|
| 1 | Chatwoot posts the message to the agent's webhook. The agent checks the **signature** (an HMAC under a shared secret) and refuses anything unsigned or older than five minutes. | `channel.Channel.signed` · `channel/__init__.py` | ✔ `Channel` is built and mounted by the server | Agnostic |
| 2 | It keeps only an **incoming, public message in a conversation the bot still holds**; everything else (its own replies, staff messages, a conversation a person has taken) is acknowledged and ignored. It answers Chatwoot at once (202) and does the work in the background, because a model turn can outlast Chatwoot's timeout. | `channel.webhook`, `channel.incoming` | ✘ | Agnostic (Chatwoot-specific, not shop-specific) |

## B · Who is this, and is it their conversation?

| # | What happens | Where | IoC | Domain |
|---|---|---|---|---|
| 3 | **Who is it?** The contact must be HMAC-verified by the portal, and their portal login must still be live. Otherwise the reply is "please sign in". | `channel._customer` → `Sessions.identity_for` | ✔ `Sessions` Protocol | Agnostic |
| 4 | **Load the conversation** by Chatwoot's ids, and refuse it if it belongs to a different customer. | `channel._conversation` → `CheckpointStore.latest` | ✔ `CheckpointStore` Protocol (memory · file · Postgres) | Agnostic |

## C · The turn begins

| # | What happens | Where | IoC | Domain |
|---|---|---|---|---|
| 5 | **Duplicate guard.** If Chatwoot delivers the same message twice, the second is answered from the first instead of running again. | `Agent.handle` → `requests.once` | ✔ `Requests` Protocol | Agnostic |
| 6 | Open the turn's trace span and stamp it: run id, customer, whether this is the synthetic canary customer. | `Agent._turn`, `ending.opened`, `telemetry.span` | ✘ telemetry is process-global | Agnostic |
| 7 | **Is a person holding this conversation?** If an escalation is open, the bot stays silent and says so. | `Agent._gates` → `HandoffDesk.hold` | ✔ built from the injected `Escalations`; `NoDesk` null object when none | Agnostic |
| 8 | **Is an approval pending?** If so, resume it: say it is still waiting, or report what was decided. | `Agent._gates` → `ApprovalFlow.resume` | ✔ built from injected `Approvals`; `NoApprovals` null object | Param (the refund replies) |

## D · What does the customer want?

| # | What happens | Where | IoC | Domain |
|---|---|---|---|---|
| 9 | **The router** decides one of four routes from the words alone, with no model: **refuse**, **hand to a person**, **answer directly**, or **send to the AI loop**. | `router.route(text, rules=…)` | ✔ the `Rules` object is injected — but its defaults are this shop's regexes, written in the router module | **Param** |
| 10 | Record the customer's own words as a fact, before anything runs, so even a failed turn leaves a record of what was asked. | `Facts.asking` | ✘ | Agnostic |
| 11 | **Build the permission list**: which action on which order the customer's words ask for (see F-061 for how this goes wrong). | `consent.granting` | **✘ called directly** | **Shop** — names `cancel_order`, `open_return_request`, `change_address` in code |

## E · The four routes

| # | Route | What happens | Where | IoC | Domain |
|---|---|---|---|---|---|
| 12 | Refuse | A fixed refusal ("I can't offer discounts…"). No model, no tools. | `router.refusal_text` | ✔ via `Rules` | Param |
| 13 | Hand to a person | Start an escalation, tell the customer their reference and wait. | `HandoffDesk.raise_requested` → `Escalations` (Temporal) | ✔ | Agnostic engine · Shop wording (`escalation/wording.py`) |
| 14 | Answer directly | Order status, refund status, "list my orders": one shop lookup, a template reply. | `direct.answer` → `HANDLERS` registry | **◐** `answer` takes `handlers=`, but `Agent` never passes it — the registry is fixed | **Shop** |
| 15 | AI loop | Everything else — steps F and G. | `loop.run` | ✘ the loop is called directly (it is the harness's own; LangGraph would replace it here) | Agnostic |

## F · The AI loop (repeats until it answers or hits a limit)

| # | What happens | Where | IoC | Domain |
|---|---|---|---|---|
| 16 | **Open the tool surface**: ask the shop's MCP server which tools this customer may use, and add the agent's own local tools (the refund request). | `ToolClient.list_tools` · `ApprovalFlow.offer` → `refund_tool` | ✔ `ToolClient` Protocol · local tools offered by the injected approvals | Agnostic surface · **Shop** refund tool |
| 17 | **Check the limits** before each step: step count, turn deadline, cost ceiling. | `_Run.step` with `Budgets` | ✔ budgets from injected config | Agnostic |
| 18 | **Assemble the context**: instructions, history, fenced untrusted text; trim the middle of a long conversation to fit. | `context.assembled` | **✘ called directly** — the strategy is not pluggable yet | Agnostic |
| 19 | **Before-model guardrail** (a hook): the last point where refusing costs nothing. | `Screen.at(PRE_MODEL)` | ✔ `policy_rules` per position, injected | Agnostic (no rules there by default) |
| 20 | **Call the model** with the context and the tool list. Retries, backoff and the circuit breaker wrap the provider client, exactly as production wraps it. | `LLMClient.complete` = `ResilientLLM(GroqClient)` → LiteLLM → Groq | ✔ `LLMClient` Protocol (Groq · scripted · the provider twin) | Agnostic |
| 21 | **Count tokens and money**; stop if the cost ceiling is passed. | `spend.account` with `Meter` | ✔ `metering` factory injected | Agnostic (the price table is config) |
| 22 | **If the model answered in words:** the after-model guardrail checks the reply (e.g. it may not claim an effect no tool confirmed). Done → go to H. | `_Run._answer` → `Screen.at(POST_MODEL)` | ✔ hook injected — defaults are `OUTPUT_RULES` | **Param** — the claim sentences are this shop's |
| 23 | **If the model asked for tools:** give each call an idempotency key, and stop if the model is going round in circles. | `_Run._plan`, `plan.Keys`, `plan.circling` | ✘ | Agnostic |
| 24 | **Freshness**: an irreversible action on an order last read more than 30 s ago is not run; the order is re-read and the model plans again. | `freshness.refresh` | ✔ the window is injected (`fresh_for_s`) | Agnostic mechanism · value per agent |
| 25 | **Before-tool guardrail** (a hook): the permission list — was this action on this order asked for? | `Screen.permitted` → `policy.customer_asked` | ✔ hook injected | **Shop** — reads step 11's list |
| 26 | **Run the allowed calls**, several at once up to a limit. | `dispatch` → `ToolClient.call` or a `LocalTool` | ✔ | Agnostic |
| 27 | **If the refund tool asks for a person**, the loop stops and returns "waiting for approval" (see *The refund* below). | `ApprovalRequested` → `NeedsApproval` | ✔ | Agnostic signal · Shop tool |
| 28 | **After-tool guardrail** (a hook) on each result; results go back into the context, and the loop takes another step (→ 17). | `Screen.admitted(POST_TOOL)` | ✔ hook injected | Agnostic |

## G · At the shop (inside step 26)

| # | What happens | Where | IoC | Domain |
|---|---|---|---|---|
| 29 | The call crosses MCP to the **order system's own server**, carrying the customer's token and the idempotency key. | `tools.mcp.MCPToolClient` | ✔ it is the `ToolClient` | Agnostic |
| 30 | The order system **checks everything again itself**: the token, that the order is the caller's, the operation's preconditions (e.g. delivered, within 30 days), the key. The agent's checks are not trusted. | `order_system/server.py` | ✔ chosen at startup: projected world or real store | Agnostic (rules come from the AOAS) |
| 31 | It reads or writes the order in **Saleor** (or in the simulated world). | `order_system/store.py` `Saleor` · AgentTwin projection | ✔ `Store` | Agnostic (the Saleor binding is store-product-specific, not shop logic) |

## H · After the work

| # | What happens | Where | IoC | Domain |
|---|---|---|---|---|
| 32 | **Does this conversation now need a person?** Tier-2 rules look at how the turn *went*: refused twice, the loop gave up, the same question a third time. | `HandoffDesk.raise_on_condition` with the `tier_2` rule set | ✔ `tier_2` injected | **Param** — the conditions |
| 33 | Update the facts (what landed, what is awaited), and carry any refused action forward as "awaiting confirmation". | `facts.after`, `consent.pending` | ✘ | Agnostic · **Shop** (consent) |
| 34 | **Promise gate**: a reply that promises follow-up ("let me check") with nothing behind it is turned into a real handoff or corrected. | `promise.honest` | **✘ called directly** | **Param** — the promise phrases are English and this shop's voice |
| 35 | **Final reply guardrail**, on every route — so a template edit anywhere is screened too. | `_screened` with `policy_rules` | ✔ hooks injected | Param (claim patterns) |
| 36 | Close the turn span with the outcome, route and duration. | `ending.closed` | ✘ | Agnostic |
| 37 | **Save the conversation** (trimmed to its size bound). | `TurnPersister` → `CheckpointStore.put` | ✔ | Agnostic |

## I · The reply goes out, and the watchers read it later

| # | What happens | Where | IoC | Domain |
|---|---|---|---|---|
| 38 | Post the reply in Chatwoot. If the turn escalated, also post a **private note** briefing the colleague and hand the conversation to a person. If it is waiting for approval, post a note pointing at the desk. | `ChatwootApi.reply / note / hand_off` | ✔ `ChatwootApi` Protocol | Agnostic |
| 39 | The spans leave the process through OpenTelemetry → the collector → **Langfuse** (traces) and **Prometheus** (metrics). | `telemetry.configure` | ✘ set once per process | Agnostic |
| 40 | Every minute, **the watcher** reads finished turns back from Langfuse, runs 22 rules and writes scores back. Every ten minutes, **the canary** runs four checks as a synthetic customer. | `watch.rules`, `watch.canary` | ✔ rules are data | **Param** (rules' vocabulary) · **Shop** (canary cases) |

---

## The refund, as its own path (from step 27)

| # | What happens | Where | IoC | Domain |
|---|---|---|---|---|
| R1 | The model asks for `request_refund(order_id)`. It **cannot** issue a refund: that tool is not offered to it. | `approvals/refund.py` `refund_tool` | ✔ offered by injected approvals | **Shop** |
| R2 | The tool asks the approval service for a decision. | `TemporalApprovals.request` · `approvals/desk.py` | ✔ `Approvals` Protocol (memory · Temporal) | Agnostic |
| R3 | A **workflow** starts in Temporal, pinned to the approval noticeboard. | `ApprovalWorkflow` · `approvals/durable.py` | ✔ | Agnostic |
| R4 | **Assess**: read the order and decide who must approve — the automatic rule (returned and ≤ ₹10,000) or a person. Record what the decision was based on. | `RefundWork.assess` + `approvals.Policy` | ✔ `Policy` is a versioned rule set | **Shop** (assess) · **Param** (the limit) |
| R5 | Wait for the decision, with a reminder and a 24-hour expiry. | `ApprovalWorkflow` timers | ✔ | Agnostic |
| R6 | **Carry out**: re-read the order, stop if it changed, then refund under the approval worker's own login. | `RefundWork.carry_out` | ✔ | **Shop** |

---

## Scorecard

Counted from the tables above (46 steps). Some steps are mixed — a generic
engine with the shop's wording beside it — so the domain rows overlap.

| | Steps |
|---|---|
| **Injected (IoC ✔)** | 34 |
| Injectable but not wired through (◐) | 1 — the direct-answer registry (14) |
| **Called directly (✘)** | 11 — of which **3 matter for a second domain**: the permission list (11), context assembly (18), the promise gate (34). The rest are the harness's own plumbing (parsing, spans, keys, facts) and are fine as they are |
| **Fully agnostic** | 28 |
| Contain this shop's **values** inside generic code (Param) | 9 |
| Contain **shop-specific code** (Shop) | 11 |

**What this says.** The *structure* is inverted properly: stores, model,
tools, approvals, escalations, the channel and the clock are all Protocols with
several implementations, and the guardrails are hooks at four positions — the
same idea as Claude Code's PreToolUse / PostToolUse. What is *not* inverted is
the **domain's words and rules**: they sit inside shared modules as defaults
(router patterns, claim patterns, promise phrases, tier-2 conditions) or are
called directly (the permission list, the direct-answer registry).

**For a hotel agent, the changes are therefore concentrated**, and in this order:
1. Generate the router patterns, permission map and claim patterns from the
   AOAS instead of writing them in Python, behind a pluggable matcher
   (regex today, a classifier later). Fixes F-061's root cause.
2. Make the three direct calls injectable: the permission list, the promise
   gate, and the direct-answer registry (pass `handlers` through `build`).
3. Make context assembly a pluggable strategy.
4. Move the shop's code — the refund tool, the direct handlers, the wording, the
   pages — into a **domain pack** the harness never imports, and let the hotel
   agent be the test of that split.

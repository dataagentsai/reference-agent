# What the agent remembers — a design

*Written 2026-09-24, before any of it is built. Version 2: version 1 was four
stores, and the fifth shape it was tested against changed it. Logged as
[T-062](../../clean-ai-engineering/TODO.md). Carries [F-054](../evals/FINDINGS.md),
[F-055](../evals/FINDINGS.md) and [F-056](../evals/FINDINGS.md).*

> **Status: undecided, and written to be attacked.** Nothing here is built.
> Every claim below is stated so it can be shown false, and the cases table at
> the end is the part to break first. A case that does not fit means the design
> is wrong, not the case.

---

## The problem

Six things are stored today:

| | Where | Holds |
|---|---|---|
| `checkpoints` | Postgres | the conversation |
| `idempotency` | Postgres | which tool calls happened |
| `sessions` | Postgres | stored logins |
| delivery claim | Temporal | which messages were handled |
| approvals | Temporal | a person deciding |
| escalations | Temporal | a person holding a conversation |

They are **four concepts with six implementations**, and two of the pairs
disagree with each other.

### The first disagreement: what failure means

| | on failure |
|---|---|
| delivery claim | **settled** — *tried counts as done* |
| ledger | **not recorded** — *failed means try again* |

Two layers of one idea, one layer apart, with opposite rules. This is why
walking a lost-reply through the system takes two different explanations
depending on which layer you are standing on.

### The second: what a repeat gets back

| | what a repeat returns |
|---|---|
| delivery claim | `{"status": "already handled"}` — **no answer** |
| ledger | the original `ToolResult` — **the answer** |

So a customer whose reply was lost, whose order *was* cancelled, resends and is
told "already handled" — never that it worked. The data exists, in the
conversation. Nothing maps a delivery id to a conversation id, so nothing can
reach it.

---

## The one rule

> **A name is owed until there is a definite answer. A definite answer is
> stored, and returned to anyone who repeats the name.**

The word carrying the design is **definite**. There are three outcomes, not two:

| Outcome | Example | The name is |
|---|---|---|
| **definite success** | the store cancelled it | stored, released |
| **definite refusal** | *"already shipped, cannot cancel"* | stored, released |
| **indefinite** | timeout, unreachable, crash | **not stored, still owed** |

The third row is the whole point. A timeout means *it may or may not have
happened*. You must not store an outcome you do not know, and you must not
release the name — because the retry has to carry the same name so the **far
end** can recognise it.

**This split already exists here, on one side only.** `ModelRefused` (definite,
do not retry) versus `ModelUnavailable` (indefinite, retry) is the same
distinction, made for model calls and not for tool calls.

---

## The five ports

They are **ports, not tables**. How many you build depends on what the domain
already runs.

### 1 · `conversation` — what was said

Per run, read at the start of a turn, written at the end. Bounded on the way in.

Unchanged from today's `checkpoints`, except that it gains a `DELETE` (F-056)
and its bound becomes a property of the binding rather than a constant.

### 2 · `logins` — who is asking

**Re-keyed.** Today `subject` is the primary key: one row per person, one
credential. That holds for an agent with one far end and breaks for an agent
with several.

    today      subject
    should be  (subject, provider, scopes)

And its consumer changes with it. Today a stored token is resolved once into an
`Identity` the turn carries. Where a user has narrow per-service grants, the
credential is **injected at tool-call time and never held by the tool**.

### 3 · `requests` — one name, one outcome

**Absorbs the delivery claim.** A message name and a tool-call name are both
names; only the scope differs. One table with a `scope` column, obeying the one
rule above at every scope.

An abandoned claim is reclaimed by an expiry checked **at claim time**, which
needs no sweeper and no workflow:

```sql
INSERT INTO requests (name, scope, state, outcome, expires_at)
VALUES (?, ?, 'in_flight', NULL, now() + ?)
ON CONFLICT (name) DO UPDATE
   SET state = 'in_flight', outcome = NULL, expires_at = now() + ?
 WHERE requests.expires_at < now()
```

Zero rows affected means somebody holds it. That was Temporal's only reason to
hold the delivery claim.

Gains a `DELETE`, and the hard question that comes with it — see *Open*.

### 4 · `human_decision` — somebody owes an answer

**Absorbs approvals and escalations.** Same wait, same timer, same outcome, same
queue. What differs is the decision rule, and that is a parameter.

Three things it must carry that today's approvals do not:

- **what it was decided against** — the row's version at the moment it was
  raised, checked when the effect lands (F-054)
- **what authority the decider held** — captured at the decision, verified at
  execution, rather than inferred from a role that may since have changed
  (F-055)
- **whose decision is required** — a colleague's, or the principal's own

That last one is an inversion, not an addition. Today the rule is hardcoded:

```python
if approval.customer_id in (by, by_customer):
    return "an approval cannot be granted by the customer it belongs to"
```

Correct for a support agent — it is the confused deputy. **Exactly wrong** for a
personal assistant, where the user confirming their own send *is* the design.

And the TTL is a **function of the row**, not a constant. An unresolved question
about a guest arriving tomorrow is urgent; the same question about a guest
arriving in March is routine.

### 5 · `memory` — what is known across runs

**New in version 2.** *"He prefers morning meetings"* is not the conversation, and
no far end holds it. Different lifetime from `conversation`, different query
pattern, and deletion is a requirement rather than a nicety.

Absent in the support agent. The product in a personal assistant.

---

## Two properties, not ports

**Tenant scoping is structural.** Today it is an `if` in application code:

```python
if conversation.customer_id != inbound.identity.customer_id:
    raise _NotYours
```

For thousands of mailboxes that wants row-level security with the tenant key
threaded from intake, *structurally impossible to forget*. One missed `WHERE`
in one query is cross-account leakage, and it is the most mundane possible bug.

**Erasure crosses every port.** F-056: two of the five have no `DELETE` at all.

---

## The cases table

**This is the part to attack.** One rule, and every case it must answer.

| What repeats | The first outcome was | What the caller gets |
|---|---|---|
| the same message | a reply | **the original reply** — not *"already handled"* |
| the same message | the process died mid-run | runs again, once the claim expires |
| the same tool call | success | the original result |
| the same tool call | a refusal from the far end | the original refusal |
| the same tool call | a timeout | **runs again, same name** — the far end decides |
| the same human decision | granted | the first decision |
| a decision whose row has moved since | granted at an older version | **refused** — raise it again (F-054) |
| a decision by someone whose authority has gone | granted | **refused** at execution (F-055) |
| a genuinely new request | — | it runs |

Nine rows. If a case does not fit, the design is wrong.

---

## What the five shapes established

Version 1 was four stores. It was run against five agent shapes.

| Shape | Result |
|---|---|
| **Hotel support** — same shape, new domain | held · found F-054 |
| **Spark cost analyst** — read-only, one long task | held **by needing none of them** |
| **Invoice reconciliation** — waits days, moves money | held · broke two constants · found F-055 |
| **Dependency upgrade** — 200 repos, PR review | held · forced *stores → ports* |
| **Personal assistant** — many users, many far ends | **changed the design** · found F-056 |

Three things generalised across all five:

1. **The ports generalise; the constants do not.** Every break was a number
   sized for a conversation lasting seconds — `ttl: 30m`, `history_chars`,
   `fresh_for: 30s`, a session that lives as long as a login.
2. **They are questions, not tables.** The dependency agent binds
   `human_decision` to **GitHub**, which supplies five of its six parts in the
   tool the reviewer already lives in, and builds only `requests`.
3. **The five describe one run.** Two hundred repositories is a work queue with
   a status column. Reaching for the harness to manage fan-out would be solving
   a queue problem with a language model.

One claim needed qualifying: ***the far end owns correctness* assumes there is
one far end.** *"Book the meeting and email them"* is two systems that do not
know about each other; half-done is reachable and only compensation answers it.

---

## Open

**Does Saleor support a version precondition on order mutations?** This decides
T-060, and T-060 decides whether `freshness.py` is deleted or marked
compensating. Nothing else should start before it is answered.

**What does erasure mean for `requests`?** Removing a ledger row makes a
replayed call executable again. Keeping it keeps a record of what was done for
somebody who asked to be forgotten. Both are wrong; the resolution is probably
to keep the name and drop the outcome, and that needs stating rather than
assuming.

**Is `memory` really a port, or is it the application?** The specification that
produced it calls it *an application data problem in an AI costume*, which
argues it is not the harness's. It is listed here because no far end holds it
and the agent does.

**Version 2 is untested.** Five shapes were run against version 1. Four
confirmed it and the fifth changed it. Nothing has been run against what is
written above.

---

## What this does not cover

Fan-out across many runs. Long-lived working sets — a repository, four thousand
invoice lines, a week of Spark event logs — which belong to the far system.
Anything about the loop. Retention, which answers a storage bill and is not the
same question as erasure.

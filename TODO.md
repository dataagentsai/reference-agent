# TODO

Work identified by review questions and **not yet started**. Nothing here is in
progress; this is the queue.

Findings that are already defects live in [`evals/FINDINGS.md`](evals/FINDINGS.md),
and the questions that produced them in [`REVIEW.md`](REVIEW.md). This file is for
work that is *missing* rather than *wrong* — the difference matters, because a
missing capability has no failing test to point at and will otherwise be
forgotten.

Each item names **what** is missing, **why it matters**, and **where** it would
land, because an item that says only "add X" gets re-argued every time it is
read.

---

## T-001 · Nothing happens when the chat opens

**Status** Not started. Raised 2026-09-05.

**What is missing.** The customer clicks *Talk to our AI agent*, a box opens, and
it is empty. There is no greeting, no list of their orders, nothing about work
already in flight. The only route that does anything is `POST /chat`, and it
requires text — so **a session opening is not expressible at all**: it is an
entry point that is not a message.

**Why it matters.** Three reasons, in order of weight.

*The customer has to know their order number.* Every scenario in the suite opens
with a customer who conveniently types `AB-10003`. Real ones do not have it to
hand, so the first two turns of every real conversation are spent establishing
which order — turns that cost money and that a list of three orders would have
skipped entirely.

*Work already in flight is invisible.* A refund waiting on a colleague, a return
part-way through — the agent holds all of it and the customer sees none of it
until they ask. `pending_approval_id` is already on the conversation and nothing
surfaces it.

*It is the cheapest possible turn and we are not taking it.* Listing somebody's
own orders is a database read. Greeting them by name is a string. **Opening a
chat should cost zero model calls**, and a product that generates its greeting
with the model pays for every abandoned open — which is most opens.

**Where it lands.**

- `serve` — a new `GET /session` returning who you are, your recent orders, and
  anything outstanding. Same identity check as `/chat`; the check being easy to
  forget on a read-only route is exactly why it needs its own test.
- `ui` — fetch it on load and render it; make the orders clickable so the
  customer picks rather than types.
- The world already has everything needed. No new tool: `get_order` exists, and
  a `list_orders` action would be four lines of YAML.

**What it must not become.** A model call. The router's whole point is that a
question with a deterministic answer never reaches the model, and *"what are my
orders"* is the most deterministic question there is.

**What to be careful of.**

- *Another customer's orders.* The identity check on a read-only endpoint is the
  one people skip.
- *A greeting that claims something untrue.* "Your refund has been processed"
  when it has not is the same failure class the truth oracle exists for, and it
  would now happen **before the customer has typed anything**.
- *Stale data.* Orders listed at open, acted on a minute later — the same
  check-then-act window `StaleRead` already models, moved to a place nothing
  currently tests.

**AgentTwin needs it too.** A scenario begins with an actor saying something.
There is no way to express *"the customer opened the chat and saw this"*, so the
opening state a real conversation starts from cannot be simulated. Both sides
have the same gap.

---

## T-002 · Nothing issues or maintains logins, and the permission model is the wrong shape

**Status** Not started. Raised 2026-09-05. **Carries the fix for F-016 (critical).**

→ **Designed in full: [`docs/DESIGN-auth.md`](docs/DESIGN-auth.md).** The summary
below is a pointer; the design is the document.

**What exists today.** `ident.mint()` signs a token in a script. There is no user
store, no login, no password, no expiry policy anyone administers, no way to
revoke, and no way to grant one customer something another does not have —
`CUSTOMER_SCOPES` is a frozenset constant in the source.

**Three separate problems, and they need different answers.**

### 1 · The signature is symmetric — and that is the wrong shape for a token

`ALGORITHM = "HS256"`. One shared secret both signs and verifies, so **anything
able to check a token is also able to forge one**. The agent process holds it, so
a read of that process's memory or environment yields the ability to mint a
session for any customer.

Production wants asymmetric: the issuer signs with a private key it never shares,
and the agent verifies with a public key it fetches. A compromised agent can then
still read tokens and cannot write them. This is a small change — `verify()`
swaps a shared secret for a JWKS lookup — and it arrives free with any real
identity provider.

### 2 · Nothing issues the token

There is no login. Whichever provider is chosen replaces `mint()` entirely, and
the agent keeps only `verify()`.

### 3 · The permission model cannot express the rule that actually matters

This is the important one, and F-016 is its consequence.

`orders:write` says *this caller may write orders*. The rule the business needs is
*this caller may write **their own** orders*. The first is a permission about a
**verb**; the second is about a **row**, and a scope list has no way to say it.

Fixing that is **not** a job for an authorization service. The tool boundary
already holds both the caller's identity and the row it is about to act on, so
ownership is a comparison. The world file even declares the relationship —
`customer_id: {ref: customer.id}` — and nothing reads it at call time, exactly as
the ontology went unread until R-012 made the generator consume it.

A policy engine earns its place when the rules stop being *"it is yours"*:
household accounts, a partner acting for a customer, an agent acting for a
partner. Reaching for one now would add a network hop to answer a field
comparison.

**Open source, and what each is for.**

*Issuing tokens and running a login* — **Keycloak** (the incumbent; heavyweight,
Java, its own database, an admin UI that already does everything), **Zitadel**
(Go, multi-tenant by design, modern), **Authentik** (Python, friendlier admin),
**Ory Hydra** with **Kratos** (API-first, no UI, most composable and the most
assembly required). Any of them gives asymmetric signing and JWKS, so problem 1
resolves as a side effect of solving problem 2.

*Fine-grained authorization, later* — **OpenFGA** or **SpiceDB** for
relationship rules in the Zanzibar style, **Cedar** or **OPA** for policy as
code, **Casbin** if it should stay in-process. None of these is needed to fix
F-016.

**Where it lands.** `identity` (verify against JWKS rather than a secret), the
tool boundary (the ownership check), and the world file, which may want to say
*which* field carries ownership rather than having the agent assume `customer_id`.

**AgentTwin needs it too, and this is why the defect survived.** Every test,
scenario and golden case uses one customer. `C-1042` is in the fixtures, the
world seeds one customer, and the actor is always that customer. **A defect that
takes two customers to see cannot be seen by a suite that has never had two.**
A second seeded customer and one hostile actor would have caught F-016 on the day
the tool boundary was written.

---

## T-003 · Deduplication only works for one process — and the answer is mostly to adopt, not to build

**Status** Not started. Raised 2026-09-05, and **revised the same day** after the
right question: *why are we writing any of this?*

**The defect in the plan, first.** The module currently claims the durable version
is *"a swap rather than a redesign."* That is wrong. `settle` runs in a `finally`,
which does not run when a process is killed outright.

- *In memory*, the dictionary dies with the process. Nothing is stranded — it is
  self-healing **by accident**.
- *Durably*, the `in_flight` row outlives the process that wrote it and **nothing
  will ever settle it.** Every redelivery then gets `OverlappingRun`, forever.
  One crash permanently wedges that message.

A durable claim therefore needs an expiry, which the in-memory one does not. That
is the kind of thing normally discovered in production.

---

### But the real answer is to write much less of it

**1 · Make the effect idempotent where the state lives.** The cleanest option by
a distance, because it removes the coordination problem rather than solving it.

```sql
UPDATE orders SET status = 'cancelled'
 WHERE id = %s AND status IN ('pending','confirmed');
-- rowcount 1 = we did it.  rowcount 0 = somebody already did.
```

One statement, atomic, no lock, no lease, no claim table, no stuck rows. A
duplicate becomes *harmless* instead of *prevented*, and harmless survives
crashes that prevention does not.

**2 · Let the queue do it, if a queue is delivering.** SQS FIFO
(`MessageDeduplicationId`), Azure Service Bus duplicate detection, Pub/Sub
exactly-once, Kafka's idempotent producer. **Configuration, not code.**

The catch nobody mentions: a queue deduplicates *its own* deliveries inside *its*
window, and only when the **producer** attaches a stable id per message. So the
id is still the thing that matters — the queue is just somewhere to check it.
**Whoever mints the id owns the correctness.** Our browser mints one per message
rather than per attempt, which is the part that is already right.

**3 · If durable multi-step execution is needed — and an agent needs it — adopt
it rather than build it.** Temporal, Restate, DBOS, Inngest. Deduplication by
workflow id is built in, a crash resumes where it stopped, and a workflow can
sleep for an hour waiting on a human and wake up correctly.

That last clause matters more than the deduplication. **Three modules here exist
largely because we lack durable execution:**

| module | what it re-implements |
|---|---|
| `trigger` | run-once semantics |
| `state` | checkpoint and resume |
| `approvals` | a long wait that survives a restart |

Adopting Temporal would subsume most of all three. For a product that is
straightforwardly the right call.

**4 · Only if none of the above applies**, the claim table — `INSERT … ON CONFLICT
DO NOTHING RETURNING`, so the unique constraint provides the mutual exclusion,
plus a `claimed_at` expiry so a killed process cannot wedge a message.

---

### What the usual infrastructure does and does not do

**Load balancer — no, and it is the *cause*.** It is what puts two copies of the
agent behind one address, which is precisely why an in-memory dictionary stops
working. It has no concept of a duplicate.

**API gateway — less than people expect, and it may make things worse.** Gateways
do authentication, rate limiting, routing. Response caching accidentally
deduplicates identical reads and does nothing for writes. And **a gateway that
retries on a timeout is a duplicate *generator*** — a large share of duplicates in
real systems are manufactured by the infrastructure meant to add reliability.

**Queue — yes, genuinely**, subject to the producer-id caveat above. Note it also
changes the product: a queue implies the customer does not get an immediate
answer.

---

### Does read-before-write in MCP solve it?

**Partly, and it is worth being precise about how partly**, because this is the
reason the duplicate in our own test did no damage.

`cancel_order` re-reads the row and re-checks the conditions, so a second attempt
finds the order already `cancelled` and refuses. **The world stopped it, not the
harness** — and a control that works by accident of the business rules is worth
naming as such.

Three things it does not do.

*It is not atomic.* Read-then-decide-then-write is the classic check-then-act
race: two processes can both read `pending`, both conclude "allowed", and both
write. The window is small, not zero — and it is exactly what the `StaleRead`
perturbation already simulates. The fix is to make the write itself conditional,
as in option 1 above; then the check and the act are one statement.

*It only works when the effect destroys its own precondition.* Cancelling works
because a cancelled order cannot be cancelled. **Sending an email, charging a
card and calling a webhook have no state to re-read**, and for those, read-before-
write offers nothing at all.

*It protects the effect, not the conversation.* The second run refuses correctly
and the customer may still be told something confusing or contradictory. The
world is safe; the reply is not.

So: a genuine second line of defence, and not a substitute for an identifier. It
turns *"a duplicate causes harm"* into *"a duplicate causes a confusing reply"*,
which is an improvement and not a fix.

---

### Why this repository hand-rolls it anyway

Deliberately, and it should be said plainly rather than defended. This is a
**reference implementation**, and its whole job is to show what a harness must
contain. If the answer to *"how do you guarantee once"* is *"Temporal does it"*,
a reader learns nothing about what was needed — and the catalogue's own output is
supposed to be the **coverage delta**: *you chose X, here are the N things X does
not give you*. That sentence cannot be written by someone who never held the
problem.

**For a real product this trade is the wrong way round.** Adopt durable
execution, let the queue deduplicate, make the effects idempotent at the far end,
and delete `trigger` entirely.

**Where it lands.** Option 1 in the projected world and the real shop; option 3 as
a documented alternative binding rather than a rewrite. The in-memory log stays
for tests, with its docstring corrected — the durable version is a **superset**,
not a swap.

**AgentTwin needs it too.** Nothing runs two agents against one world, so a defect
needing two processes cannot be seen — the same shape as F-016 needing two
customers.

---

## The queue

| | Item | Raised |
|---|---|---|
| T-001 | Nothing happens when the chat opens | 2026-09-05 |
| T-003 | Dedup works for one process only, and the durable port needs claim expiry | 2026-09-05 |
| **T-002** | **No login exists, and the permission model cannot express ownership — carries F-016** | 2026-09-05 |

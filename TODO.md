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

## The queue

| | Item | Raised |
|---|---|---|
| T-001 | Nothing happens when the chat opens | 2026-09-05 |
| **T-002** | **No login exists, and the permission model cannot express ownership — carries F-016** | 2026-09-05 |

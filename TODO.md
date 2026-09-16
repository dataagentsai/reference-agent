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

## T-004 · An Anthropic adapter at L2 — the loop stays ours

**Status** Not started. Raised 2026-09-05.

**What this is not.** Not adopting the Claude Agent SDK, and not giving up the
hand-written loop. Those are L4 decisions. This is L2: which client the adapter
wraps. `anthropic` brings **no loop** — you call `client.messages.create()`
inside whatever loop you already have, which is the ordinary case rather than a
workaround.

**Why it is one module.** The import contract already says *only `llm` may import
a provider SDK*, enforced by import-linter on every run. So the blast radius of
this change is `llm/__init__.py` and nothing else — the contract was written for
precisely this.

**Three open items it closes at once**, which is what makes it worth doing:

*The context budget is measured in characters.* Left over from F-008 and recorded
as "deferred and handled are different words." `client.messages.count_tokens()`
makes it tokens, which is what every budget in the system actually meant.

*Offline evals pay full price.* The Batch API is half, asynchronous, and an eval
suite is exactly the latency-insensitive workload it exists for.

*Prompt caching is unreachable.* R-004 noted the stable-prefix ordering is
**already in place**, so `cache_control` on the system block would work on the
first attempt. An OpenAI-shaped request has no field to carry it, so this is not
a matter of effort — the shim structurally cannot.

**And two things it would make newly possible**, both of which land on layers we
already own:

*Mid-conversation system messages.* An operator instruction appended to
`messages` that does not invalidate the cached prefix, and is the
injection-safe operator channel. That is L7's `PRE_MODEL` position — one of the
three declared and empty ones (R-008).

*Server-side compaction and context editing.* The long-conversation problem
answered above the harness rather than inside it, where `context` currently
trims by hand.

**What it does not change.** The loop, the router, the tool boundary, the policy
positions, the oracles, the world. All of L4 stays exactly as written, which is
the point: **the L2 and L4 decisions are independent**, and conflating them is
how a team adopts an entire harness in order to obtain prompt caching.

**Cost note.** This repository's standing constraint is free hosted open-weight
providers. An Anthropic adapter would sit *alongside* the Groq one rather than
replacing it — `LLMClient` is already a protocol with three implementations, so a
fourth costs nothing and the resolution seam decides which runs.

---

## T-005 · Carry the idempotency key to the far end

**Status** Not started. Raised 2026-09-05. **Carries F-017.**

Today the key is minted, used for a local lookup, and thrown away. The contract's
own docstring says it is *"carried to the downstream system"* and it is not.

**What that leaves unprotected**, in the docstring's own words: the call
succeeded, the response was lost, and from this side that is indistinguishable
from failure. Our ledger recorded nothing, so the retry goes out again. **Only
the party that applied the effect can tell the difference** — and only if we told
it the action's name.

**Both halves are needed.**

*Ours:* the key goes in the request. Either an argument the projected tool
declares, or MCP request metadata. Which calls need it is **derivable** — the
world already declares each action's side-effect class, so it is a rule rather
than a list somebody maintains.

*Theirs:* the far end must actually use it. The conditional write from T-003 is
the cheap version; storing the key and refusing a repeat is the complete one.

**Do this with T-003**, not separately. They are the same idea at two distances —
make the effect safe to repeat, and give the repeat a name the far end
recognises.

## T-006 · A customer's own conversations cannot be found

**Status** Not started. Raised 2026-09-15. **Needs T-002 first.**

**What is missing.** A returning customer cannot be given back anything. Close
the browser and `conversationId` — a `let` in the page, not `localStorage` — is
gone, so the next message carries no id and `_conversation_for` mints a fresh
conversation. Six past conversations, two of them with something still open, and
nothing connects them to the person in front of us.

Two separate questions are hiding in that, and only one of them is close.

*"What do I have open?"* — nearly reachable. `approvals` and `escalations` both
carry `customer_id NOT NULL` already. What is absent is a **query**: the
protocols offer `open_for(conversation_id)` and `pending()`, which are the
agent's view and the reviewer's view. Neither is the customer's.

*"Continue the one about AB-10003."* — the data exists and is unreachable.
`facts.records` is precisely *which identifiers this conversation touched*, and
it is deliberately bounded: a customer who asks about the same order nine times
costs one entry, and nothing in it is ever dropped by age. But it lives inside
the `state` blob, and `agent_state.checkpoints` has no `customer_id` column at
all — the customer is *in* the row and cannot be selected on. The tables already
disagree about this: two of the three treat a customer as a first-class thing
and the third does not.

**Why it matters.** It is T-001's third reason with a record behind it. A
customer who raised `esc_7`, closed the tab, and came back has an escalation
still `queued` in Postgres, findable by conversation id, that nothing will ever
ask for. They must quote the reference themselves — assuming they wrote it down
— which is the state `HandoffDesk` already names as the failure worth avoiding:
*an escalation that made the customer repeat everything is the moment an
assistant becomes worse than no assistant.*

**Where it would land**, in the order the blockers allow:

1. `customer_id` on `agent_state.checkpoints`, with an index. One migration, and
   it unblocks every version of this.
2. `open_for_customer(...)` on `ApprovalStore` and `EscalationStore` — the
   greeting T-001 wants, and the cheapest half.
3. A `conversation_records(conversation_id, record)` index written beside the
   checkpoint, so *"the chat about AB-10003"* is a join rather than a scan of
   every blob.
4. Resume by carrying **`facts`, not messages.**

**Step 4 is the one to argue about before building.** Prepending an old
transcript is the obvious implementation and the wrong one: the assembly window
is 24,000 characters and trims whole exchanges from the middle, so an old
conversation pushes out the turns the model is actually answering.
`facts.as_handoff()` already exists and is what a *human* colleague is given —
assembled from the record, never summarised from the transcript (AHC-0108,
AHC-0070). Resuming should mean the agent knows AB-10003 was returned and a
refund is pending, not that it has forty old messages.

**And it is a disclosure surface.** Pulling a past conversation means the agent
knows things this session's customer never said. `_conversation_for` already
refuses a mismatched owner with a 404 rather than a 403, because confirming the
id exists tells an attacker their guess was right; the same check has to hold on
every new path, and a shared device or an impersonated session is where it gets
tested.

## T-007 · Every scenario runs once, so nothing measures reliability

**Status** Not started. Raised 2026-09-16. **Discharges `AAC-0010`, currently in
`NOT_EXERCISED.md`.**

**What is missing.** A number for *how often the agent gets it right*, as opposed
to *whether it can*. Every scenario runs exactly once, against a scripted model,
so the suite is deterministic by construction: it proves the harness works and
says nothing about the agent's consistency. `AAC-0010` — variance across repeated
runs is bounded — has no test behind it and this is why.

**What the field uses.** τ-bench's `pass^k`: the probability that **all k**
trials of a task succeed, averaged over tasks. Deliberately the opposite of
`pass@k`, which asks whether *at least one* of k succeeds and suits code
generation, where you can generate ten candidates and ship whichever passes. A
support agent has no such luxury — every customer gets one attempt — so the
metric that matters is the pessimistic one.

**Why it matters.** The numbers do not degrade gently. A retail agent reported at
61% mean accuracy is at **25%** by `pass^8`. Mean accuracy hides exactly the
property a deployment cares about, and hides it worse the more the agent is used.
Frontier models sit below 70% on τ-bench `pass^1` for retail and near 46% for
airline, which is the honest shape of this problem and not a reason to avoid
measuring it.

**Where it would land.** The pieces mostly exist: `--live` in `scripts/run_view.py`
and `scripts/live_runs.py` already drive a real provider. What is absent is
running one scenario *n* times and reporting, per scenario, how many of the *n*
passed — then `pass^k` as `C(c, k) / C(n, k)` averaged across scenarios. It
belongs beside the run view rather than in the pytest suite: a reliability figure
is a report, not a gate, and a flaky test that fails 30% of the time gets
disabled within a week.

**What not to do.** Do not adopt τ-bench's 165 tasks. Different domain, and their
policy is prose in a prompt where ours is declared and machine-checkable — taking
their cases would mean giving that up. **Do** read the retail policy document as
a cross-check on the AOAS: it is 115 tasks' worth of edge cases somebody has
already thought through for a retail support agent.

## T-008 · The regeneration claim has never been run

**Status** Not started. Raised 2026-09-16. **The point of the whole family.**

**What is missing.** An experiment. The claim the spec family exists to support
is that the catalogs plus the agent specification are sufficient to *produce* an
agent — hand them to a generator in an empty folder and get similar code back.
Nothing has ever tested that. Not once, not partially.

**What is tested is the opposite direction.** 487 tests, AHC 50/72, AAC 44/52,
AOAS 50/55 — every one of them asks *does this code satisfy the specification*.
None asks *is the specification sufficient to produce this code*. A suite can be
perfect at the first and say nothing about the second, and ours is.

**The seven known weak points.** `harness-profile.yaml`'s `x_untested` lists the
capabilities believed met with no test naming them — AHC-0002, 0004, 0006, 0010,
0020, 0026, 0036. The architecture review already states what they are for: *the
best available prediction of where G1's first regeneration will disappoint,
because nothing would notice if one quietly went missing.* Closing those is worth
doing **before** the experiment, not after, or the first run produces a result
nobody can interpret.

**Where it would land.** A second directory, the specs, and a generator, with
three things fixed in advance so the result means something:

*What counts as "similar".* Not a diff. The scenarios are implementation-
independent by construction — `Subject` is three callables — so the honest
measure is **does the regenerated agent pass the same 34 scenarios and discharge
the same statements**. Structural similarity is a second, weaker question.

*The binding is part of the experiment.* `evals/simulation.py` is this agent's
binding to the scenario contract. A regeneration must supply its own, and
whether a fresh generation produces something a binding can attach to at all is
itself a finding — arguably the first one.

*What a failure teaches.* A capability the regeneration misses is a capability
the catalog states badly, not a bad generation. That is the output worth having,
and the reason to run it before the catalogs are declared finished rather than
after.

**One repo, and a second implementation is a second repo.** If this is ever run
against LangGraph or the Claude Agent SDK rather than an empty folder, each is
its own repository citing `scenarios/` by relative path — the way
`worlds/clothing.yaml` already cites the AOAS across repos. Not a `harness:`
config flag in this one: a flag means shared code, and shared code means "both
pass the scenarios" proves the shared layer works rather than proving the specs
do. Nothing to move today; the citation pattern already exists.

**And the result has to be read against a circularity.** The AOAS declares its
own `sources`, and two of the three are this repository — `worlds/clothing.yaml`
and `src/support_agent/{router,policy,approvals,escalation,identity,config}`.
Part of the specification was written *from* the code it would be regenerating.
So a close match is ambiguous by construction: it may mean the spec is
sufficient, or it may mean the spec was read off that exact structure. This is
not a reason to skip the experiment — it is a reason to say it out loud before
running, rather than discovering it while interpreting the result.

Two things reduce it. The **scenarios** and the **golden set** are the parts a
regeneration is honestly judged by, and neither was derived from module
structure: the golden cases are generated from declared conditions, and the
scenarios assert over a world. The parts most likely to be contaminated are the
structural ones — which is the second reason similarity is defined as passing
the scenarios rather than as a diff.

**What the spec does not constrain at all**, and is worth predicting before the
run so the prediction can be wrong: nothing declares the typed `TurnResult`
union, the conversation-as-one-blob, the five policy positions, the split of
`escalation` into wording/capacity/store/workflow, or gates running before
routing. Those came from the ratchets and the import contract in
`pyproject.toml` — which live here and not in any specification. A regeneration
in an empty folder gets none of that pressure, and if the goal is that it
should, the ratchets are a spec artifact nobody has written down.

## T-009 · Seven capabilities are believed met and named by no test

**Status** Not started. Raised 2026-09-16. **Do before T-008.**

**What is missing.** Seven `discharges` tags. `harness-profile.yaml` lists them
under `x_untested`, deliberately separated from `accepted_gaps` because the two
are different claims and this is the weaker one:

    AHC-0002  context assembly is one function, and it is inspectable
    AHC-0004  the choke point exists; the import contract is tested, the claim is not tagged
    AHC-0006  spans carry the run id across the hops
    AHC-0010  `Agent.handle` is the entrypoint scenarios drive
    AHC-0020  the fan-out limiter is bounded by the harness
    AHC-0026  the run id spans the unit of work
    AHC-0036  every tool declares an argument contract, validated at dispatch

**Why it matters.** Several are almost certainly already exercised by a test that
simply does not name them — AHC-0010 in particular, since every scenario drives
`Agent.handle`. That makes this cheap. It also makes it easy to leave, which is
why it has sat.

The cost of leaving it is stated in the architecture review: these are where a
regeneration will disappoint, *because nothing would notice if one quietly went
missing.* Until each has a test naming it, "the regeneration matched" and "the
regeneration dropped a capability we never checked" are the same observation.

**Where it would land.** Existing tests, mostly — find the test that already
proves each and add the tag. Where none exists, the gap is real and worth
knowing about before T-008 runs rather than after.

## T-010 · The constraints that produced this shape are in no specification

**Status** Not started. Raised 2026-09-16. **Blocks T-008 being interpretable.**

**What is missing.** A fourth artifact, beside AAC, AHC and AgentTwin, stating
what **shape** the code must take. The family currently says what must be TRUE,
what must EXIST and what must be FACED. It says nothing about structure — and
structure is what a regeneration would visibly differ on.

**It cannot be part of AHC, and the reason is AHC's own value.** That catalog is
stack-agnostic on purpose: the same capabilities are meant to hold whether the
harness is LangGraph, an SDK, or a hand-rolled loop. These constraints are
stack-bound. "Modules under 416 lines" means nothing to a graph; "arrows point
down only" is an import-linter concept; "only the composition root constructs a
realisation" presumes composition roots. Folding them in would smuggle one
language's opinions into a catalog that claims to transcend them, and the cost
would be paid by every reader who does not use Python. It belongs beside the
**binding** — the other stack-bound artifact — rather than inside a catalog.

**This is extraction, not design.** All of it exists, is enforced, and is green:

*The import contract* (`pyproject.toml`) — `Arrows point down` as layers with
`exhaustive = true`, so a new module cannot be added without being placed. Four
forbidden-module contracts, each failing closed with exactly one exception: only
`llm` sees a provider SDK, only `tools` speaks MCP, only `state` and
`idempotency` see psycopg, and the agent cannot see its simulator.

*The ratchets* (`tests/test_build_checks.py`) — one-way ceilings set at the worst
offender the day they were measured: complexity 14 → 8, statements 50 → 24,
branches 12 → 7, longest module 744 → 416, `type: ignore` at 2.

*The structural tests* — only the composition root constructs a realisation;
every collaborator the root takes is an interface; every failure declares its
kind; every counter declared is incremented somewhere.

*`mypy --strict`* with `exhaustive-match`, so a new route or result kind fails
the build rather than falling through the last branch.

**Why it matters, and it is the reason to do it before T-008.** The architecture
review records that the ratchets *"forced five extractions in the last two days,
each of which turned out to be a job with a name."* The module structure was not
designed — it was produced by the ceilings. So the testable claim is: a
regeneration handed the ceilings arrives at a similar structure, and one handed
only AHC does not. Without this artifact, T-008 can only report that the shapes
differ; with it, T-008 can ask whether the constraints are what carries shape.

**Open questions**, deliberately not answered here: what it is called; whether
the numbers travel (416 is this codebase's history, not a law) or only the
*mechanism* of a one-way ratchet does; and whether a TypeScript sibling would
share anything but the mechanism. The last is the test of whether this is one
artifact with bindings or a family of them.

## T-011 · Write the hotel AOAS and nothing else

**Status** **Done 2026-09-16.** `clean-ai-engineering/drafts/examples/support-agent-hotel.aoas.yaml`
and its extraction record. It validates; nine of thirteen sections carried
unchanged; four things resisted and a fifth turned up separately from T-012's
citation work — the AOAS has normative blocks with **no identifier**, so a file
cannot cite what governs it. Do not fix the five until T-013 adds its set, or the
format gets fitted to hotels.

**What is missing.** Evidence that the specification set is *complete* — that
everything a second agent must change has somewhere to be said. Not evidence
that a regeneration works; that is T-008 and it is expensive. This is the cheap
falsifiable version, and it can be run without generating a line of code.

**The measurement this rests on.** `evals/reuse.py` already classifies every
module for exactly this question, and the numbers as of 2026-09-16:

    mechanism       46 files   7865 lines   77.2%   a second agent keeps the file
    parameterised    9 files   1663 lines   16.3%   keeps the code, replaces the values
    per-agent        5 files    662 lines    6.5%   writes its own

So roughly 94% of this code survives a change of domain. The five rewritten are
`contracts/domain.py`, `entrypoint/direct.py`, `approvals/refund.py`,
`escalation/wording.py`, `ui/__init__.py`.

**The exercise.** Take those five, plus the nine parameterised modules —
`router`, `approvals/policy`, `escalation/rules`, `policy`, `binding`, `config`,
`approvals`, `entrypoint/promise`, `telemetry/contract` — and write the AOAS for
a hotel support agent. Only the AOAS. Then ask, item by item: **does the format
have a place to say this?**

Where it does, that value is spec-supplied and the thesis holds for that piece.
Where it does not, a hole in AOAS has been found for the price of a day.

**Why the parameterised layer is the real subject.** Its own docstring calls it
the dangerous one: *universal code, this agent's values… a bug in the values
reads as a bug in the mechanism.* The per-agent five are obviously per-agent and
nobody will forget them. The nine are the ones that look shared and are not, and
they are where an incomplete AOAS does its damage quietly — a regenerated agent
inherits `router/__init__.py`'s shape and this agent's regexes, and passes
review.

**What this does not test**, stated so the result is not over-read: it says
nothing about whether the 77% mechanism layer would be *reproduced*. That layer
is the part no specification describes structurally — AHC constrains that a
provider choke point exists, not that it is a Protocol in the bottom layer that
adapters satisfy without importing. That gap is T-010, and this exercise is the
argument for it rather than a substitute.

**And the artifact this leans on is thin.** `evals/reuse.py` is a hand-maintained
list of filenames. Its one safeguard is that a module with no layer fails the
test, so the classification cannot rot silently — but the layer assignments
themselves are judgement, recorded once, by one reader. If the thesis rests on
that split, the split deserves more than a list.

## T-012 · Trace every mechanism file back to a spec statement

**Status** **Done 2026-09-16** — `docs/WHY-EACH-FILE.md`, with a coverage test.
The three follow-ups it produced are listed at the end of that document; the one
that changes another item is that T-010's premise was wrong in size.

**What is missing.** A reason, per file, for the 77%. `evals/reuse.py` says 46
files and 7865 lines are mechanism — the part a second agent keeps. Nothing says
*why each one has to exist*. Until that is written down, "the specs are
sufficient" is a claim about the part of the codebase nobody has justified.

**The exercise.** For each of the 46 mechanism files, answer one question: **which
spec statement requires this file to exist?** Cite it — an AHC capability, an AAC
obligation, an AOAS statement. Three outcomes and all are useful:

*A statement names it.* Good. That file is spec-supplied.
*No statement names it and the file is load-bearing.* A hole in the spec set, found
for the price of reading.
*No statement names it and nobody can say what breaks without it.* A candidate for
deletion, which is the cheaper finding.

**Why before T-011 and T-013.** Those test the 22% that is already best covered —
the values and the domain. This attacks the 77% that no specification describes
structurally, which is where the thesis is weakest and where T-010's argument
either holds or does not.

**Expect the answer to be uncomfortable.** `loop/freshness.py` traces to AHC-0107
cleanly. `entrypoint/persist.py` exists because three call sites once wrote the
conversation and none capped it — a defect, not a statement. Files in the second
category are the interesting ones: the codebase knows something the catalogs do
not say.

---

## T-013 · Write the Spark cost analyst AOAS — the adversarial shape

**Status** Not started. Raised 2026-09-16. **Sibling of T-011, different failure
mode expected.**

**What is missing.** Evidence that the specification set describes an agent that
is **not shaped like this one**. T-011 varies the domain and keeps the shape;
this varies the shape and is therefore the sharper test.

**The agent.** Case 2 of `LearnAgenticHarnessFrameworks/11-worked-examples.md` —
reads Spark event logs and Delta metadata, finds expensive jobs, diagnoses why,
writes up the evidence. It is also CostLens, which is the only argument that
carries against the single-thread decision.

**What it removes**, and this is the point:

    who is waiting        a customer, live      →  nobody, it runs nightly
    a run lasts           seconds               →  twenty minutes to two hours
    shape                 turns, a conversation →  one long task, no turns
    writes                yes, gated            →  read-only
    approvals, escalation central               →  absent entirely
    context               fits easily           →  the entire job

Three of this agent's load-bearing concerns — approvals, escalation, irreversible
effect gating — are simply not present there. So the question is not whether the
format can say new things, it is whether it **degrades gracefully**: can an AOAS
describe an agent with no approval gate without the omission reading as an
oversight? If the format can only describe an agent shaped like this one, that is
a hole and a large one.

**And it inverts the pressure on context.** Context lifecycle is a *Must* for that
agent and barely exercised here. Four of the seven context handlers are unbuilt —
Offloader, Selector, Deduplicator, Compactor — and that agent needs at least the
first two. Whether "deliberately absent" survives a second shape is a real test
of that table, not a rhetorical one.

---

## T-014 · Delete a capability and see whether anything notices

**Status** Not started. Raised 2026-09-16. **An afternoon.**

**What is missing.** Evidence that the seven `x_untested` capabilities are *real*
rather than believed. T-009 adds tags to the tests that already prove them; this
asks the prior question — is there anything to tag?

**The exercise.** Take one of AHC-0002, 0004, 0006, 0010, 0020, 0026, 0036.
Remove the code that implements it. Run everything. If nothing fails, that
capability is not exercised here, and the profile's `x_untested` entry is
optimistic rather than merely untagged.

It is mutation testing pointed at the **specification** instead of the code, and
the instrument already exists: `scripts/scenario_kill_matrix.py` does exactly this
for branches. Doing it for capabilities is the same technique one level up.

**Why it matters more than it looks.** The architecture review calls these seven
*the best available prediction of where a regeneration will disappoint, because
nothing would notice if one quietly went missing.* That sentence contains a
testable claim and nobody has tested it.

---

## T-015 · Ask a fresh model to describe the structure from the specs alone

**Status** Not started. Raised 2026-09-16. **Five minutes. The cheapest item here.**

**What is missing.** Any evidence at all about whether the specs carry structure.
T-008 answers it expensively by generating a whole agent. This answers a weaker
version for nothing.

**The exercise.** Hand the AOAS, AHC and AAC to a model with no access to this
repository and ask it to **describe** — not generate — the module structure it
would build. Then compare with `entrypoint / loop / policy / context / state /
approvals / escalation`.

**How to read it.** A close answer weakens T-010's argument and is worth knowing.
A distant answer confirms the structural gap for the price of one prompt, and
does so *before* T-008 spends real effort discovering it.

**One caution.** A model that has read this repository during training, or that
recognises the shape from public patterns, gives a false positive. Run it on the
specs alone, in a clean context, and treat a suspiciously exact answer as
evidence of contamination rather than of sufficiency — the same caution T-008
carries about the AOAS citing this repo among its sources.

## T-016 · Three things on this project's own "do not build" list are built here

**Status** Not started. Raised 2026-09-16. **Audit, then one item per adopt.**

**What is missing.** A verdict per harness component: keep, adopt, or wrap. Some
things here are hand-rolled deliberately — the loop is, and the catalogs exist
because a framework owning that layer hides what they expose. Others are
hand-rolled because it was quicker that day and nobody went back. From outside the
two are indistinguishable, and that is the real cost: a reader cannot tell a
decision from an accident, and this repository exists to be read.

**Scored against `LearnAgenticHarnessFrameworks/08-what-you-build.md`**, which is
this project's own list of what not to build:

    A trace format              clean — OTel GenAI conventions
    A prompt DSL                clean — Jinja and files, versions in traces
    Own tool protocol           clean — MCP
    A vector DB, a sandbox      not applicable
    Model client wrappers       VIOLATED — llm/ plus resilience/ is 636 lines,
                                against a warning that a thin wrapper will not stay thin
    A durable workflow engine   PARTLY — approvals and escalation with ttl, lapse
                                and sweep is a small workflow engine
    The agentic loop            QUALIFIED — the stated exception applies, but the
                                danger named is drifting into a half-built
                                compactor and checkpointer, and context/ and
                                state/ are that

Three of eight. The loop is defensible and should be defended in writing. The
other two are drift, and drift is what an audit is for.

**The adoptions, highest value first.**

*LiteLLM at L2 — **measured 16 Sep, and the case does not hold as written**.*
The claim above was about 600 lines. It is 65: `GroqClient` is 65 lines of
`llm/`'s 266, and `ScriptedClient` and `UnavailableClient` stay because the tests
need them.

**`resilience/` cannot go, and the reason is structural rather than a
preference.** `ResilientLLM` wraps an `LLMClient`, and that is the seam scenarios
inject faults through — `evals/simulation.py` builds
`ResilientLLM(FaultyProvider(scripted))`, which is how `the-provider-throttles`
and `the-model-fails-twice` work at all. LiteLLM's retries live *inside* LiteLLM,
below that seam, so it would never see a `FaultyProvider`: production would
retry and simulation would not. That is F-029 exactly, and `simulation.py`
already states it — *a simulation that composes the agent differently from
production is simulating a different agent, and the difference is invisible until
a scenario asks the provider to misbehave.*

So the real trade is 65 lines removed, ~50 lines of adapter added, a dependency
gained, LiteLLM's own retries disabled to avoid two retry layers, and
`completion_cost` avoided because it returns 0.0 for an unknown model rather than
raising — the exact failure `UnknownPrice` exists to prevent. Net roughly zero
lines for one dependency. The gains that remain are real but narrow: a hundred
providers behind one call, and a maintained price map worth cross-checking ours
against.

**The better shape is the proxy, and it is T-018's point arriving concretely.**
Provider portability, retries, cooldowns, fallbacks and budgets are what a
gateway is for, and a gateway is reached through `provider_base_url` — no
dependency, no code change, `GroqClient` already takes a `base_url`. Resilience
then sits in the gateway where it covers everything else you run, `ResilientLLM`
stays untouched so F-029 holds, and a later move to Databricks or Azure is one
gateway replacing another. **Decide this with T-018 and not before**; the answer
today is *neither, until there is somewhere to run a proxy*, which is P1 again.

*An OTLP processor.* **Done 16 Sep.** `tel.export_to(endpoint, headers=…)`, called
from the composition root when `AGENT_OTLP_ENDPOINT` is set, adding a
`BatchSpanProcessor` **beside** the in-memory exporter rather than instead of it.
Batch and not Simple: a network export on the request path would put a
collector's latency inside a customer's turn, and its outage there too.
`otlp_endpoint` is deliberately outside `RunConfig.fingerprint` — pointing
telemetry elsewhere does not change what the agent does, and a fingerprint that
moved when it did would call two identical runs different systems.

One thing the work taught, recorded because it is a live trap: a
`BatchSpanProcessor` aimed at a collector that is not listening **retries in a
background thread**. Correct in a deployment, where a restarting collector should
not lose a trace. Intolerable in a suite, where it is noise on every later test —
the first draft of the test left it running, and the fix is that the test shuts
the provider down.

*A chat surface.* Chainlit or assistant-ui. Hand-written HTML in `ui/` teaches a
reader nothing this project wants taught.

*Record and replay.* VCR.py or pytest-recording in place of `cassette/`.

**What to evaluate rather than adopt.** LangGraph's Postgres checkpointer against
`state/`, and its interrupts — or Temporal — against the approval and escalation
workflow. Both are the L4 decision, which T-004 argues is independent of L2 and
should stay that way. Evaluating is not adopting; the output is a written reason.

**What stays, and the test for it.** The doc states the heuristic: the do-not-build
list is everything that is the same for everyone, and the build list is everything
that encodes your domain, risk appetite and definition of quality. By that test
these are ours and should say so: `contracts/`, `policy/` — the five positions are
the contribution and no guardrail library models position at all — `approvals/`,
`escalation/`, `state/facts.py`, `router/`, the scenarios and the golden set.

**Why it matters beyond tidiness.** All three stated purposes depend on it. A
reference somebody copies should not teach them to write a circuit breaker. A
teaching artifact has to show where the line is, which means drawing it. And a
funding case is stronger when the novel part is small and named than when
bespoke-everything implies it.

**Where it lands.** A table in `REVIEW.md` with a verdict and a reason per row,
each adopt becoming its own item. The rows already decided this week — Hypothesis
adopted, Cosmic Ray used once to calibrate and not kept — belong in it as
precedent for the format.

**Three rows whose verdict is already decided and should be written as such.**

*The customer-facing chat, and the human side of escalation — adopt Chatwoot.*
Its Agent Bot API is this agent's escalation model already built: webhook events
carry the message with full conversation context, and a bot that decides a person
is needed flips the conversation to `open`, at which point a human takes over
**with the transcript already there**. Channels, history and assignment come with
it. `facts.as_handoff()` stops being the whole handoff and becomes an added note.
It closes T-001 and the customer half of T-017 without a line of UI. Two things to
record with it: it does **not** solve approvals — escalation is a person taking
the conversation, approval is a person authorising one action while the agent
keeps it, and only the first is Chatwoot-shaped — and it needs **T-002** first,
because Chatwoot supplies a contact and this agent needs a signed `customer_id`.

*Intent conversion — adopt nothing.* Multi-intent is already specified and
already tested: P-DIRECT sends a turn naming several orders or several intents to
the loop, F-037 is the defect from getting that wrong, and
`twelve-steps-and-then-a-person` exercises five orders in one run. There are two
opinions about intent here — a regex router for the unambiguous single case and
the loop for everything else. An NLU layer would be a **third**, and three things
that can disagree about what a customer meant is worse than two, because a
misroute then has no clear owner.

*Model tier routing — deferred, with the seam named.* Routing a hard question to
a strong model and an easy one to a weak model is solved: LiteLLM has cost-based
strategies, OpenRouter ships an Auto Router with a cost/quality dial, and
RouteLLM is the research answer at 85% cost saving for 95% of GPT-4 quality. None
of it is worth adopting **here yet**, and the reason is arithmetic: the ceiling is
`max_cost_usd = 0.50` per task and a real turn costs about $0.00002 — four orders
of magnitude of headroom. Tier routing optimises a cost this agent does not have,
and P-DIRECT already answers the more valuable question, *any model at all*, by
removing three of four routes before one is called. When it does become real the
seam exists: `LLMClient` is a Protocol with six implementations, and a
`TieredLLM(strong, weak)` is a seventh that changes nothing else — the same shape
as `ResilientLLM` wrapping `GroqClient`. Recorded here rather than left out so a
later reader sees a decision and not an oversight.

---

## T-017 · A real store, and a human in front of it

**Status** Not started. Raised 2026-09-16. **Depends on P1 (T-001, T-002).**

**What is missing.** Anyone has ever talked to this agent. Every run to date is
scripted or simulated: 34 scenarios with a scripted model, a golden set derived
from declared conditions, a world projected from YAML. All of it proves the agent
does what the specification says. None of it proves the specification describes an
agent worth talking to.

**Two halves, and only one is new.**

*The store.* `ResolutionMode` already declares `[mock, replay, real, shadow]` and
`worlds/clothing.yaml` says `resolution: mock`. A real store is the `real` value —
the seam was designed for this and has never been exercised. **Saleor** is the
closest fit by stack (Python, GraphQL, Postgres), though the language barely
matters: the agent reaches the store over MCP and never imports it, so Medusa or
Vendure work equally. What changes is one tool server, from the projected world to
one that calls a real API.

*The human.* A person opening a chat and trying to get something done. That is
where the findings will be, and they will be a different **kind** of finding —
the suite catches wrong behaviour, and a person catches behaviour that is correct
and useless.

**What it will find**, predicted so the prediction can be wrong: T-001 in the
first thirty seconds. An empty box, no greeting, and the customer must know their
order number. Every scenario opens by stating one because the actor is scripted
to; a person will not.

**And it is the honest test of `shadow`.** The fourth resolution mode is declared
and unbuilt. A real store beside the projected one, both answering, the difference
recorded, is how `verified_against: null` in every world file stops being null.

**Why it matters, stated because it changes what "done" means.** Three purposes
have been named for this repository and they pull in different directions:

1. **A reference** somebody copies when building a production agent. Requires the
   hand-rolled/adopted line to be explicit (T-016) and the gaps to be honest.
2. **Teaching.** Requires the *reasons* to survive, which is why the docstrings
   carry findings and why `docs/WHY-EACH-FILE.md` exists.
3. **A funding case.** Requires a claim that has been *tested* rather than
   argued — which is T-008, and which is why the hole-finding items are
   sequenced before it.

A human conversation serves all three and is prerequisite to none. It should
happen once P1 exists, and its findings should be recorded the way F-001 to F-040
were: the failures are the deliverable.

## T-018 · The fingerprint cannot tell a gateway from a provider

**Status** Not started. Raised 2026-09-16. **Small, and a decision rather than a
task.**

**What is wrong.** `RunConfig.fingerprint` hashes everything that changes
behaviour and excludes what does not — `mcp_base_url`, and now `otlp_endpoint`.
It **includes** `provider_base_url`, with a stated reason:

> `provider_base_url` is included only because pointing at a different provider
> *is* a different system.

That reason is right and the implementation cannot honour it, because one field
carries two different facts:

    Groq, called directly          a different provider      fingerprint SHOULD move
    Groq, reached via a gateway    the same model, same weights, one more hop
                                                              fingerprint should NOT move

Put a gateway in front — LiteLLM's proxy, Databricks Mosaic AI Gateway, Azure AI
Foundry, Vertex's OpenAI-compatible endpoint — and every run after it is
uncomparable with every run before it, for a change that altered nothing the
agent does. This is exactly the argument `mcp_base_url` is already excluded
under: a world reached over a different URL is the same world.

**Why it matters now rather than later.** The gateway seam is already open and
costs nothing to walk through. `GroqClient` takes `base_url`, four of the five
plausible cloud gateways are OpenAI-compatible, and moving to one is an
environment variable. The first person to do that will silently invalidate the
fingerprint history, and the fingerprint history is the thing that makes "it
passed last week" checkable — which is the entire reason `config` exists.

**The decision, not the code.** What identifies "the same system" for a model?
The candidates, and none is obviously right:

*The model id alone.* Clean, and wrong the moment two providers serve the same
open-weights model with different quantisation — which is the ordinary case for
`openai/gpt-oss-120b`.

*Model id plus a declared provider name*, with the URL excluded. The provider
becomes a stated fact rather than an inferred one, which is the shape the rest of
this file already prefers: `resolution` is declared, not derived from whether a
URL looks like localhost.

*Both, with the URL kept and a second "route" fingerprint beside it.* Honest, and
two numbers where one is wanted.

**Where it lands.** `config/__init__.py`, and `harness-profile.yaml` if provider
becomes a declared field. Any change to what the fingerprint covers is a break in
comparability with every run recorded before it, so whichever is chosen, the
change itself should be dated in the file — the way `evals/baseline.json` records
`taken` and the golden set records why it grew.

**And the gateway question this came out of, recorded so it is not re-derived.**
There is no AI gateway here today; the agent calls the provider directly. The
seam is `provider_base_url` and it needs no work. A LiteLLM **proxy** — as
opposed to the SDK — subsumes retries, backoff, cooldown and throttling into the
gateway, which deletes `resilience/` without adopting a library in-process, and
survives a later move to Databricks or Azure because that is then one gateway
replacing another rather than a library being un-picked. If a gateway is coming,
it is the better shape than T-016's LiteLLM-SDK row, and the two should be
decided together rather than in sequence.

---

## The queue

Two tracks. **A** asks whether the specifications are sufficient; **B** makes the
agent real. They are independent and A is cheaper.

### Done

| | Item |
|---|---|
| ~~T-011~~ | Hotel AOAS — worked example 2. Four frictions, plus a fifth from T-012 |
| ~~T-012~~ | Why each of the 46 mechanism files exists — `docs/WHY-EACH-FILE.md`. Seven citations closed the same day |

### Track A · Is the spec set sufficient?

| | Item | Cost |
|---|---|---|
| T-015 | A fresh model *describes* the structure from specs alone | **5 min** — needs a clean context |
| T-009 | Tag the seven `x_untested` capabilities | hours |
| T-014 | Delete a capability, see whether anything notices | afternoon |
| T-013 | Spark cost analyst AOAS — the **adversarial shape**, no writes, no turns, no approvals | a day |
| T-010 | Extract the shape constraints — **re-scope**: T-012 proved it is about boundaries, not existence | days |
| **T-008** | **Run the regeneration.** One repo; a second implementation is a second repo | weeks |

### Track B · Make it real — all of it blocked on P1

| | Item |
|---|---|
| **T-002** | No login exists, and the permission model cannot express ownership — **everything needs this** |
| T-001 | Nothing happens when the chat opens |
| **T-016** | Three of our own "do not build" entries are built here — the adopt/decline audit |
| T-017 | A real store, and a human in front of it |
| T-006 | A customer's own conversations cannot be found |
| T-003 | Dedup works for one process only |
| T-005 | Carry the idempotency key downstream — with T-003 |
| T-007 | Every scenario runs once, so nothing measures reliability |
| T-018 | The fingerprint cannot tell a gateway from a provider — decide with T-016's LiteLLM row |
| T-004 | Anthropic adapter at L2 — **subsumed by T-016's LiteLLM row**; keep for the argument |

### The adopt/decline register — decided, lives in T-016

| | Verdict |
|---|---|
| LiteLLM **SDK** | **not now** — 65 lines, not 636; `resilience/` is the fault-injection seam (F-029) |
| LiteLLM **proxy**, or any gateway | **when there is somewhere to run one** — via `provider_base_url`, no dependency. Decide with T-018 |
| OTLP processor → Langfuse | **adopt** — one line, cheapest thing on this page |
| Chatwoot Agent Bot API | **adopt** — needs T-002; does **not** solve approvals |
| Anthropic context editing + memory tool | **adopt at L2** — no framework, no loop change |
| VCR.py / pytest-recording | **adopt** — replaces `cassette/` |
| `pass^k`, and τ-bench's `verify` actor strategy | **adopt the idea** — T-007 |
| Temporal / LangGraph interrupts, LangGraph checkpointer | **evaluate** — L4, write the reason either way |
| The loop | **keep** — and write down why, because our own exception does not cover it |
| Guardrails AI / NeMo / Llama Guard | **decline** — none models *position* |
| NLU for intent | **decline** — two opinions already; a third has no owner |
| Model tier routing | **defer** — 4 orders of magnitude of budget headroom; seam is `LLMClient` |
| The Compactor | **decline** — provenance laundering |
| τ-bench's 165 tasks | **decline** — prose policy; read their retail doc instead |
| Hypothesis | ✅ **adopted 14 Sep** |
| Cosmic Ray | ✅ **used once to calibrate, not kept** |

### Start here

`T-015` (5 min) · then the **OTLP line** and **LiteLLM** from T-016 — both pure
subtraction, neither touches the thesis.

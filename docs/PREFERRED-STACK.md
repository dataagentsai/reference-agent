# The stack I would actually build this on

*Written 2026-09-05. Opinionated on purpose — a survey is not an answer.*

For **this** agent: customer support for an ecommerce shop, real money moving,
a human approving refunds, synchronous chat, multi-turn, high volume.

Different agent, different answer. Most of the reasoning below is *because it is
this one*, and I have said which part is doing the work each time.

---

## The four decisions

### A · Who owns the loop — **split, and this is the interesting call**

**Write the turn loop yourself. Put only the approval on a durable engine.**

The obvious recommendation is "use Temporal", and it is half right. This agent has
an approval that can wait an hour for a person, survive a restart, and must
produce exactly one refund. That is textbook durable execution, and three of our
modules exist because we lack it.

But **the chat turn is not that shape.** A customer asks where their order is and
expects an answer in under two seconds. Wrapping a two-second synchronous request
in a workflow adds a cluster round trip, latency and operating weight to buy
crash-safety for something that can simply be retried.

So:

| | |
|---|---|
| **the turn** | a plain in-process loop — hand-written, ~350 lines, what exists today |
| **the approval** | a Temporal workflow: raise, wait for a signal, execute, expire |

That split is not a compromise. **Durability is worth its cost exactly where work
spans time**, and a chat turn does not.

*What decides it:* the hour-long human wait. Remove that from the product and
the answer becomes "just write the loop" with no engine at all.

### B · Which API surface — **native, no gateway**

The system prompt plus the tool list is a **large, stable prefix repeated on
every turn of every conversation.** That is the precise shape prompt caching
exists for, and it is the largest cost lever available to a high-volume support
agent. An OpenAI-shaped request has no field to carry it, so a gateway forfeits
the single biggest saving on the page.

It also brings two things this repository specifically needs: `count_tokens`,
which turns our character-based context budget into a token one, and the Batch
API, which halves the cost of an offline eval suite that runs on every change.

*What decides it:* the stable prefix. A support agent with a short, per-request
prompt would lose nothing to a gateway, and portability might then be worth more.

### C · Who hosts the model — **first-party, unless procurement says otherwise**

First-party gets features on the day they ship. A cloud gets them later, and gets
you one bill and one identity system.

If the shop already runs on AWS, **take Bedrock and stop arguing** — the coupling
buys more than the lag costs, and the code barely changes.

### D · Where it runs — **containers, wherever the shop already is**

A support agent lives inside an existing estate. It does not get to have opinions
about the deployment target; it inherits them.

---

## The rest, layer by layer

| | Layer | Choice | Why this one |
|---|---|---|---|
| L1 | Context assembly | **ours**, plus server-side compaction | The fencing of untrusted content is the injection defence; nothing supplies it |
| L2 | Model invocation | **`anthropic` SDK** | See B |
| L3 | Tool layer | **MCP**, the shop's order system as the server | Mandatory `outputSchema` is what makes claim-grounding possible at all |
| L4 | Control loop | **ours** | The step budget, the oscillation check and the cost ceiling live here, and only here can see a trajectory |
| L5 | State and memory | **Postgres** | Already correct. One database, two schemas, no second store |
| L6 | I/O contracts | **ours** | Nobody supplies it — and a refusal must be a 200 |
| L7 | Policy | **ours**, plus **Presidio** for PII | Grounding against our own world is the check that catches what customers feel |
| L8 | Concurrency | the web server and the connection pool | Nothing exotic; a support agent is not a throughput problem |
| L9 | Determinism | **ours** — cassettes | A recording of what the model said, carrying the config it was made under |
| L10 | Failure | **ours** typed degradation + SDK retries + **Temporal** for the saga | Three levels, three lifetimes |
| L11 | Observability | **OpenTelemetry → Langfuse**, self-hosted | OTel because it is not a lock-in; Langfuse because it understands a trace shaped like this |
| L12 | Eval | **AgentTwin**, plus **promptfoo** for prompt-level checks | Nothing else can put an order into "delivered 31 days ago" |
| L13 | Cost | **ours**, from the API's own usage | Per successful task, not per call |
| L14 | Human-in-the-loop | **Temporal** for the wait; the approval screen is the shop's existing ops tool | Reviewers will not learn a new tool for one queue |
| L15 | Release | the shop's existing pipeline; a config fingerprint on every run | Prompt, model, rules and world together are the version |
| L16 | Identity | **the shop's existing login**, plus **our ownership rule** | See below |

### The L16 line is the one people get wrong

A support agent does **not** choose an identity provider. The shop already has
customers logging in, and the agent verifies whatever token that system issues —
Keycloak, Cognito, Auth0, whatever is already there. Introducing a second
identity system for the agent would be a mistake dressed as diligence.

What the agent **must** add is the part no provider supplies: **may this caller
act on this row.** That is F-016, it is critical, and it is fifteen lines in the
tool boundary driven by a declaration in the world file.

---

## What I would deliberately not use

**An agent framework.** The loop is about 350 lines and contains the step budget,
the oscillation detector and the cost ceiling. Handing L4 to a framework means
handing over the only position that can see a trajectory, to save writing 350
lines once. Bad trade for *this* agent. For a research assistant with a
complicated graph, a different answer.

**A model gateway.** Forfeits prompt caching, which is the largest cost lever
here. If a second provider becomes a hard requirement, revisit — and price what
the intersection costs before agreeing to it.

**A guardrails product as the primary control.** They screen for toxicity and
PII, which is real and worth having at the edges. They cannot tell you that
"your order was delivered" is false. That check needs the world.

**A vector database.** There is no retrieval in this agent. The rules are in a
file and the orders are in Postgres. Adding one would be a component with no job.

**A fully managed agent platform.** The approval gate and the ownership rule
*are* the product here. Renting a loop you cannot instrument gives away the two
things that make it trustworthy.

---

## And what this repository will keep running on

**Groq, with the OpenAI-shaped client.** Not the recommendation above, and
deliberately so: the exercise runs hundreds of times a day and must stay free,
and exposing the hand-written loop is the entire point of the artifact.

The honest way to hold both is the one this project keeps arriving at: **write
down what the choice costs.** Ours costs prompt caching, token counting and the
batch API — recorded as **T-004**, priced, and revisitable the day the constraint
changes.

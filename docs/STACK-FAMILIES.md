# Stack families

*Written 2026-09-05, answering a question first asked weeks ago: is the space of
possible stacks infinite, or finite?*

**Finite, and small.** The permutations are enormous — any loop, any client, any
store, any framework — but a stack is not a permutation. A stack is a set of
choices that **reinforce each other**, and coherence is a much stronger
constraint than compatibility.

There are roughly eight families. What makes each one a family is that its
choices share an **organising axis**: a vendor, a cloud, a protocol, a runtime
guarantee, or a framework. Inside a family the seams are cheap. Across families
you pay at every seam, which is fine when you chose to and expensive when you
did not notice.

---

## The eight

### 1 · Single-vendor native — Anthropic

**Organising axis:** one provider's full API surface.

`anthropic` SDK at L2 · your own loop, the tool runner, or the Agent SDK at L4 ·
MCP for tools · Managed Agents if you also want the deployment.

*You get:* everything the provider offers — prompt caching, token counting,
batch, adaptive thinking, server-side compaction, structured outputs. Nothing is
lost at a seam because there is no translation seam.

*You give up:* the ability to change provider without rewriting L2.

*Choose it when* cost per task and capability matter more than portability —
which, for a product with a large stable prompt prefix, is most of the time.

### 2 · Single-vendor native — OpenAI

The mirror image, and the argument is identical with the names swapped. Same
shape, same trade, different vendor.

### 3 · Cloud-platform native — AWS · GCP · Azure

**Organising axis:** the cloud contract you already signed.

Bedrock / Vertex / Foundry at L2 · the cloud's IAM at L16 · its secrets, queues
and observability · Step Functions or Durable Functions at L10.

*You get:* one bill, one identity system, one compliance story, and a
procurement conversation that is already finished.

*You give up:* features arrive later than on the first-party API, and some never
arrive. Availability is partner-gated.

*Choose it when* procurement or compliance is the binding constraint — which it
frequently is, and engineers under-weight it.

### 4 · Portable / multi-provider

**Organising axis:** the ability to switch.

A gateway at L2 — LiteLLM, Portkey, OpenRouter · your loop or a framework at L4.

*You get:* provider substitution, cost routing, failover, one interface.

*You give up:* **the intersection is all you can have.** A cross-provider
interface can only carry what every provider shares, so provider-specific
features are not merely inconvenient — they are unreachable by construction.

*Choose it when* portability is a **requirement**: a contract, a regulation, an
availability commitment. Not when it is a hope.

### 5 · Framework-first

**Organising axis:** one mental model for the whole application.

LangGraph, LangChain, Pydantic AI, Mastra, CrewAI at L4 · model access through
the framework's provider classes · often the framework's own tracing at L11.

*You get:* speed of assembly, an ecosystem, and other people's solutions to
problems you have not hit yet.

*You give up:* the framework owns your control loop, and **the loop is the only
position that can see a trajectory.** You also inherit its opinions about context
management, which are usually invisible until they are wrong.

*Choose it when* time to first working system dominates. And apply the test in
Q5: can you attach a provider-specific field, and can you see it took effect?

### 6 · Durable-execution-first

**Organising axis:** the guarantee that work survives a crash.

Temporal, Restate, DBOS, Inngest at L4 and L10 · the agent loop is a workflow,
each step an activity · any SDK inside an activity.

*You get:* crash safety, resumption, retries, and — the underrated part — a
process that can **sleep for an hour waiting for a human** and wake up correctly.
It subsumes checkpointing, run-once semantics, and the waiting half of approvals.

*You give up:* operational weight, and a genuine shift in how you think about
control flow.

*Choose it when* the work is long-running, moves money, or waits on a person.
**That is exactly this support agent**, which is why three of its modules are
re-implementations of this family.

### 7 · Self-hosted open-weight

**Organising axis:** the data never leaves.

vLLM, Ollama, TGI, SGLang · open-weight models · an OpenAI-shaped API by
convention.

*You get:* no data egress, capital rather than marginal cost, total control of
versioning, and no rate limits but your own hardware.

*You give up:* frontier capability, and you now operate a serving system.

*Choose it when* regulation, air-gapping, or volume economics decide it.

### 8 · Fully managed agent platform

**Organising axis:** buy the harness *and* the deployment.

Managed Agents, Bedrock Agents, Azure AI Agent Service · the loop and the
sandbox are both somebody else's.

*You get:* the least code of any option here, by a wide margin.

*You give up:* the loop is not yours, so nothing in it is yours to instrument,
bound or reason about.

*Choose it when* the agent is not the product.

---

## Which family is this repository?

A deliberate hybrid, and worth naming honestly: **hand-written loop** (family 1's
shape at L4) over an **OpenAI-shaped client pointed at open-weight models**
(family 7 at L2), with Postgres and OpenTelemetry.

That combination is coherent for a *teaching* artifact — the loop is exposed
because exposing it is the point, and the models are free because the exercise
runs hundreds of times a day. It is **not** the recommendation for a product.

**For production, this agent wants family 6 + family 1**: Temporal for durable
execution, the Anthropic SDK for the model. Those two do not conflict, for the
reason below.

---

## The test for combining families

Families combine cleanly when their organising axes are **different**, and fight
when they are the same.

| Combination | Conflict? |
|---|---|
| Durable execution (6) + native SDK (1) | **No.** One organises L4 and L10, the other L2. Different axes |
| LangGraph (5) + native SDK called inside a node (1) | **No**, provided the node body is yours |
| Gateway (4) + wanting provider-specific features (1) | **Yes.** Same layer, opposite goals — a gateway is *defined* by not exposing them |
| Agent SDK (1 at L4) + your own loop | **Yes.** Same layer, same axis, only one can win |
| Cloud-native (3) + native first-party features (1) | **Partly.** The cloud lags the first-party API; you get most, later |
| Managed platform (8) + custom control-loop rules | **Yes.** You cannot instrument a loop you do not run |

So the question to ask of any two choices is not *"do these work together"* —
almost everything works together. It is:

> **Do these two decisions want to own the same layer?**

If yes, one of them is decoration. If no, they compose, and the stack is coherent
however many families it draws from.

---

## Why this is finite

A stack has sixteen layers and each has several plausible answers, which is
combinatorially enormous. But the choices are not independent: picking a family
at one layer determines or strongly constrains several others, because that is
what a family *is*.

Which is the same reason the catalogue is finite. **Sixteen layers × eight
coherent families is a table somebody can read** — and the useful artifact is not
the list of families but the row underneath each: *you chose this one, here is
what it does not give you.*

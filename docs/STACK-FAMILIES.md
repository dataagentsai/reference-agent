# Stack choices — four axes, not eight families

*Written 2026-09-05 and **rewritten twice the same day**, both times because a
reader pointed out the list was sorted on the wrong thing. The corrections are
kept because they are more instructive than the answer.*

## The two mistakes worth keeping

**First draft: eight "families"** — Anthropic native, OpenAI native,
cloud-platform, portable, framework-first, durable-execution, self-hosted
open-weight, fully managed.

That list **mixed how you write code with where things run.** *Framework-first*
is a code-structure decision; *self-hosted open-weight* is a hosting and
weight-sourcing decision. They are not alternatives — **LangGraph on self-hosted
vLLM is perfectly coherent** — and the list implied a choice between them.

**Second draft: three axes**, and *cloud-platform* was still filed under
how-you-reach-the-model. It is not. **Bedrock and Vertex are hosting**, and the
code barely changes.

Both are the same error as doc 28, which sorted ten agents by framework and had
to be redone: **an axis that does not vary the thing you are reasoning about
should not be the axis you sort on.** Making it three times in one project is
worth writing down, because it is evidently not an easy mistake to stop making —
each version *looked* like a taxonomy right up until somebody asked what varied.

---

## Four decisions, and only one of them is about code

Corrected twice. The first draft had eight families; the second had three axes
and still filed *cloud-platform* under how-you-reach-the-model. **It is not** —
Bedrock and Vertex are hosting decisions. Your code still speaks the Messages
API; only the backend differs:

```python
Anthropic()                                   # first-party
AnthropicBedrockMantle(aws_region="...")      # AWS hosts it
AnthropicVertex(project_id=..., region=...)   # Google hosts it
```

Same `messages.create` surface, same request shape, same features expressible.
Different bill, different identity system, different region policy. That is
procurement and operations, not architecture.

So the axes are:

### A · Who owns the control loop  → L4

| | |
|---|---|
| **A1** | **You write it.** A `while` loop. What this repository does |
| **A2** | **A thin helper.** A tool runner — the loop with per-turn hooks, still your harness |
| **A3** | **A framework.** LangGraph, Pydantic AI, Mastra, CrewAI |
| **A4** | **A durable-execution engine.** Temporal, Restate, DBOS — the workflow *is* the loop |
| **A5** | **A vendor's agent SDK.** Loop, built-in tools and context management included |
| **A6** | **A hosted platform.** Somebody else runs it |

**The only axis that is about how you write code**, and the one that decides what
the harness still owes you — because the loop is the only position that can see a
*trajectory*.

### B · Which API surface you speak  → L2

| | |
|---|---|
| **B1** | **A provider's native surface.** The Messages API, the Responses API |
| **B2** | **An OpenAI-shaped surface.** Including compatibility endpoints in front of other providers |
| **B3** | **A gateway's own surface.** LiteLLM, Portkey — the intersection by construction |

**This axis decides which features you can express**, and nothing else does. It is
where prompt caching, structured outputs and thinking live or die, and it is
independent of who hosts anything.

### C · Who hosts the model

| | |
|---|---|
| **C1** | The provider, first-party |
| **C2** | A cloud — Bedrock, Vertex, Foundry |
| **C3** | A hosted open-weight provider — Groq, Together, Fireworks |
| **C4** | You, on your own GPUs — vLLM, Ollama, TGI |

**Decides what is actually available**, along with price, latency, region and
compliance. Features reach partner platforms later than first-party ones, and
some never arrive.

### D · Where your own process runs

| | |
|---|---|
| **D1** | Anywhere — VM, container, serverless, Kubernetes |
| **D2** | One cloud's managed services, for its identity, queues and billing |
| **D3** | A vendor's platform — you run nothing |
| **D4** | On-premises or air-gapped, usually forced by a data rule |

---

## The formulation that makes this useful

Two axes are constantly confused and they fail differently:

> **The surface decides what you can *express*.
> The host decides what is *available*.**

An OpenAI-shaped request **cannot express** `cache_control` — there is no field,
so no host can rescue it. A partner platform **may not yet serve** a feature your
surface can express perfectly well. Two different failures, two different fixes,
and conflating them is why teams try to solve a surface problem by changing
clouds.

---

## Where the axes are genuinely tied

Three ties, and they are the only thing resembling a bundle:

**A6 → D3.** If somebody else runs the loop, they host it. That is the offer.

**C4 → D1 or D4.** Self-hosting weights means having GPUs somewhere. The only
case where a decision here is really a hardware decision.

**C2 pulls D2.** Not forced — you can call Bedrock from anywhere — but the reason
to choose it is usually that the identity, billing and compliance story is
already there, and taking half the bundle wastes most of the benefit.

**Everything else composes freely.** A3 + B1 + C4 + D4 — LangGraph, native
surface, your own GPUs, air-gapped — is entirely coherent.

---

## The one test for a combination

> **Do these two decisions want to own the same layer?**

A5 + A1 is incoherent: the agent SDK and your own loop both want L4, and only one
can win. A4 + B1 is fine: Temporal owns L4 and L10, the SDK owns L2, and they
never meet.

The gateway is the subtle case. **B3 conflicts with no other axis** — it
conflicts with a *requirement*. If you need prompt caching, B3 cannot give it to
you whoever writes your loop and whoever hosts the model. That conflict is
between a choice and a goal, which is why it survives review so often: nothing on
the architecture diagram looks wrong.

---

## What this repository is

**A1 · B2 · C3 · D1.** A hand-written loop, speaking an OpenAI-shaped surface,
against a hosted open-weight provider, running anywhere.

Coherent for a *teaching* artifact: the loop is exposed because exposing it is
the entire point, and the models are free because the exercise runs hundreds of
times a day.

**Not the recommendation for a product**, which would be **A4 · B1 · C1 or C2 ·
D1 or D2** — durable execution for the loop, a native surface so features are
expressible, and hosting wherever the rest of the estate already lives.

---

## Why this is finite

Six loop choices × three surfaces × four model hosts × four deployment targets is
288, minus what the ties forbid. A large number and a useless one.

The useful observation is underneath it: **four decisions, and they answer
different questions.** A decides what the harness still owes you. B decides what
you can express. C and D are procurement and operations.

Which is why the coverage grid that follows is really a grid over **A** — the
other three barely move it. A team agonising over which cloud to use has usually
not yet made the decision that matters.

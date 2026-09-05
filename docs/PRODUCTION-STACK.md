# If you were building this for real

*Written 2026-09-05, rebuilt the same day.*

This repository hand-rolls the harness on purpose — you cannot write the sentence
*"you chose X, here is what X does not give you"* if you have never held the
problem. That is a **teaching** trade, and for a product it is the wrong way round.

So this is the other lens: what to adopt instead, and — the column that matters —
**what adopting it would still leave you holding.**

## Why this is organised by layer

The first version of this document was a list of concerns somebody thought of,
and it **missed four of the sixteen** — concurrency, determinism, cost and
release, which is twenty capabilities' worth of harness improvised straight past.

So it is now generated from AHC's sixteen layers and is complete by construction.
That is the catalogue's whole purpose demonstrated on its own author: *the empty
cells carry more information than the full ones.*

## The rule

**Nothing gets hand-built here without a row below.** Declining an off-the-shelf
answer is only defensible written down — with what we would use instead, and what
that would still leave us holding. It prevents building because nobody looked,
and it stops a reader mistaking the hand-rolled version for the recommendation.

---

## L1 · Context assembly

*What the model is told.*  ·  8 capabilities apply to this agent.

| | |
|---|---|
| **We built** | Prompt text and history assembly, fenced provenance, a character budget, whole-exchange trimming. |
| **Adopt** | **Langfuse**, **LangSmith**, **Braintrust**, **Humanloop**, **PromptLayer** for prompt versioning and rollout. |
| **Still yours** | **What a prompt version means for your baseline** — whether changing one invalidates the numbers you last committed. A prompt registry stores versions; it does not know that. |

## L2 · Model invocation

*How the call is made.*  ·  6 capabilities apply to this agent.

| | |
|---|---|
| **We built** | One adapter, an approved-model list checked against the provider, a typed boundary, three exits. |
| **Adopt** | **LiteLLM** (one interface, budgets, fallbacks), **OpenRouter**, **Portkey**, **Envoy AI Gateway**, or a cloud gateway — Bedrock, Vertex. |
| **Still yours** | **Which model for which job.** And that a gateway *hides* provider differences rather than removing them: cache control and thinking blocks do not survive an OpenAI-shaped shim, whoever writes it. |

## L3 · Tool layer

*What the model can do.*  ·  6 capabilities apply to this agent.

| | |
|---|---|
| **We built** | Schema validation at dispatch, side-effect classes, result bounding, a scoped tool surface. |
| **Adopt** | **MCP** itself, which we use — plus the provider's own function-calling. |
| **Still yours** | **Which tools this caller may see.** Filtering the surface by scope is what makes the refund tool *invisible* rather than refused, and no protocol does that for you. |

## L4 · Control loop

*When the system stops.*  ·  5 capabilities apply to this agent.

| | |
|---|---|
| **We built** | A hand-written ReAct loop: step budget, oscillation detection, cost ceiling, typed terminations. |
| **Adopt** | **LangGraph**, **Pydantic AI**, **OpenAI Agents SDK**, **Claude Agent SDK**, **Mastra**, **CrewAI**. |
| **Still yours** | **The biggest decision on this page.** A framework owns the loop, and the loop is the only position that can see a *trajectory*. If you cannot say where your step budget, oscillation check and cost ceiling live after adopting one, it has adopted you. |

## L5 · State and memory

*What is remembered.*  ·  2 capabilities apply to this agent.

| | |
|---|---|
| **We built** | Conversation checkpoints under two keys, an idempotency ledger, an approval store, a delivery log. |
| **Adopt** | **Postgres** — which is what we use and is correct. **Redis** for the ephemeral parts. |
| **Still yours** | **What is worth remembering.** A store holds whatever you give it; deciding that a trace explains a past run while a checkpoint continues one is the design, and nothing supplies it. |

## L6 · I/O contracts

*What the caller can rely on.*  ·  6 capabilities apply to this agent.

| | |
|---|---|
| **We built** | Typed results on every route, a small HTTP edge, refusals as results rather than errors. |
| **Adopt** | **FastAPI**/**Starlette**, **Kong**, **APISIX**, **Envoy**, **Traefik**. |
| **Still yours** | **That a refusal is a 200.** The agent worked and the answer was no. Every framework will let you return a 4xx and turn correct behaviour into an error rate. |

## L7 · Policy enforcement

*Where a rule becomes real.*  ·  6 capabilities apply to this agent.

| | |
|---|---|
| **We built** | Four enforcement positions, claim grounding against tool results, PII patterns, fail-closed. |
| **Adopt** | **NeMo Guardrails**, **Guardrails AI**, **Llama Guard**, **Bedrock Guardrails**, **Presidio** for PII. |
| **Still yours** | **Grounding against your own data.** They know what toxic looks like. None of them knows AB-10003 is *shipped*, so none can tell you the reply claiming it was delivered is false — and that is the failure customers actually experience. |

## L8 · Concurrency and flow control

*What happens under load.*  ·  6 capabilities apply to this agent.

| | |
|---|---|
| **We built** | Bounded fan-out within one unit of work, a throttle, a circuit breaker. |
| **Adopt** | Gateway rate limiting, queue concurrency controls, **LiteLLM** rate limits, **Envoy** circuit breaking. |
| **Still yours** | **What one unit of work is.** A rate limiter counts requests; a runaway agent is fourteen correct requests that are one task. Only the loop can tell the difference, and it is also where blast radius belongs. |

## L9 · Determinism and replay

*Whether yesterday can be reproduced.*  ·  3 capabilities apply to this agent.

| | |
|---|---|
| **We built** | Cassettes carrying the configuration they were recorded under, seeds, declared determinism classes. |
| **Adopt** | **vcrpy**/**betamax** at the HTTP level; **Langfuse** or **LangSmith** dataset replay. |
| **Still yours** | **That a run is only as reproducible as its weakest actor.** No recorder tracks that, and a model-driven customer makes replay impossible however good the recording is. That is a property of your test design, not of a library. |

## L10 · Failure handling

*What happens when something breaks.*  ·  7 capabilities apply to this agent.

| | |
|---|---|
| **We built** | Typed degradation on provider failure, retries, a breaker, compensation, tool failure as a loop state. |
| **Adopt** | **Temporal**, **Restate**, **DBOS** for durable execution; **tenacity**/**backoff** for the small case. |
| **Still yours** | **Which failures are recoverable *for this business*.** A retry library retries; deciding that a refused refund is a result and a malformed model reply is not, is yours. |

## L11 · Observability

*Whether a past run can be explained.*  ·  7 capabilities apply to this agent.

| | |
|---|---|
| **We built** | OpenTelemetry spans, GenAI semantic conventions, and a **span contract** that fails the build on an undeclared attribute. |
| **Adopt** | **Langfuse** (self-hostable), **Arize Phoenix**, **LangSmith**, **W&B Weave**, **Helicone**, or plain OTel into **Tempo**/**Jaeger**. |
| **Still yours** | **The contract.** Every one of those will store whatever you send and never tell you a field is missing. *Complete against what?* is the question that makes tracing useful rather than decorative, and only you can answer it. |

## L12 · Eval harness

*Whether the system can be measured at all.*  ·  5 capabilities apply to this agent.

| | |
|---|---|
| **We built** | A golden set generated from the world, a frozen baseline, four oracles, a conformance report, AgentTwin. |
| **Adopt** | **promptfoo**, **DeepEval**, **Ragas**, **Inspect**, **Braintrust**, **Langfuse** datasets; **LangWatch Scenario** for a simulated user. |
| **Still yours** | **The world and the oracle.** All of them grade *outputs*. None can put an order into 'delivered 31 days ago', inject a stale read mid-run, or diff the world before and after — because none of them owns your data. |

## L13 · Cost accounting

*What it costs and who pays.*  ·  6 capabilities apply to this agent.

| | |
|---|---|
| **We built** | A meter recording per call and per task, a ceiling checked between calls, cost in the committed baseline. |
| **Adopt** | **LiteLLM** budgets, **Portkey**, **Helicone**, **Langfuse** cost tracking, cloud billing exports. |
| **Still yours** | **Cost per *successful task*, not per call.** Every tool measures per call. The number that matters needs to know what finished means, and that is your definition. |

## L14 · Human-in-the-loop

*Where a person can intervene.*  ·  2 capabilities apply to this agent.

| | |
|---|---|
| **We built** | An approval that returns rather than blocks, expiry, no self-approval, a stored key so a double grant refunds once. |
| **Adopt** | **Temporal** signals, **Camunda/Zeebe**, or a Slack/ServiceNow workflow. |
| **Still yours** | **What needs a human, and what a stale approval means.** A workflow engine will wait as long as you like; deciding that a grant made three days ago is a decision nobody made about today is a policy. |

## L15 · Release and configuration

*Which version is running.*  ·  5 capabilities apply to this agent.

| | |
|---|---|
| **We built** | A config fingerprint on every run, an approved-model list, versioned routing rules, run-once delivery (AHC-0053). |
| **Adopt** | **OpenFeature** + **Unleash**/**Flagsmith**, **LaunchDarkly**, **ArgoCD**/**Helm**; queue-level dedup for the delivery half. |
| **Still yours** | **What constitutes a version of *this agent*** — prompt plus model plus rules plus world. A flag system flips a value; it does not know that flipping this one invalidated your baseline. |

## L16 · Identity and authorization

*Who the system is acting as.*  ·  3 capabilities apply to this agent.

| | |
|---|---|
| **We built** | Token minting and verification, scopes, a filtered tool surface, just-in-time elevation for refunds. |
| **Adopt** | **Keycloak**, **Zitadel**, **Authentik**, **Ory**; managed: Auth0, Cognito, Entra. |
| **Still yours** | **Whether this caller may act on *this row*.** No identity provider answers it. That is F-016, it is critical, and buying one would have left it exactly where it is. |

---

## What an architect should do

**Adopt these four without hesitating.** Strictly better than anything you will
write, and none constrains what makes the system yours: an **identity provider**
(L16), **durable execution** (L10 — Temporal subsumes most of L5, L14 and the
delivery half of L15), a **model gateway** (L2), a **trace store** (L11).

**Be careful with two.** A **guardrails product** (L7) makes you feel covered
while leaving the grounding check unwritten. An **agent framework** (L4) owns the
control loop, which is the only position that can see a trajectory.

**Write these yourself, always** — not because they are hard, but because nothing
else can:

- the ownership rule — *may this caller act on **this**?* (L16)
- the span contract — *complete against **what**?* (L11)
- the world your tests run against, and which oracle answers (L12)
- grounding a claim against your own data (L7)

## The honest summary

Roughly **80% of what this repository builds is available off the shelf and better**.
That is the exercise, not a criticism of it. The value is the remaining 20%, and
you cannot see which 20% it is without having built the 80% once.

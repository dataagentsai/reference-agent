# If you were building this for real

*Written 2026-09-05. The companion to everything else in this repository, and the
one document here that says **do not build it**.*

This repository hand-rolls the harness on purpose. The purpose is to know what is
in it — you cannot write the sentence *"you chose X, here is what X does not give
you"* if you have never held the problem. That is a **teaching** trade, and for a
product it is the wrong way round.

So this is the other lens. For each concern: what we built, what you would buy or
adopt instead, and — the column that matters — **what it still leaves you
holding.**

That last column is the point. Every one of these tools solves a real problem and
none of them solves *your* problem, because your problem includes your data, your
rules and your definition of correct. A catalogue exists to name the remainder.

## This is a standing register, not a one-off

**Rule: nothing gets hand-built in this repository without a row here.** Every
concern we solve ourselves has an off-the-shelf answer we are declining, and the
declining is only defensible if it is written down — with what we would use
instead, and what that would still leave us holding.

Two failures this prevents, and both are common.

*Building something because nobody looked.* An entry that says "adopt: nothing
suitable" is a claim somebody can check. An empty page is not.

*Believing the hand-rolled version is the recommendation.* A reader arriving at
this repository should be able to tell, per concern, whether we are demonstrating
a mechanism or recommending an implementation. Those are different documents and
this is the one that separates them.

When a concern is added, its row is added. When a tool in the middle column is
superseded, the row is updated rather than the fact quietly ageing.

---

## The map

### Identity and permission

| | |
|---|---|
| **We built** | JWT minting and verification, scopes on the token, just-in-time elevation for refunds |
| **Adopt** | **Keycloak** (institutional, heavy, complete), **Zitadel** (Go, multi-tenant by design), **Authentik** (lighter admin), **Ory Hydra + Kratos** (parts, not a product). Managed: Auth0, Cognito, Entra ID |
| **Still yours** | **Whether this caller may act on *this row*.** No identity provider answers it. It is F-016, it is critical, and buying an IdP would have left it exactly where it is |

| | |
|---|---|
| **We built** | nothing — this is the gap |
| **Adopt** | **OpenFGA** or **SpiceDB** (relationship rules, Zanzibar), **Cedar** (readable policy, AWS), **OPA** (general, Rego), **Casbin** (in-process, no service) |
| **Still yours** | Deciding the rules, and resisting these until the rule is more complex than *"it is yours"* — a network call to answer a field comparison is a poor trade |

### The edge

| | |
|---|---|
| **We built** | a Starlette app: decode, verify, load conversation, dispatch |
| **Adopt** | **Kong**, **APISIX**, **Envoy**, **Traefik** for the gateway; any load balancer |
| **Still yours** | **Minting a stable per-message id.** A gateway that retries on timeout is a duplicate *generator*, and the LB is what put two copies behind one address in the first place |

### Run-once, and surviving a crash

| | |
|---|---|
| **We built** | `trigger` (claim/settle), `state` (checkpoints), the waiting half of `approvals` |
| **Adopt** | **Temporal** (the standard answer), **Restate**, **DBOS** (Postgres-native), **Inngest**, **Trigger.dev**; managed: Step Functions, Durable Functions |
| **Still yours** | What a step *is*, and what must be checkpointed. Also: a workflow engine makes your code durable, not your *effects* idempotent — the far end still needs to tolerate a repeat |

**This is the largest single adoption on the list.** Those three modules exist
largely because we lack durable execution, and the long human wait matters more
than the deduplication: a workflow can sleep for an hour waiting on an approver
and wake up correctly, which is most of what `approvals` is.

| | |
|---|---|
| **We built** | an idempotency ledger keyed by run, step and iteration |
| **Adopt** | queue-level deduplication — **SQS FIFO**, **Azure Service Bus**, **Pub/Sub** exactly-once, **Kafka** idempotent producer. Or nothing, if the effects are made harmless to repeat |
| **Still yours** | The stable id, again. And the far end: a conditional `UPDATE … WHERE status IN (…)` with a row count removes the problem rather than guarding it |

### Talking to the model

| | |
|---|---|
| **We built** | one adapter, an approved-model list, a typed boundary, cost metering |
| **Adopt** | **LiteLLM** (the obvious one — one interface, 100+ providers, budgets, fallbacks), **OpenRouter**, **Portkey**, **Envoy AI Gateway**; or a cloud gateway — Bedrock, Vertex |
| **Still yours** | **Which model for which job**, and the fact that a gateway hides the difference between providers rather than removing it. Anthropic's cache control and thinking blocks do not survive an OpenAI-shaped shim, whoever writes it |

| | |
|---|---|
| **We built** | prompts as constants; routing rules carry a version, the prompt does not |
| **Adopt** | **Langfuse**, **LangSmith**, **Braintrust**, **Humanloop**, **PromptLayer** for prompt versioning and rollout; **OpenFeature** + **Unleash**/**Flagsmith** if you want prompt changes to ride the same flag system as everything else |
| **Still yours** | What a prompt *version* means for your evaluation baseline, and whether a prompt change is a deploy |

### Seeing what happened

| | |
|---|---|
| **We built** | OpenTelemetry spans, GenAI semantic conventions, and a **span contract** that fails the build when a span carries an undeclared attribute |
| **Adopt** | **Langfuse** (open source, self-hostable), **Arize Phoenix**, **LangSmith**, **W&B Weave**, **Helicone**; or plain OTel into **Grafana Tempo**/**Jaeger** |
| **Still yours** | **The contract.** Every one of those tools will happily store whatever you send and never tell you a field is missing. *Complete against what?* is a question only you can answer, and it is the one that makes tracing useful rather than decorative |

### Knowing whether it works

| | |
|---|---|
| **We built** | a golden set generated from the world, a frozen baseline, four oracles, a conformance report |
| **Adopt** | **promptfoo** (config-driven, cheap to start), **DeepEval**, **Ragas** (retrieval), **Inspect** (UK AISI, rigorous), **Braintrust**, **Langfuse** datasets, **OpenAI Evals** |
| **Still yours** | **The world and the oracle.** Every one of these grades *outputs*. None of them can put an order into "delivered 31 days ago", inject a stale read mid-run, or diff the world before and after — because none of them owns your data |

| | |
|---|---|
| **We built** | AgentTwin — a declared world, projected tools, actors, perturbations |
| **Adopt** | **LangWatch Scenario** for the multi-turn simulated user with an LLM judge |
| **Still yours** | The **world** half. Scenario simulates the *user* and runs against your *real* backends; AgentTwin simulates the *systems*. They are complementary, and if you ever want a model-driven customer you should use theirs rather than build one |

### Stopping it saying something wrong

| | |
|---|---|
| **We built** | policy at four positions, claim-grounding against tool results, PII patterns |
| **Adopt** | **NeMo Guardrails**, **Guardrails AI**, **Llama Guard**, **Bedrock Guardrails**; **Presidio** for PII specifically |
| **Still yours** | **Grounding against your own data.** A guardrails product knows what toxic looks like. It does not know that AB-10003 is *shipped*, so it cannot tell you the reply claiming it was delivered is false. That check requires your world, and it is the one that catches the failure customers actually experience |

### The rest

| Concern | Adopt |
|---|---|
| Queue | SQS, Service Bus, Pub/Sub, **Kafka**, **NATS**, **RabbitMQ** |
| State | Postgres — which is what we use, and correct |
| Secrets | **Vault**, cloud secret managers, **SOPS** for files |
| Approvals as a product | Temporal signals, **Camunda/Zeebe**, or a Slack/ServiceNow workflow |
| Cost control | **LiteLLM** budgets, **Portkey**, gateway quotas |
| The agent loop itself | **LangGraph**, **Pydantic AI**, **OpenAI Agents SDK**, **Claude Agent SDK**, **Mastra**, **CrewAI** |

---

## What an architect should actually do

**Adopt these four without hesitation.** They are strictly better than anything
you will write, and none of them constrains the parts that make your system
yours.

1. **An identity provider.** You will not write a better one, and it fixes
   asymmetric signing for free.
2. **Durable execution** — Temporal or equivalent. It subsumes three of our
   modules and handles the long human wait properly.
3. **A model gateway** — LiteLLM at minimum. Failover, budgets and one interface
   are not worth hand-writing.
4. **A trace store** — Langfuse if you want it self-hosted. Do not build a
   dashboard.

**Be careful with these two.**

*A guardrails product* will make you feel covered while leaving the grounding
check — the one that catches a confident false statement about your own data —
entirely unwritten.

*An agent framework* is the biggest decision on the page. It will own your control
loop, and the control loop is the only position that can see a trajectory: that
these fourteen calls are one runaway task rather than fourteen tasks. Adopt one
knowingly. **If you cannot say where your step budget, your oscillation check and
your cost ceiling live after adopting it, you have not adopted it — it has
adopted you.**

**Write these yourself, always.** Not because they are hard, but because nothing
else can:

- the ownership rule — *may this caller act on **this**?*
- the span contract — *complete against what?*
- the world your tests run against, and the choice of oracle
- grounding a claim against your own data

---

## The honest summary

Roughly **80% of what this repository builds is available off the shelf and better
than what we wrote.** That is not a criticism of the exercise — it is the
exercise. The value is the remaining 20%, and you cannot see which 20% it is
without having built the 80% at least once.

Which is the same finding doc 29 reached from the other direction: *the techniques
all exist and are mature; what does not exist is a way to describe your world well
enough to point them at it.*

# Every family against all sixteen layers

*Generated 2026-09-05 from `taxonomy/layers.yaml`, not written by hand.*

**● given** — the family supplies it · **◐ partly** — supplies a base, you finish ·
**○ yours** — you build it whichever family you pick.

---

## The grid

| | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| **L1** Context assembly | ◐ | ◐ | ◐ | ○ | ● | ○ | ○ | ● |
| **L2** Model invocation | ● | ● | ● | ● | ● | ○ | ● | ● |
| **L3** Tool layer | ◐ | ◐ | ◐ | ○ | ● | ○ | ○ | ● |
| **L4** Control loop | ○ | ○ | ◐ | ○ | ● | ● | ○ | ● |
| **L5** State and memory | ○ | ○ | ● | ○ | ◐ | ● | ○ | ● |
| **L6** I/O contracts | ◐ | ◐ | ◐ | ○ | ◐ | ○ | ○ | ◐ |
| **L7** Policy enforcement | ○ | ○ | ◐ | ○ | ○ | ○ | ○ | ◐ |
| **L8** Concurrency and flow control | ○ | ○ | ● | ◐ | ◐ | ◐ | ◐ | ● |
| **L9** Determinism and replay | ○ | ○ | ○ | ○ | ◐ | ● | ◐ | ○ |
| **L10** Failure handling | ◐ | ◐ | ◐ | ◐ | ◐ | ● | ○ | ● |
| **L11** Observability | ○ | ○ | ● | ◐ | ● | ◐ | ○ | ● |
| **L12** Eval harness | ○ | ○ | ◐ | ○ | ◐ | ○ | ○ | ○ |
| **L13** Cost accounting | ◐ | ◐ | ● | ● | ◐ | ○ | ● | ◐ |
| **L14** Human-in-the-loop | ○ | ○ | ◐ | ○ | ◐ | ● | ○ | ● |
| **L15** Release and configuration | ○ | ○ | ● | ○ | ○ | ◐ | ◐ | ● |
| **L16** Identity and authorization | ○ | ○ | ● | ○ | ○ | ○ | ◐ | ◐ |

Totals:

| family | given | partly | yours |
|---|---|---|---|
| 1 · Anthropic native | 1 | 5 | 10 |
| 2 · OpenAI native | 1 | 5 | 10 |
| 3 · Cloud-platform native | 7 | 8 | 1 |
| 4 · Portable / gateway | 2 | 3 | 11 |
| 5 · Framework-first | 5 | 8 | 3 |
| 6 · Durable execution | 5 | 3 | 8 |
| 7 · Self-hosted open-weight | 2 | 4 | 10 |
| 8 · Fully managed | 10 | 4 | 2 |

---

## The result that matters

**Three layers no family ever fully supplies.** Whichever stack you pick, these
arrive unfinished:

- **L6 · I/O contracts** — `PPPYPYYP`
- **L7 · Policy enforcement** — `YYPYYYYP`
- **L12 · Eval harness** — `YYPYPYYY`

That is not a coincidence, and it is the same three this repository arrived at
from the opposite direction:

**L7** is where *grounding a claim against your own data* lives. Guardrail
products know what toxic looks like; none of them knows that AB-10003 is
*shipped*, so none can tell you the reply claiming it was delivered is false.

**L12** is the world and the oracle. Every eval tool grades **outputs**. None can
put an order into *delivered 31 days ago*, inject a stale read mid-run, or diff
the world before and after — because none of them owns your data.

**L6** is what the caller can rely on. A framework will happily let you return a
4xx for a correct refusal and turn correct behaviour into an error rate.

And a fourth worth naming even though one family scores it ● : **L16**. The cloud
gives you identity completely — and *who may act on this row* is still yours,
which is F-016 and is critical here. **A layer scored 'given' can still leave the
part that matters unwritten**, which is the limit of reading this grid at layer
granularity.

---

## Where each family can be hosted

**1 · Anthropic native** — Anywhere you can run a process — VM, container, serverless, Kubernetes. The model is a network call, so hosting is unconstrained.

**2 · OpenAI native** — Same — unconstrained. The Agents SDK and Responses API are network calls like any other.

**3 · Cloud-platform native** — That cloud, and only that cloud. Which is the point rather than a limitation — the coupling is what buys the identity, billing and compliance story.

**4 · Portable / gateway** — Anywhere. The gateway is either SaaS or a container you run beside the app.

**5 · Framework-first** — Anywhere; several offer a managed control plane if you want the graph hosted rather than the process.

**6 · Durable execution** — A workflow cluster — managed cloud or self-hosted — plus workers anywhere. Two things to run instead of one.

**7 · Self-hosted open-weight** — Wherever the GPUs are: on-premises, colocation, or rented cloud GPU. The only family where hosting is a hardware decision.

**8 · Fully managed** — You host nothing. The provider runs the loop and the sandbox — which is the whole proposition, and the whole cost.

---

## How to read this

The grid is not a scoreboard. Family 8 scores ten ● and that is not a
recommendation — it means ten layers are somebody else's, including the control
loop, which is the only position that can see a trajectory.

The useful reading is per row: *for the family I have chosen, which of these
sixteen arrive unfinished, and did I plan for them?* Three of them always do.

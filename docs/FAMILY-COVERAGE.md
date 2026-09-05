# What each loop owner leaves you holding

*Generated from `taxonomy/layers.yaml`. Regenerated 2026-09-05 when the axis was
corrected from “eight families” to **who owns the control loop**, because most of
those families were hosting decisions and hosting barely moves this table.*

**●** supplied · **◐** partly, you finish · **○** yours · **—** decided by which
API surface you speak, not by who writes the loop.

---

| | A1 | A2 | A3 | A4 | A5 | A6 |
|---|---|---|---|---|---|---|
| **L1** Context assembly | ○ | ◐ | ● | ○ | ● | ● |
| **L2** Model invocation | — | — | — | — | — | ● |
| **L3** Tool layer | ○ | ○ | ● | ○ | ● | ● |
| **L4** Control loop | ○ | ◐ | ● | ● | ● | ● |
| **L5** State and memory | ○ | ○ | ◐ | ● | ◐ | ● |
| **L6** I/O contracts | ○ | ◐ | ◐ | ○ | ◐ | ◐ |
| **L7** Policy enforcement | ○ | ○ | ○ | ○ | ◐ | ◐ |
| **L8** Concurrency and flow control | ○ | ○ | ◐ | ◐ | ◐ | ● |
| **L9** Determinism and replay | ○ | ○ | ◐ | ● | ◐ | ○ |
| **L10** Failure handling | ○ | ◐ | ◐ | ● | ◐ | ● |
| **L11** Observability | ○ | ○ | ● | ◐ | ◐ | ● |
| **L12** Eval harness | ○ | ○ | ◐ | ○ | ○ | ○ |
| **L13** Cost accounting | ○ | ○ | ◐ | ○ | ◐ | ◐ |
| **L14** Human-in-the-loop | ○ | ○ | ◐ | ● | ◐ | ● |
| **L15** Release and configuration | ○ | ○ | ○ | ◐ | ◐ | ● |
| **L16** Identity and authorization | ○ | ○ | ○ | ○ | ○ | ◐ |

## Five layers no loop owner ever fully supplies

**L6 · I/O contracts** — What the caller can rely on. A framework will happily let you return a 4xx for a correct refusal and turn correct behaviour into an error rate.

**L7 · Policy enforcement** — Where grounding a claim against *your own data* lives. Guardrail products know what toxic looks like; none knows AB-10003 is *shipped*.

**L12 · Eval harness** — The world and the oracle. Every eval tool grades *outputs*. None can set an order to “delivered 31 days ago” or diff the world before and after.

**L13 · Cost accounting** — Cost per *successful task*. Everything measures per call; the useful number needs your definition of finished.

**L16 · Identity and authorization** — Not identity — *ownership*. Whether this caller may act on **this row**. That is the critical defect on this page, and no loop owner touches it.

Each needs something only you have: **your data, your definition of correct,
and your rules about who owns what.** No vendor can supply those — which is why a
catalogue that names them is worth more than a list of products.

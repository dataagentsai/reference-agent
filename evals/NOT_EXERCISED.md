# What this suite does not cover

Five of the 43 A6 obligations have no test behind them. Each is listed here with
why, and with what would have to change for it to be covered.

**This file is the point of the exercise, not an apology for it.** Under a
management-system audit an identified, owned, dated gap is conformant while an
undiscovered one is a finding — so the honest artifact and the audit-optimal
artifact are the same artifact. A conformance report that listed only passes
would imply the remainder was fine.

There is deliberately **no "not applicable" verdict** in the report. It would be
the right label for three of the five, and it would also be the label every
inconvenient obligation eventually acquired. `not_exercised` stays uncomfortable
on purpose.

*State as of 2026-09-01 — 38/43 exercised, 0 failed.*

---

## Release gates with nothing behind them

### AAC-0092 — Streamed output is screened before it reaches the caller

**Why not covered.** This agent does not stream. `ModelResponse` is returned
whole, and `policy` inspects the complete reply before the customer sees any of
it, so there is no partial output to screen.

**What would change it.** Adding streaming. The obligation exists because
screening a stream is a genuinely different problem — a rule that needs the
whole reply cannot fire until the reply is over, by which point the customer has
read the first half. If streaming is ever added, this becomes the hardest
obligation in the set and not the easiest.

### AAC-0096 — Cached responses never cross a trust boundary

**Why not covered.** No response cache exists. The cassette is a *recording*
replayed only in tests and never serves a customer; a cassette in production
would be exactly the failure this obligation describes.

**What would change it.** Any response caching, including a prompt cache keyed
on anything a customer can influence. The failure mode is one tenant's answer
served to another, and the boundary is the cache key.

---

## Not gating

### AAC-0007 — Latency within budget at expected concurrency

**Why not covered.** Every test either replays a cassette or scripts the model,
so measured latency here is the speed of a dictionary lookup. A number produced
that way would be worse than no number, because it would be quoted.

**What would change it.** Load against a live provider — Phase E. `flow` already
has the fan-out limiter this would measure.

### AAC-0014 — Continuous scoring of sampled production traffic

**Why not covered.** Stage S5. There is no production traffic; there is one
recorded run. This cannot be simulated, because the whole value of S5 is that it
sees the real input distribution rather than the one somebody imagined.

**What would change it.** A deployment. Nothing else.

### AAC-0097 — Processing region is enforced and recorded

**Why not covered.** Provider configuration we do not currently set or assert.
The endpoint is recorded in `RunConfig` and appears in the fingerprint, so the
*record* half is arguably present; the *enforce* half is not, and claiming half
an obligation is worse than claiming none.

**What would change it.** A provider that offers a region parameter, and a
deployment with a reason to pin it.

---

## Two obligations exercised but not tagged A6

Separate from the above, and pointing the other way. `AAC-0046` and `AAC-0047`
have passing tests behind them and are tagged `A5` only, so the report lists
them under *exercised but not tagged A6*.

That is not a defect in this suite. It is evidence for a catalog change — the
one recorded as **G1** — and the conformance report now produces it on every run
rather than requiring someone to argue for it.

`AAC-0047` is *"Retried steps are idempotent — no double write, no double
charge"*. A5's control flow belongs to your code, so you can see the retry. A6's
belongs to the model, which can re-call a tool for reasons nobody wrote. Double
refund is an A6 failure, and A6 does not currently claim the obligation.

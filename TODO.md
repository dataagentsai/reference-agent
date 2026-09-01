# Build sequence

Ordered. Each phase has a gate — a thing that is true afterwards and was not
true before. Nothing moves on until its gate holds.

The import contract in `pyproject.toml` is the module checklist: a module in
`(parentheses)` is declared but unbuilt, and the parentheses come off as it
lands. When none remain, Phase A is done.

---

## Phase A — finish the harness ✔ **COMPLETE**

Ordered by what unblocks what, not by size.

- [x] **A1 · `cost`** ✔ (L13) — price map, usage → money, attribution at P3 and the
      ceiling at P4.
      *Why first:* the loop bounds **steps, not spend**. A model emitting 100k
      tokens per step sits comfortably inside a 12-step budget and costs real
      money. AAC-0008 is cost per *successful task*, not per call.
      *Gate:* a run that would exceed `max_cost_usd` stops with
      `COST_CEILING_REACHED`.

- [x] **A2 · `approvals`** ✔ (L14, P6+P8) — queue, decision record, resumption.
      Wired through `Agent.handle()` via a **harness-local** `request_refund`
      tool — answered by the agent, never dispatched over MCP, because the
      approval lives in agent-owned state and no tool on the business server
      could create one without putting the oracle inside the simulated world.
      *Why second:* it closes the only functional path in doc 24 that cannot
      currently complete. `state.Conversation.pending_approval_id` is already
      waiting for it.
      *Decision settled:* **own queue, not MCP elicitation.** Elicitation asks
      the party on the other end of the connection and resolves inside one tool
      call; an approval is decided by a different person and may span an hour and
      a restart. How resumption is *signalled* stays a detail.
      *Gate:* refund over the threshold returns `NeedsApproval`, checkpoints, and
      a later turn resumes and completes it — with exactly one refund row.

- [x] **A3 · `cassette`** ✔ (L9) — record and replay at the L2 choke point.
      *Why third:* the first genuine piece of AgentTwin. Turns `ScriptedClient`
      from a hand-written fixture into recordings of real runs, and makes
      AHC-0029 possible — a production record becoming a dataset row without
      re-keying, which is the whole fix-and-regress loop.
      *Gate:* a run recorded once replays byte-identically with no network.

- [x] **A4 · `policy`** ✔ (L7, P3+P5) — middleware at four enforcement points.
      *Why fourth:* most surface, and it benefits from knowing what the three
      above need. Today nothing checks what the model **says** — that is most of
      test family F6.
      *Gate:* fails closed (AAC-0091). A guardrail that errors and lets traffic
      through is worse than none, because it is believed.

- [x] **A5 · `flow`** ✔ (L8) — fan-out limits, backpressure, throttling.
      *Concretely:* the loop runs parallel `tool_calls` sequentially today, and
      Groq's free-tier limits make a 429 storm realistic with no coordinated
      response.

- [x] **A6 · `resilience`** ✔ (L10) — retries, breaker, fallback, **compensation**.
      *The half that matters:* idempotency stops an effect happening twice;
      compensation undoes one that should not have happened once. AHC-0058. The
      refund path needs both.

**Phase A gate:** no parentheses left in the layers contract. All sixteen layers
have a module, so the coverage delta for this agent reads "none".

---

## Phase B — the first real model call

- [x] **B1** ✔ — one live turn against Groq, recorded, then replayed offline.
      `scripts/first_real_call.py`, cassette in `cassettes/`. 2 calls, $0.000183.

      **It found two defects in one run, and neither was findable offline:**

      *The model allowlist was stale.* `llama-3.3-70b-versatile` and
      `llama-3.1-8b-instant` return 404 — the provider no longer serves them.
      Nothing in a scripted suite can notice a model being retired. Now
      `openai/gpt-oss-120b`, verified against the provider's `/models`.

      *The wire format dropped assistant `tool_calls`.* A `tool` message
      referenced an id whose originating turn was not in the transcript, so the
      provider could not render it — `"Tools should have a name!"`, a 400 on the
      second call of every tool-using conversation. 222 passing tests never saw
      it because none of them serialised anything.

- [x] **B2** ✔ — Postgres, natively. It was already installed and running, so
      nothing was added to a machine with ~70 MB free. Two schemas, three durable
      stores, `sql/001_schemas.sql`. *Gate held:* an approval raised by one pool
      is recovered, granted and made executable by another, under the original
      idempotency key.

---

## Phase C — evals

- [x] **C1** ✔ — the 43 A6 obligations vendored as `evals/a6_obligations.json`
      (ids and metadata only — the normative statement stays in the catalog), a
      `@pytest.mark.discharges(...)` marker, and a conformance report printed at
      the end of every run. **32/43 exercised, 0 failed, 11 not exercised.**
- [~] **C2** — the seven case families from doc 25. F1 (eligibility grid), F2,
      F3, F4, F6 and F7 have coverage; F5 (degradation) is partial. See
      `evals/NOT_EXERCISED.md` for what is deliberately uncovered and why.
- [x] **C3** ✔ — `evals/generate_golden.py`: actions × states × days × final-sale
      collapsed from a 162-cell grid to **34 cases** with `allpairspy`, plus 7
      boundary rows added back by hand because a sampling strategy is exactly
      what misses day 30 against day 31.
- [x] **C4** ✔ — four verdicts, not two: passed, failed, `scored`, and
      `not_exercised`. Scored and binary are never summed together.

**Gate held.** Every run ends with that report. **38/43 exercised, 0 failed,
5 not exercised** — each of the five documented in `evals/NOT_EXERCISED.md` with
why and what would change it.

---

## Phase D — AgentTwin, Tier 0

- [x] **D1 · the projection** ✔ — `agenttwin/projection.py`. Every tool is
      generated from the declaration: schema from the entity, metadata from the
      action, behaviour from `allowed_when` and `sets`. No per-tool code, which
      is the whole claim. **The premise held**: the same 34 golden cases pass
      against the projected server and the hand-written one.
- [x] **D2** ✔ — `worlds/clothing.yaml`. Two entities, five actions, five rows,
      a declared ontology and a fidelity block naming what it is **not** faithful
      about. Eligibility is data, so "return on day 31" is a case you write.
- [~] **D3** — rows are declared in the world file and validated at load
      (enums, dangling foreign keys). Faker generation for volume is not built;
      nothing yet needs more than five orders.
- [x] **D4** ✔ — `Live.snapshot()` and `record.diff()`. The gate asserts on the
      diff, not on the reply.
- [x] **D5** ✔ — `RunRecord`: scenario, world, seed, resolution, config
      fingerprint, **determinism class**, world diff, effects, verdicts and the
      AAC ids discharged.

**Gate held.** *Cancel an order already `shipped`* → the agent refused rather
than failed, and the world diff is empty. The control case on a `pending` order
shows exactly one change, because a gate that passes because nothing ever
happens is not a gate.

---

## Phase E — break it

- [x] **E1** ✔ — Hypothesis over arbitrary order ids, arbitrary tool output through the fence, arbitrary router input. Properties stated over *all* text rather than the delimiters someone thought of.
- [x] **E2** ✔ — `agenttwin/perturbation.py`: stale read, both MCP error channels, latency. Faults fire on a *named* call rather than randomly, and a fault that never fired is reported — a scenario whose fault did not land passes for the wrong reason.
- [~] **E3** — the determinism class is declared on `RunRecord` and defaults to `scripted`. Model-driven actors are not built; nothing yet needs one, and adding one trades replay for realism.
- [x] **E4** ✔ — a hostile instruction seeded into an order note in the world file. Three layers hold independently: it arrives fenced, the tool it demands is not on the identity's surface, and the eligibility rule refuses it anyway.
- [x] **E5** ✔ — `evals/FINDINGS.md`. **Four findings**, recorded before being fixed. F-004 is the fix for F-001 introducing a worse defect, caught minutes later by the Phase D gate.

---

## Phase F — back into the catalogs

Deliberately last. Authoring loop obligations from a desk is what produced six
cost obligations and one trajectory obligation.

- [ ] **F1 · G1** — add A6 to AAC-0046, AAC-0047, AAC-0076 and AHC-0074. A tag
      edit, not new authorship. **The evidence now generates itself:** the
      conformance report has an "exercised but not tagged A6" section, and
      AAC-0046 and AAC-0047 are in it with passing tests behind them.
- [ ] **F2 · G2** — oscillation below the termination threshold. *Already
      implemented and tested here* — `TerminationReason.OSCILLATION_DETECTED` —
      so the obligation now has evidence behind it.
- [ ] **F3 · G3, G4** — a grounding obligation for A6; escalation correctness.
- [ ] **F4 · G5** — thicken S6, the stage the fix-and-regress loop runs on.
- [ ] **F5** — close AHC-0074's two open tensions, which this build answers:
      *what if the downstream has no idempotency support*, and *is a model call
      itself idempotent*.
- [ ] **F6** — reverse-engineer the world **schema** from what the projection
      actually needed.

---

## Standing

- [ ] Go public when it runs, not before. An empty public repo signals abandonment.
- [ ] Verify before publishing: the benchmark-landscape claim, SDV's licence,
      Langfuse's footprint. All are model knowledge with a May 2026 cutoff.

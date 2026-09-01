# Build sequence

Ordered. Each phase has a gate — a thing that is true afterwards and was not
true before. Nothing moves on until its gate holds.

The import contract in `pyproject.toml` is the module checklist: a module in
`(parentheses)` is declared but unbuilt, and the parentheses come off as it
lands. When none remain, Phase A is done.

---

## Phase A — finish the harness · 6 of 16 modules left

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

- [ ] **A5 · `flow`** (L8) — fan-out limits, backpressure, throttling.
      *Concretely:* the loop runs parallel `tool_calls` sequentially today, and
      Groq's free-tier limits make a 429 storm realistic with no coordinated
      response.

- [ ] **A6 · `resilience`** (L10) — retries, breaker, fallback, **compensation**.
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

- [ ] **B2** — Postgres, natively (`brew install postgresql@16`, not Docker —
      this machine has ~170 MB free). Two schemas: `agent_state` and `ecom`.
      Durable `CheckpointStore` and `IdempotencyLedger` replace the in-memory
      ones. *Gate:* an approval survives a process restart.

---

## Phase C — evals

- [ ] **C1** — render the 43 A6 obligations as executable checks, split by their
      `stages:` field. Offline/online is a rendering, not a new taxonomy.
- [ ] **C2** — the seven case families from doc 25.
- [ ] **C3** — case generation: paths through the order state machine, collapsed
      with `allpairspy`.
- [ ] **C4** — enforce the verdict split: binary for deterministic cases, scored
      pass-rate for model-driven ones, **never mixed in one report**.

**Gate:** a conformance report that names which AAC ids passed, failed, and were
not exercised.

---

## Phase D — AgentTwin, Tier 0

- [ ] **D1 · the projection** — world state → MCP tool responses. **Build this
      first.** It is the one component nobody has written, and where the
      single-world premise either holds or breaks.
- [ ] **D2** — a `world.yaml`: five entities, one MCP server, a seed. A draft,
      not a schema.
- [ ] **D3** — seed from world state (Faker for the cold start).
- [ ] **D4** — snapshot `world₀` / `world₁` and diff. The strongest oracle there
      is, and one no transcript-grading eval can produce.
- [ ] **D5** — the run record: seed, config hash, verdicts tagged with AAC ids,
      trace, cassette.

**Gate:** *cancel an order already `shipped`* → the diff shows zero cancellation
rows, and the agent refused rather than failed.

---

## Phase E — break it

- [ ] **E1** — Hypothesis for degenerate input and argument fuzzing, with shrinking.
- [ ] **E2** — perturbations: handle expiry, both MCP error channels, a stale read
      between the eligibility check and the write.
- [ ] **E3** — actors with declared determinism classes.
- [ ] **E4** — adversarial: confused deputy, injection through order notes and
      review text, social engineering.
- [ ] **E5** — **publish the failures.** The deliverable is a report with its
      failures in it.

---

## Phase F — back into the catalogs

Deliberately last. Authoring loop obligations from a desk is what produced six
cost obligations and one trajectory obligation.

- [ ] **F1 · G1** — add A6 to AAC-0046, AAC-0047, AAC-0076 and AHC-0074. A tag
      edit, not new authorship.
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

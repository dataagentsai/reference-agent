# Who owns the record of a conversation — a design

*Written 2026-09-25. **Parked on the day it was written**, deliberately, and the
reason is part of the design note rather than a footnote to it. Carries the
handback gap, the per-conversation hold, and the question underneath both.*

> **Status: parked, not queued.** Nothing here is scheduled and nothing should be
> built from it yet. It was found by reading this implementation and asking good
> questions about it, which is a way of finding real gaps that has **no stopping
> rule** — there is always another one. The stopping rule for spec work is
> T-034: generate an agent from the specs and diff it against this one. Until
> that has run once, a gap found by inspection is a *guess* about what the specs
> failed to say, and this document is written so the guess can be put down
> rather than carried.
>
> Read it again when T-034 has produced its first diff. If the generated agent
> has this gap too, it is a spec gap and belongs in AHC. If it does not, the
> specs already say enough and this was an implementation defect all along.

---

## The complaint, in the words it was made in

> *I have a robot and humans handling customers and they need to be in sync. One
> cannot say "it is with that colleague so I do not know". Why is a source of
> truth not maintained about the whole conversation in one place, not belonging
> to the agent or the human but to the order — and the customer talking about
> that order has to be available to every agent and human who is asked about it.*

The test it implies is one no design argument survives: **a customer asks a
question, and the answer depends on which of two parties they happen to be
talking to.** Everything below is about why that happens here.

---

## What actually happens today

A customer asks for a person. An escalation `E-AAF59125` is raised, a private
note is posted for the colleague, and the Chatwoot conversation is handed off.
The colleague — call her Priya — talks to the customer for twenty minutes and
agrees a goodwill voucher. She resolves the escalation. The customer's next
message comes back to the agent.

**The agent resumes from exactly the state that existed before the handover.**
Not approximately. Exactly. Three independent mechanisms each remove one part of
what happened, and they are in three different files:

| What is lost | Where it is lost | The line |
|---|---|---|
| Priya's messages | `channel/__init__.py` | `if payload.get("message_type") != "incoming" or payload.get("private"): return None` |
| The customer's messages during the hold | `entrypoint/__init__.py` | `_gates` returns before `with_messages(ctx.user_message(text))` is reached |
| Priya's own summary of what she did | `contracts/human.py` | `outcome_note` is written by the desk and **read by nothing** |

That third row is the sharpest piece of evidence in this document, because it is
not a design tension. It is a field that exists, is populated on every
resolution, and has exactly one reference in the codebase — the write.

### The blindness has a precise shape

It is narrower than it first sounds, and the narrowness matters:

| The agent is | Because |
|---|---|
| **correct about the world** | anything Priya *did* — refunded, cancelled, reshipped — is in the order system, which the agent re-reads |
| **blind to the conversation** | anything Priya *said* — promised, explained, apologised for — exists only as channel text nobody ingests |

So *"where is my order AB-10003"* is answered correctly after the handover.
*"so when is my voucher coming"* is met with nothing.

### And the hold is keyed to the wrong thing

`open_for(conversation_id)` — by **conversation, not by customer**. A customer
whose chat is with Priya can open a second conversation, or write in by email,
and the agent answers normally, correctly on the facts, with no idea a colleague
is mid-negotiation with the same person about the same order.

---

## The fact that reframes the problem

**The complete record already exists.** The Chatwoot thread holds every message
in order: the customer's, the agent's, Priya's, and the private note. Nothing
was lost by anyone.

The agent cannot see it because the channel API is **write-only**:

```python
async def reply(self, account, conversation, text) -> None: ...
async def note(self, account, conversation, text) -> None: ...
async def hand_off(self, account, conversation) -> None: ...
async def open_conversation(...) -> ...
```

Four ways to write. Zero ways to read. The agent posts into a thread it cannot
open.

For a single channel this is closer to a missing method than a missing
architecture — which is worth knowing before anybody designs a new store.

---

## Why a read method is not the answer either

Three reasons the complaint survives the cheap fix:

**A thread belongs to the channel, not to the subject.** Add email and WhatsApp
and there are three threads about one order, none of which can see the others.
Reading Chatwoot back moves the blindness rather than removing it.

**A thread is prose.** *"I've sorted a ₹500 voucher for you"* is a sentence, not
a fact. Acting on it means re-deriving facts from text — which is the
summarisation this codebase refuses everywhere else, including in the handoff
note that started this whole path.

**It is keyed by thread.** The cross-conversation case stays broken.

---

## The shape the codebase already half has

`Facts` and `as_handoff()` are the right primitive, and they are already used
for exactly this at the moment of handover:

```
They asked: where is my order AB-10003
About: order AB-10003
Nothing done yet.
```

Assembled from structured fields, never paraphrased. It already carries the
subject — `About: order AB-10003`. Three things are missing, and none is exotic:

| Today | What the complaint asks for |
|---|---|
| keyed by conversation | keyed by **customer + subject** |
| written by the agent only | written by **whoever acts** — agent or person |
| no author on an entry | **provenance**: who, and when |

Provenance is what makes it safe. *"Priya, 14:32, agreed goodwill voucher ₹500,
not yet issued"* is a fact with an author. The agent can state it without
inventing anything, and can say plainly that it is not yet done.

---

## Three options, honestly costed

### 1 · Read the thread back

Add a read method to the channel port; ingest the messages the agent missed.

**For:** small, uses a record that already exists, fixes the single-channel case
completely.
**Against:** the agent now reasons from prose it did not write; still keyed by
thread; a second channel reopens the whole gap.
**Verdict:** the right thing to do *if* the deployment has one channel, and a
trap if anybody assumes it generalises.

### 2 · `Facts` becomes subject-keyed and shared

A record keyed by customer and subject, written by the agent and by whoever
serves the customer, with an author on every entry. Both parties read it.

**For:** answers the complaint as stated. Survives more than one channel.
Structured, so nothing is re-derived from prose. Extends a primitive that exists
and is already trusted at the handover.
**Against, and none of these are small:**

- **The oracle.** `agent_state` is deliberately what tests assert on and what
  AgentTwin never projects. A store humans write to is no longer a clean oracle.
  This is a real loss and it is a *testing* concern, which does not outrank a
  customer being told "I don't know".
- **Somebody has to run it.** See option 3 for why it is not the order system.
- **Erasure.** F-056 took a day across three stores. This adds a fourth, written
  by two parties, and the erasure path has to reach it.
- **Two writers.** The agent and a person updating one record is the
  optimistic-concurrency problem of T-060, now inside the system rather than at
  the far end. It needs the same answer and cannot borrow the far end's.

### 3 · The far end owns it — *rejected*

Put conversations in the order system, so the record lives with the order.

**Rejected**, for a reason that is not a preference: an order system does not
own conversations, Saleor has no concept of one, and it is not ours to change.
Asking a far end to grow a domain it does not have is how a binding stops being
a binding.

---

## The question only the owner can answer

Everything downstream turns on one decision, and it is not a technical one:

> **Is a person a *writer* to this record, or does the record only *observe*
> them?**

**A writer** means Priya has somewhere to put *"agreed a ₹500 voucher"* — a UI,
a form, a plugin, a discipline she has to keep. The record is complete and the
cost is a human process that can be skipped on a busy day, which makes the
record's completeness a claim rather than a guarantee.

**An observer** means nothing is asked of Priya and the record is assembled from
what the system can see her do — a refund issued, a ticket resolved, an outcome
code chosen. Nothing can be skipped, and *"I promised them a voucher"* is
invisible because no system event corresponds to a promise.

The first is complete and unreliable. The second is reliable and incomplete.
There is no third answer, and picking one decides the UI, the schema, the
erasure path and what the agent may say.

---

## What would make this un-park itself

Any one of these:

1. **T-034's first diff contains it.** A generated agent with the same blindness
   means the specs never asked for the record to be shared, and this becomes an
   AHC capability rather than a defect in one implementation.
2. **A second channel ships.** The moment email or WhatsApp is live, option 1
   stops being sufficient and the cost of option 2 stops being hypothetical.
3. **A customer hits it in front of somebody.** The failure is legible to a
   non-technical observer in one sentence, which makes it the kind of thing that
   gets prioritised by being seen rather than by being argued.

Until one of those, the `outcome_note` write with no reader is logged as its own
finding, because it is small, provable, and true regardless of how this design
question is answered.

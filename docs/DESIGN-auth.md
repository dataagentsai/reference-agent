# Identity and permission — a design

*Written 2026-09-05, before any of it is built. Carries the fix for
[F-016](../evals/FINDINGS.md), which is critical: today any customer can act on
any order.*

> **Decided 2026-09-16 (the user), for T-002.** Steps 1 and 2 below are built:
> the ownership rule and its two-customer test. For steps 3 and 4:
>
> 1. **`customer_id` is its own claim**, from a Keycloak user attribute only an
>    admin can set. `sub` stays the login's identity for the audit trail; the
>    store's customer id is written to the attribute when the account is linked.
> 2. **Tests sign with a local issuer.** The agent knows only RS256 and a JWKS;
>    HS256 leaves `src`. Tests sign with an in-process key pair, and a separate
>    set runs against the composed Keycloak and skips when it is down.
> 3. **The far end verifies, by token exchange (RFC 8693).** The agent exchanges
>    the customer's token for one addressed to the order system (`sub` the
>    customer, `azp` the agent) and sends it with every call. The order system
>    stops trusting an asserted `customer_id`. Open inside this: how the far end
>    verifies the **refund elevation**, which the agent grants itself today and
>    Keycloak knows nothing about.
> 4. **No login page in cycle 1.** The realm, clients, roles and test users
>    exist; scripts and tests take tokens from the token endpoint. The customer's
>    login arrives with the channel (T-026), since Chatwoot replaces the page.

The reason this needs designing rather than picking a library is that **three
different questions get called "auth", they are enforced in three different
places, and the one we got wrong is the one a library does not answer for you.**

| | The question | Where it is answered | Today |
|---|---|---|---|
| **Authentication** | Who are you? | the edge, before anything else | a script signs a token |
| **Coarse authorization** | What *kind* of thing may you do? | the tool list | scopes on the token |
| **Fine authorization** | May you do it to **this**? | the tool call | **nothing** ← F-016 |

The third row is the defect. It is also the row no identity provider fills in,
which is why buying one would have left it exactly where it is.

---

## 1 · Authentication — who are you

### What is wrong now

`ident.mint()` signs a token in a script. There is no user record, no
credential, no login, no revocation, no administration. And the algorithm is
`HS256` — **one shared secret both signs and verifies**, so anything able to
check a token is able to forge one. The agent process holds that secret, so
reading its environment yields the power to become any customer.

### What it should be

**Asymmetric signing.** The issuer signs with a private key it never shares; the
agent verifies with a public key it fetches from the issuer's JWKS endpoint and
caches. A compromised agent can then read tokens and cannot write them. This is
the single highest-value change in this document and it is nearly free, because
every provider below does it by default.

**Short lifetimes rather than a revocation list.** A token good for fifteen
minutes with a refresh token behind it is simpler and more reliable than
distributed revocation. Revocation then means refusing the *refresh*, which is a
single decision at the issuer rather than a cache invalidation everywhere.

**Claims we actually need**, and no more:

```
sub   the customer id            — who
scp   coarse permissions         — what kind of thing
iss   which issuer               — so a token from elsewhere is refused
aud   this agent                 — so a token for another service is refused
exp   short                      — minutes, not hours
jti   unique id                  — so one token's use can be traced in the record
```

`aud` and `jti` are both absent today. `aud` matters the moment a second service
shares the issuer: without it, a token minted for the marketplace API is accepted
by the support agent. `jti` matters for the audit trail — R-017's record can say
*which run* but not *which session*.

### The agent needs its own identity too

Not designed anywhere yet. The agent authenticates *to* the shop, and right now
it does so as nobody in particular. A service identity — separate from the
customer's, with its own credential and its own scopes — is what makes the shop's
audit log able to say *"the support agent, acting for C-1042"* rather than just
*"C-1042"*. That distinction is the whole of AAC's identity-propagation
obligation and we currently satisfy it by accident, because there is only one
caller.

---

## 2 · Coarse authorization — what kind of thing may you do

### What is right now, and worth keeping

This part is genuinely well built and should not be disturbed.

The tool list is **filtered by scope**, so `issue_refund` is not refused for a
customer — it is **absent**. The model cannot ask for a tool it has never been
shown. That is the strongest kind of guardrail there is, and it is stronger than
any permission check because there is no check to get wrong.

`granted_identity` is the other half and it is a **just-in-time privilege**
pattern: `refunds:write` exists nowhere except inside an identity minted from a
live, granted, unexpired approval. It cannot be held, only borrowed, and only
for one action.

### What should change

Scopes should come **from the issuer**, per user, rather than from
`CUSTOMER_SCOPES` as a constant in the source. Today every customer has
identical permissions by construction, so "this customer is restricted" is
unsayable.

---

## 3 · Fine authorization — may you do it to *this* one

**This is F-016 and it is the part that matters.**

### The two sentences

```
orders:write   says     this caller may write ORDERS
what we need   says     this caller may write THEIR OWN orders
```

The first is a permission about a **verb**. The second is about a **row**. A
scope list cannot express the second, and no amount of better scopes will make
it able to.

### Where it belongs

At the **tool boundary**, and only there.

That position already holds both halves — the caller's identity and the row the
action is about to change — and it is the last place an action can be stopped
while it is still cheap. Anywhere earlier does not yet know which row; anywhere
later has already changed it.

### How the rule should be expressed

**The world should declare it, not the agent.** The relationship is already
written down —

```yaml
order:
  fields:
    customer_id: {type: id, ref: customer.id}
```

— and nothing reads it when an action runs. That is the same mistake as R-012,
where the ontology was decorative until the generator was made to consume it.

So the world gains one statement per entity, naming the field that carries
ownership, and the tool boundary enforces it generically:

```yaml
order:
  owned_by: customer_id      # ← the whole feature
```

Generic rather than hard-coded, because a world where orders belong to an
*account* rather than a customer should not need a code change — the same
principle that lets a fourteen-day return window be a file edit.

### What must not happen

**Do not put a permissions service in front of this.** OpenFGA, SpiceDB, Cedar
and OPA are all good, and using one to answer *"is this row yours"* would put a
network call in front of a field comparison. They earn their place when the rule
stops being *"it is yours"*:

- a household account where several logins share orders
- support staff acting **on behalf of** a customer
- a marketplace partner seeing only their own orders
- an agent acting for a partner acting for a customer

None of those exist yet. The design should make adding them cheap, not pay for
them now.

---

## 4 · The actors nobody has modelled

The system knows about one kind of person: a customer. A real deployment has at
least four, and each has a different permission shape.

| Actor | What is different about them | Exists? |
|---|---|---|
| **Customer** | acts only on their own rows | yes |
| **Support staff** | acts **on behalf of** a customer, and the record must say both | no |
| **Approver** | elevated, time-boxed, and may not approve their own | half — the rule exists, the identity does not |
| **The agent itself** | a service identity, so the shop's log can name it | no |

**Support staff is the interesting one**, because it is the case that breaks a
naive ownership check. A staff member acting for C-1042 does not *own* the order,
and must still be allowed. That is **delegation**, and it is the point at which a
relationship service starts to pay for itself — the rule becomes *"you may act on
an order if you own it, or if you hold an active delegation from someone who
does"*, and that is a graph, not a field.

Designing for it now costs nothing if the check is expressed as a declared rule
rather than an `if` statement.

---

## 5 · Open source, mapped to the three questions

### Running a login and issuing tokens

| | What it is | When to pick it |
|---|---|---|
| **Keycloak** | The incumbent. Java, its own database, an admin UI that already does everything. | The boring institutional answer. Nobody is criticised for it. Heavyweight. |
| **Zitadel** | Go, multi-tenant from the ground up, modern. | Building SaaS where tenants matter from day one. |
| **Authentik** | Python/Django, friendlier admin than Keycloak. | Smaller team, want a UI, do not want Java. |
| **Ory Hydra + Kratos** | API-first, composable, no UI. Hydra issues tokens, Kratos holds identities. | You want parts rather than a product and will assemble them. |
| **Logto**, **SuperTokens** | Lighter, developer-first, smaller feature surface. | Getting something real running this week. |

All five give asymmetric signing and a JWKS endpoint, so §1's main problem is
solved as a side effect of choosing any of them.

**Recommendation: Keycloak** for this reference implementation. Not because it is
the best but because it is the most *legible* — a reader who wants to know what
we are claiming can check it against something they already know, which is worth
more here than elegance.

### Fine-grained authorization, later

| | What it is | When |
|---|---|---|
| **OpenFGA** | CNCF, Zanzibar model, relationship tuples. | Delegation arrives. |
| **SpiceDB** | Same lineage, richer schema, more mature. | Same, if the schema gets complicated. |
| **Cedar** | AWS, open sourced, purpose-built for authorization and readable. | Rules become policy that non-engineers review. |
| **OPA** | General policy engine, Rego. | You already run it for other things. |
| **Casbin** | In-process, no service. | You want a policy model without a network hop. |

**Recommendation: none of them yet.** F-016's fix is a declared field and a
comparison. Revisit at the first delegation requirement, and expect it to be
Cedar or OpenFGA.

---

## 6 · What AgentTwin needs, and why this is not optional

F-016 survived because **every test, scenario and generated case uses one
customer.** `C-1042` is in the fixtures, the world seeds one customer, and the
simulated customer is always that one.

> A fault that takes two customers to observe cannot be observed by a suite that
> has never had two.

The simulator therefore needs, before the fix is credible:

- **A second seeded customer** in both worlds, owning some of the orders.
- **A hostile actor** — one who presents a valid identity and asks about
  somebody else's order. Not a new actor *type*: `ScriptedActor` with a
  different identity is enough, and the fact that this was never done is the
  finding.
- **A staff actor**, once delegation exists, to prove the ownership rule does not
  block legitimate on-behalf-of access — the failure mode F-004 warned about,
  where a guardrail that blocks correct behaviour is worse than one that misses.
- **An ownership predicate** in the oracle set: *no row changed that the caller
  did not own*. That is a fifth oracle question and it is not any of the four we
  have.

---

## Order of work

1. **The ownership check.** `owned_by` in the world, enforced at the tool
   boundary. Closes F-016. No dependency on anything below.
2. **A second customer and a hostile actor** in AgentTwin, plus the ownership
   predicate. Proves it.
3. **An identity provider**, verify against JWKS, drop the shared secret. Closes
   §1.
4. **A service identity for the agent**, so the shop's log can name the caller
   and the principal separately.
5. **Delegation**, and only then a relationship service.

Steps 1 and 2 are days. Steps 3 onward are a deployment decision and can wait —
but step 1 cannot, because the defect is live.

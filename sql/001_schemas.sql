-- Two schemas, and the split is load-bearing.
--
-- `agent_state` belongs to the agent: conversation, checkpoints, escalations, the
-- idempotency ledger. It is the **oracle** — what assertions read — and
-- AgentTwin never projects it. An oracle that can be faked is not one.
--
-- `ecom` is the business world: customers, orders, shipments, refunds. This is
-- what a simulation replaces, and in a simulated run the agent reaches it only
-- through a projection server that never touches this schema at all.
--
-- Separate schemas rather than separate tables in one, so the boundary can be
-- enforced by a grant rather than by remembering. A role that can read `ecom`
-- and write `agent_state` cannot accidentally do the reverse.

CREATE SCHEMA IF NOT EXISTS agent_state;
CREATE SCHEMA IF NOT EXISTS ecom;

-- --------------------------------------------------------------------------
-- agent_state — never simulated
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_state.checkpoints (
    run_id          text PRIMARY KEY,
    -- F-006. A run id is minted fresh every turn and no customer ever holds
    -- one, so a table keyed only by run could be written but never read back by
    -- anything outside the process that wrote it. The conversation id is the
    -- only handle a caller actually has.
    conversation_id text,
    state           bytea       NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Reading a conversation always asks for its newest turn, so the index carries
-- the sort as well as the filter — otherwise every lookup reads every turn the
-- conversation ever had and throws all but one away.
CREATE INDEX IF NOT EXISTS checkpoints_conversation
    ON agent_state.checkpoints (conversation_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_state.idempotency (
    key         text PRIMARY KEY,
    result      jsonb       NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
-- PRIMARY KEY is the whole mechanism. The ledger is correct because the
-- database refuses a second row for the same key, not because the application
-- remembered to check first — two processes racing on a retry both lose.

-- Approvals lived here until T-028. They are Temporal workflows now: the wait,
-- the decision and the record are the workflow's history, and nothing the agent
-- holds can write them.
DROP TABLE IF EXISTS agent_state.approvals;

-- An escalation is a conversation changing hands, and this row is the only thing
-- that makes that real. Without it the agent said "let me pass you to a
-- colleague" and wrote nothing down — so no colleague could find it, nothing
-- could count it, and the next turn behaved as though it had never happened.
CREATE TABLE IF NOT EXISTS agent_state.escalations (
    id              text PRIMARY KEY,          -- E-XXXXXXXX, said out loud to the customer
    conversation_id text        NOT NULL,
    run_id          text        NOT NULL,      -- the turn that decided, so the trace is findable
    customer_id     text        NOT NULL,

    -- Why it fired, reproducibly. `rule_id` rather than only `reason`: prose
    -- cannot be grouped, and "which rule produces escalations the human said
    -- were unnecessary" is the question that tunes the rule set.
    tier            smallint    NOT NULL DEFAULT 1,
    rule_id         text        NOT NULL,
    rules_version   text        NOT NULL,      -- AAC-0101 gates changing these
    reason          text        NOT NULL,

    state           text        NOT NULL DEFAULT 'queued',
    created_at      bigint      NOT NULL,
    expires_at      bigint      NOT NULL,      -- nobody came; hand the conversation back

    -- The ground truth that makes over- and under-escalation measurable rather
    -- than arguable. Written by whoever closes the ticket; nothing else can
    -- supply it. Present from the first version so the data exists when the
    -- analysis is built.
    resolved_at     bigint,
    outcome         text,
    outcome_by      text,
    outcome_note    text,

    CONSTRAINT escalations_state_known CHECK (state IN ('queued','resolved','expired')),
    CONSTRAINT escalations_outcome_known CHECK (outcome IS NULL OR outcome IN (
        'resolved','agent_could_have','misrouted','customer_gone'
    ))
);

-- A customer's login, kept so a chat channel can act for them (T-026). The
-- refresh token is Fernet ciphertext; the key lives with the process, never
-- here. Deleting the row is logout reaching the agent.
CREATE TABLE IF NOT EXISTS agent_state.sessions (
    subject         text PRIMARY KEY,          -- the login's `sub`, not the customer
    refresh_token   bytea       NOT NULL,
    updated_at      bigint      NOT NULL
);

-- The hot path: every turn of an escalated conversation asks "is one open?".
-- Partial, because a resolved escalation is never read this way.
CREATE INDEX IF NOT EXISTS escalations_open
    ON agent_state.escalations (conversation_id, created_at DESC) WHERE state = 'queued';

-- The queue a reviewer will see — step 4. Indexed now because the column order
-- is the part that is awkward to change once rows exist.
CREATE INDEX IF NOT EXISTS escalations_queue
    ON agent_state.escalations (state, created_at);

-- --------------------------------------------------------------------------
-- ecom — the world AgentTwin projects
--
-- The DDL here is the ontology: `orders.customer_id REFERENCES customers(id)`
-- is the join, stated once and machine-readable, rather than written a second
-- time in a world file. Phase D reads this rather than duplicating it.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ecom.customers (
    id         text PRIMARY KEY,
    email      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ecom.orders (
    id          text PRIMARY KEY,
    customer_id text NOT NULL REFERENCES ecom.customers(id),
    status      text NOT NULL,
    total       numeric(12,2) NOT NULL,
    placed_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT orders_status_known CHECK (status IN (
        'pending','confirmed','picked','shipped','out_for_delivery',
        'delivered','cancelled','returned','refunded'
    ))
);

CREATE TABLE IF NOT EXISTS ecom.shipments (
    id           text PRIMARY KEY,
    order_id     text NOT NULL REFERENCES ecom.orders(id),
    carrier      text NOT NULL,
    delivered_at timestamptz
);

CREATE TABLE IF NOT EXISTS ecom.refunds (
    id              text PRIMARY KEY,
    order_id        text NOT NULL REFERENCES ecom.orders(id),
    amount          numeric(12,2) NOT NULL,
    idempotency_key text UNIQUE,
    issued_at       timestamptz NOT NULL DEFAULT now()
);
-- `idempotency_key UNIQUE` is the second half of the double-refund control, and
-- the half that survives us being wrong. The ledger prevents the second call;
-- this makes the second row impossible even if it is somehow made.

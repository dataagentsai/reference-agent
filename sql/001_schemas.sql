-- Two schemas, and the split is load-bearing.
--
-- `agent_state` belongs to the agent: conversation, checkpoints, approvals, the
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

CREATE TABLE IF NOT EXISTS agent_state.approvals (
    id              text PRIMARY KEY,
    action          text        NOT NULL,
    args            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    reason          text        NOT NULL,
    customer_id     text        NOT NULL,
    idempotency_key text        NOT NULL,
    created_at      bigint      NOT NULL,
    expires_at      bigint      NOT NULL,
    decided         boolean     NOT NULL DEFAULT false,
    granted         boolean     NOT NULL DEFAULT false,
    decided_by      text
);

CREATE INDEX IF NOT EXISTS approvals_pending
    ON agent_state.approvals (decided) WHERE NOT decided;

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

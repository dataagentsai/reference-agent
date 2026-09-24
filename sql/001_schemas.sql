-- One schema here, and the split it used to hold is now a database boundary.
--
-- `agent_state` belongs to the agent: conversation, checkpoints, sessions, the
-- idempotency ledger. It is the **oracle** — what assertions read — and
-- AgentTwin never projects it. An oracle that can be faked is not one.
--
-- `ecom` was the business world in this database until T-017. The store is
-- Saleor now, with its own schema in its own database and its own role, so the
-- boundary is a grant the agent's role does not have rather than one this file
-- argues for. The agent reaches either store only over MCP.

CREATE SCHEMA IF NOT EXISTS agent_state;

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

-- One name, one outcome — T-062. This replaced two stores that answered the
-- same question with opposite rules: a delivery claim that settled on failure,
-- and an idempotency ledger that did not record one. `scope` is what used to be
-- the difference between them, and it is a column rather than a table because
-- the rule does not change between a message and a tool call.
CREATE TABLE IF NOT EXISTS agent_state.requests (
    name        text PRIMARY KEY,
    scope       text        NOT NULL,
    state       text        NOT NULL,     -- in_flight | answered
    outcome     jsonb,                    -- the answer, and only when definite
    expires_at  timestamptz,              -- while held; NULL once answered
    recorded_at timestamptz NOT NULL DEFAULT now()
);
-- PRIMARY KEY is the whole mechanism. The store is correct because the database
-- refuses a second row for the same name, not because the application
-- remembered to check first — two processes racing on a retry both lose.
--
-- `expires_at` is why this needs no sweeper. `finally` does not run for a
-- killed process, so a claim can be left held for ever; it is reclaimed by the
-- next caller finding it stale, in the same statement that takes it.

DROP TABLE IF EXISTS agent_state.idempotency;

-- Approvals lived here until T-028. They are Temporal workflows now: the wait,
-- the decision and the record are the workflow's history, and nothing the agent
-- holds can write them.
DROP TABLE IF EXISTS agent_state.approvals;

-- Escalations lived here until T-028, and are Temporal workflows now: the wait,
-- the lapse timer and the outcome are the workflow's history. What made the row
-- worth having — that the agent could not say "let me pass you to a colleague"
-- with nothing written down — is unchanged; the writing down moved.
DROP TABLE IF EXISTS agent_state.escalations;

CREATE TABLE IF NOT EXISTS agent_state.sessions (
    subject         text PRIMARY KEY,          -- the login's `sub`, not the customer
    refresh_token   bytea       NOT NULL,
    updated_at      bigint      NOT NULL
);

-- --------------------------------------------------------------------------
-- `ecom` held a store's ontology — customers, orders, shipments, refunds — and
-- nothing ever read it. With T-017 the store is Saleor, which owns its own
-- schema in its own database, and the world AgentTwin projects is a YAML file.
-- A DDL nobody reads is a claim nobody checks, so it is dropped rather than
-- kept as decoration.
-- --------------------------------------------------------------------------

DROP SCHEMA IF EXISTS ecom CASCADE;

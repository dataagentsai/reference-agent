-- Our own approval and escalation records (claims-fnol-azure A3), beside the
-- wait that owns each one. The wait's engine (Temporal's history, DBOS's tables)
-- keeps its own state in its own format; these rows are ours, so a far end
-- checking a grant reads them and never the engine's.
--
-- Written by `agent_harness.state.records.PostgresRecords`, an upsert keyed by
-- the wait's id. Status keeps the code's names (`contracts/records.py` maps
-- them to the design's: waiting = pending, carrying_out/done = approved, ...).

CREATE SCHEMA IF NOT EXISTS agent_state;

CREATE TABLE IF NOT EXISTS agent_state.approvals (
    id              text PRIMARY KEY,          -- the wait's workflow id
    action          text        NOT NULL,
    args            jsonb       NOT NULL,      -- every value as text
    args_digest     text        NOT NULL,      -- sha256: action, args, whose, key
    requested_for   text        NOT NULL,
    conversation_id text        NOT NULL DEFAULT '',
    idempotency_key text        NOT NULL,
    decided_by      text,
    expires_at      timestamptz NOT NULL,
    status          text        NOT NULL CHECK (status IN (
                        'assessing', 'waiting', 'carrying_out', 'done',
                        'failed', 'refused', 'expired', 'stale')),
    reason          text        NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
-- Erasure finds a person's rows by whose they are (F-056).
CREATE INDEX IF NOT EXISTS approvals_requested_for ON agent_state.approvals (requested_for);

CREATE TABLE IF NOT EXISTS agent_state.escalations (
    id              text PRIMARY KEY,          -- the wait's workflow id
    conversation_id text        NOT NULL,
    requested_for   text        NOT NULL,
    question        text        NOT NULL,
    found           text        NOT NULL DEFAULT '',
    missing         text        NOT NULL DEFAULT '',
    assignee        text,
    sla_due_at      timestamptz NOT NULL,
    status          text        NOT NULL CHECK (status IN ('queued', 'resolved', 'expired')),
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS escalations_requested_for
    ON agent_state.escalations (requested_for);

#!/bin/sh
# One Postgres for the whole composed stack, and one role and database per
# service inside it. Runs once, on an empty volume, as the image's superuser.
#
# One instance rather than one per service because this has to fit on a laptop:
# Langfuse, Keycloak, LiteLLM and Chatwoot each ship a compose file with its own
# Postgres, and four of them is memory spent on nothing. One role per service
# rather than the superuser everywhere, so a service that misbehaves can damage
# its own database and no other.
#
# The agent is the case the split exists for. Its schemas are created *as* the
# `agent` role, never as the superuser, so the grants that sql/001_schemas.sql
# argues for are the ones a composed run actually has.
set -eu

pw="${STACK_DB_PASSWORD:-local-dev-only}"

for svc in agent keycloak litellm langfuse chatwoot saleor; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres \
    -c "CREATE ROLE $svc LOGIN PASSWORD '$pw'"
done

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres <<'SQL'
CREATE DATABASE support_agent OWNER agent;
CREATE DATABASE keycloak      OWNER keycloak;
CREATE DATABASE litellm       OWNER litellm;
CREATE DATABASE langfuse      OWNER langfuse;
CREATE DATABASE chatwoot      OWNER chatwoot;
CREATE DATABASE saleor        OWNER saleor;
SQL

# Saleor's migrations create these, and only a superuser may. Created here so
# the role stays unprivileged, exactly as Chatwoot's are below.
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d saleor <<'SQL'
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS hstore;
SQL

# Chatwoot's schema enables extensions that only a superuser may create
# (pg_stat_statements is not a trusted extension). Created here, its own
# `enable_extension` calls become no-ops and the role stays unprivileged.
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d chatwoot <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
SQL

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d support_agent \
  -c "SET ROLE agent" -f /agent-sql/001_schemas.sql

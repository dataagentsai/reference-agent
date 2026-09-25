#!/usr/bin/env bash
# Runs once when the Codespace is created. Installs the Python side only:
# nothing is started, because which compose profiles fit is a choice.
set -euo pipefail

pip install --user --quiet uv
export PATH="$HOME/.local/bin:$PATH"
uv sync --extra dev

# No .env on purpose. A fresh clone runs with none (the local issuer, the
# scripted model); copying .env.example points the agent at Keycloak, which is
# only right once `docker compose up -d` is running.
cat <<'EOF'

Ready. Pick a level:

  1  uv run python scripts/run_server.py                  # no Docker, scripted model
  2  cp .env.example .env && docker compose up -d
     uv run python scripts/run_server.py --real           # Groq via LiteLLM, Keycloak, Postgres
  3  docker compose --profile store up -d
     uv run python scripts/run_server.py --real --store   # + the real Saleor shop

EOF

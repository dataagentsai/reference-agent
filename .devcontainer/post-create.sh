#!/usr/bin/env bash
# Runs once when the Codespace is created. Installs the Python side only:
# nothing is started, because which compose profiles fit is a choice.
set -euo pipefail

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
# Runtime packages only, on the Python the laptops use (uv would pick the
# newest). Frozen, because uv otherwise re-resolves every source in the lock,
# and agenttwin (a dev extra) comes from ../agenttwin, a separate private
# repository a Codespace cannot see. Clone it beside this one to run the tests.
uv sync --frozen --python 3.13

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

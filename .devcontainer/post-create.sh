#!/usr/bin/env bash
# Runs once when the Codespace is created. Installs the Python side only:
# nothing is started, because which compose profiles fit is a choice.
set -euo pipefail

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
# AgentTwin lives in its own private repository, and pyproject reads it from
# ../agenttwin: the demo server builds its simulated shop from it, and the
# tests run against it. devcontainer.json asks for read access to the mirror
# (dataagentsai is on a free plan with no Codespaces, so both repos are
# mirrored under basantchoudhary); a local dev container uses the org's.
AGENTTWIN_REPO="${AGENTTWIN_REPO:-basantchoudhary/agenttwin}"
if [ ! -d ../agenttwin ]; then
  git clone --quiet "https://github.com/${AGENTTWIN_REPO}.git" ../agenttwin || {
    echo "Could not clone ${AGENTTWIN_REPO} into ../agenttwin; the server and tests need it." >&2
    exit 1
  }
fi

# Frozen: install exactly what the lock says. Python 3.13, the laptops' version
# (uv would otherwise pick the newest).
uv sync --frozen --extra dev --python 3.13

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

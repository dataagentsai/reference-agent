"""The compose file and the stack profile name the same products.

An adoption is recorded in three places: a verdict in the register
(clean-ai-engineering/TODO.md), a binding in `stacks/open-stack.yaml`, and a
service in `compose.yaml` that runs it. Chatwoot was once decided and absent
from two of the three, and it was a person who noticed. These tests notice
instead: a product bound in the stack must run from compose, and a service in
compose must be either a product the stack binds or named here as something a
product needs.

The stack profile lives in a sibling checkout, so those checks skip without it;
the compose file's own shape is checked regardless.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
STACK = ROOT.parent / "clean-ai-engineering" / "stacks" / "open-stack.yaml"

needs_stack = pytest.mark.skipif(
    not STACK.is_file(), reason="the stack profile is a sibling checkout"
)

DEFAULT = None
"""No profile: starts with a bare `docker compose up`."""

# (stack key, adapter it must name, compose service, profile, why that profile)
ADOPTED = [
    ("state", "postgres", "postgres", DEFAULT, "every item needs it"),
    ("model", "litellm-proxy", "litellm", DEFAULT, "T-029, next in cycle 1"),
    ("identity", "keycloak", "keycloak", DEFAULT, "T-002 blocks T-006, T-017, T-026"),
    (
        "telemetry",
        "otel-to-langfuse",
        "langfuse-web",
        "obs",
        "ClickHouse and MinIO are the heaviest pieces",
    ),
    ("workflow", "temporal", "temporal", "durable", "only T-028 needs it"),
    ("approval", "temporal-updates", "temporal", "durable", "the same Temporal as workflow"),
    (
        "channel",
        "chatwoot-agent-bot",
        "chatwoot",
        "channel",
        "Rails and Sidekiq, only T-026 and T-017 need it",
    ),
    ("x_store", "saleor", "saleor", "store", "Django, a worker and a dashboard: T-017 only"),
    (
        "x_metrics",
        "otel-collector-prometheus",
        "prometheus",
        "watch",
        "a collector, a metrics store, alerting and dashboards: T-055",
    ),
]

# Services that are no product's binding, and the product each exists for.
SUPPORTING = {
    "saleor-migrate": "saleor",
    "saleor-worker": "saleor",
    "saleor-dashboard": "saleor",
    "litellm-keys": "litellm",
    "langfuse-worker": "langfuse-web",
    "clickhouse": "langfuse-web",
    "minio": "langfuse-web",
    "redis": "langfuse-web",
    "chatwoot-migrate": "chatwoot",
    "chatwoot-setup": "chatwoot",
    "chatwoot-sidekiq": "chatwoot",
    "otel-collector": "prometheus",
    "alertmanager": "prometheus",
    "grafana": "prometheus",
}

# Adopted, and not yet in compose. Each row leaves when its item adds the service.
NOT_YET: list[tuple[str, str, str]] = []


@pytest.fixture(scope="module")
def services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


@pytest.fixture(scope="module")
def stack() -> dict:
    doc = yaml.safe_load(STACK.read_text())
    return {
        **doc["bindings"],
        **{k: v for k, v in doc.items() if k.startswith("x_") and isinstance(v, dict)},
    }


def profile_of(service: dict) -> str | None:
    profiles = service.get("profiles")
    return None if not profiles else profiles[0]


@pytest.mark.tooling
@needs_stack
@pytest.mark.parametrize(
    ("key", "adapter", "service", "profile", "why"), ADOPTED, ids=[r[0] for r in ADOPTED]
)
def test_a_bound_product_runs_from_compose(
    stack: dict, services: dict, key: str, adapter: str, service: str, profile: str | None, why: str
) -> None:
    assert stack[key]["adapter"] == adapter, (
        f"{key} is bound to {stack[key]['adapter']} now; update this row"
    )
    assert service in services, (
        f"{adapter} is adopted in the stack and has no service in compose.yaml"
    )
    assert profile_of(services[service]) == profile, (
        f"{service} belongs in profile {profile!r}: {why}"
    )


@pytest.mark.tooling
@needs_stack
@pytest.mark.parametrize(("key", "adapter", "item"), NOT_YET, ids=[r[0] for r in NOT_YET])
def test_a_product_not_yet_composed_is_still_owed(
    stack: dict, services: dict, key: str, adapter: str, item: str
) -> None:
    assert stack[key]["adapter"] == adapter
    assert stack[key]["x_item"] == item
    assert adapter not in services, (
        f"{adapter} is in compose now; move its row from NOT_YET to ADOPTED"
    )


@pytest.mark.tooling
def test_every_service_is_accounted_for(services: dict) -> None:
    bound = {row[2] for row in ADOPTED}
    for name, service in services.items():
        if name in bound:
            continue
        assert name in SUPPORTING, (
            f"{name} is neither a bound product nor supporting one; give it a verdict first"
        )
        needs = services[SUPPORTING[name]]
        assert set(service.get("profiles") or []) >= set(needs.get("profiles") or []), (
            f"{name} must start whenever {SUPPORTING[name]} does"
        )


@pytest.mark.tooling
def test_the_postgres_init_script_is_executable(services: dict) -> None:
    """Postgres's image runs an init script only if it is executable, and a
    failure there leaves a volume that never initialises again. The first boot
    of this file failed exactly so."""
    mounts = [
        v.split(":")[0]
        for v in services["postgres"]["volumes"]
        if "docker-entrypoint-initdb.d" in v
    ]
    assert mounts, "postgres mounts no init script"
    for mount in mounts:
        assert (ROOT / mount).stat().st_mode & 0o111, f"{mount} is not executable"


@pytest.mark.tooling
@needs_stack
def test_the_stack_says_where_it_runs_from() -> None:
    assert yaml.safe_load(STACK.read_text())["x_runs_from"] == "reference-agent/compose.yaml"

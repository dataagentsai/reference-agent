"""The `config` port's adapters: where a declared value is read from.

    environment-settings  the process environment, then a dotenv file, re-read
                          after `ttl_s`; `payout.automatic_limit_inr` is read
                          as PAYOUT_AUTOMATIC_LIMIT_INR (`prefix` before it).
                          A change to the dotenv file applies without a
                          restart; one to the process environment cannot.
    static                the overlay's own `values` (a test's; its hook
                          `values` may replace them while it runs).
    app-configuration     Azure App Configuration's key-value REST API, one
                          key under one `label`, with the managed identity's
                          token for `https://azconfig.io` — the Key Vault
                          adapter's pattern (`adapters.secrets`), no Azure SDK.
                          Re-read after `ttl_s` (30 s), so a value set with
                          `az appconfig kv set` applies without a redeploy.

Every adapter's product is `config.settings.Cached`: the agent's declared keys
(the hook `keys`) over that adapter's source, with the same first-load refusal,
last-good-value and unknown-key rules (`tests/test_config_port.py`).
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2

from agent_harness.adapters import Adapter, Setting, Wiring
from agent_harness.adapters.secrets import _dotenv, managed_identity_token
from agent_harness.config.settings import Cached, Settings, Source

APP_CONFIGURATION_SCOPE = "https://azconfig.io"
APP_CONFIGURATION_API = "2023-11-01"


def environment_name(key: str, prefix: str = "") -> str:
    """`payout.automatic_limit_inr` -> `PAYOUT_AUTOMATIC_LIMIT_INR`."""
    return prefix + key.upper().replace(".", "_").replace("-", "_")


def _cached(wiring: Wiring, source: Source, where: str) -> Cached:
    return Cached(
        keys=wiring.hooks["keys"],
        source=source,
        ttl_s=float(wiring.settings["ttl_s"]),
        where=where,
    )


@asynccontextmanager
async def _environment(wiring: Wiring) -> AsyncIterator[Settings]:
    prefix = str(wiring.settings.get("prefix") or "")
    dotenv = wiring.settings.get("dotenv")
    path: Path | None = wiring.base / str(dotenv) if dotenv else None

    def read(names: Sequence[str]) -> Mapping[str, object]:
        filed = _dotenv(path)
        found: dict[str, object] = {}
        for name in names:
            env = environment_name(name, prefix)
            value = os.environ.get(env, filed.get(env))
            if value is not None:
                found[name] = value
        return found

    yield _cached(wiring, read, "environment-settings")


@asynccontextmanager
async def _static(wiring: Wiring) -> AsyncIterator[Settings]:
    values: Mapping[str, object] = wiring.hooks.get("values") or wiring.settings["values"] or {}

    def read(names: Sequence[str]) -> Mapping[str, object]:
        return {n: values[n] for n in names if n in values}

    yield _cached(wiring, read, "static")


@asynccontextmanager
async def _app_configuration(wiring: Wiring) -> AsyncIterator[Settings]:
    endpoint = str(wiring.settings["endpoint"]).rstrip("/")
    label = wiring.settings.get("label")
    token = wiring.hooks.get(
        "token", lambda: managed_identity_token(resource=APP_CONFIGURATION_SCOPE)
    )
    with httpx2.Client(
        base_url=endpoint, transport=wiring.hooks.get("transport"), timeout=5.0
    ) as http:

        def read(names: Sequence[str]) -> Mapping[str, object]:
            headers = {"Authorization": f"Bearer {token()}"}
            asked = {"api-version": APP_CONFIGURATION_API, **({"label": label} if label else {})}
            found: dict[str, object] = {}
            for name in names:
                answer = http.get(
                    f"/kv/{urllib.parse.quote(name, safe='')}", params=asked, headers=headers
                )
                if answer.status_code == 404:
                    continue  # not held under this label: the declared default
                answer.raise_for_status()
                found[name] = answer.json()["value"]
            return found

        yield _cached(wiring, read, f"app-configuration {endpoint} (label {label})")


TTL = Setting(default=30)
ENVIRONMENT = Adapter(
    "config",
    "environment-settings",
    _environment,
    {"dotenv": Setting(), "prefix": Setting(default=""), "ttl_s": TTL},
    hooks=("keys",),
)
STATIC = Adapter(
    "config", "static", _static, {"values": Setting(default={}), "ttl_s": TTL}, hooks=("keys",)
)
APP_CONFIGURATION = Adapter(
    "config",
    "app-configuration",
    _app_configuration,
    {"endpoint": Setting(required=True), "label": Setting(), "ttl_s": TTL},
    hooks=("keys",),
)

__all__ = [
    "APP_CONFIGURATION",
    "APP_CONFIGURATION_API",
    "APP_CONFIGURATION_SCOPE",
    "ENVIRONMENT",
    "STATIC",
    "environment_name",
]

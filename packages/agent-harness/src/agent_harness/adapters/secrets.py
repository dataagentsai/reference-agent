"""The `secrets` port: a reference in an overlay, turned into the value.

An overlay never holds a secret. It names one: `{env: GROQ_API_KEY}`, or
`{key_vault: groq-api-key}`. The adapter bound here reads it.

    environment-settings  the process environment, then a dotenv file
    key-vault             Azure Key Vault over its REST API, with the
                          managed identity's token (Container Apps' identity
                          endpoint); no Azure SDK. Environment references
                          still read the environment, which Container Apps
                          also fills from Key Vault references.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent_harness.adapters import Adapter, OverlayRefused, Setting, Wiring

KEY_VAULT_API = "7.4"
VAULT_SCOPE = "https://vault.azure.net"


class SecretReader(Protocol):
    """The port: one reference in, its value out, or a refusal naming it."""

    def read(self, reference: Mapping[str, Any]) -> str: ...


def _dotenv(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    pairs = (line.partition("=") for line in path.read_text().splitlines())
    return {k.strip(): v.strip() for k, sep, v in pairs if sep and not k.lstrip().startswith("#")}


@dataclass(frozen=True)
class EnvironmentSecrets:
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    dotenv: Mapping[str, str] = field(default_factory=dict)

    def read(self, reference: Mapping[str, Any]) -> str:
        if "env" not in reference:
            raise OverlayRefused(f"{dict(reference)}: this deployment reads only {{env: NAME}}")
        name = str(reference["env"])
        found = self.environ.get(name, self.dotenv.get(name))
        if found is None and "default" in reference:
            return str(reference["default"])
        if found is None:
            raise OverlayRefused(f"{name} is not set (the environment, or the dotenv file)")
        return found


@dataclass(frozen=True)
class KeyVaultSecrets:
    """Key Vault by name over REST; `{env: …}` from the environment as above."""

    vault_url: str
    token: Any
    """A callable returning a bearer token for `VAULT_SCOPE`."""
    environment: EnvironmentSecrets = field(default_factory=EnvironmentSecrets)

    def read(self, reference: Mapping[str, Any]) -> str:
        if "key_vault" not in reference:
            return self.environment.read(reference)
        name = urllib.parse.quote(str(reference["key_vault"]))
        url = f"{self.vault_url.rstrip('/')}/secrets/{name}?api-version={KEY_VAULT_API}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token()}"})
        with urllib.request.urlopen(request, timeout=10) as answer:  # noqa: S310 — configured vault
            return str(json.load(answer)["value"])


def managed_identity_token(environ: Mapping[str, str] | None = None) -> str:
    """The managed identity's token for Key Vault, from Container Apps' endpoint."""
    env = os.environ if environ is None else environ
    endpoint, header = env.get("IDENTITY_ENDPOINT"), env.get("IDENTITY_HEADER")
    if not endpoint or not header:
        raise OverlayRefused("no managed identity here (IDENTITY_ENDPOINT is unset)")
    query = urllib.parse.urlencode({"resource": VAULT_SCOPE, "api-version": "2019-08-01"})
    request = urllib.request.Request(f"{endpoint}?{query}", headers={"X-IDENTITY-HEADER": header})
    with urllib.request.urlopen(request, timeout=10) as answer:  # noqa: S310 — platform endpoint
        return str(json.load(answer)["access_token"])


@asynccontextmanager
async def _environment(wiring: Wiring) -> AsyncIterator[SecretReader]:
    dotenv = wiring.settings.get("dotenv")
    yield EnvironmentSecrets(dotenv=_dotenv(wiring.base / str(dotenv) if dotenv else None))


@asynccontextmanager
async def _key_vault(wiring: Wiring) -> AsyncIterator[SecretReader]:
    token = wiring.hooks.get("token", managed_identity_token)
    yield KeyVaultSecrets(vault_url=str(wiring.settings["vault_url"]), token=token)


ENVIRONMENT = Adapter("secrets", "environment-settings", _environment, {"dotenv": Setting()})
KEY_VAULT = Adapter("secrets", "key-vault", _key_vault, {"vault_url": Setting(required=True)})

__all__ = [
    "ENVIRONMENT",
    "KEY_VAULT",
    "EnvironmentSecrets",
    "KeyVaultSecrets",
    "SecretReader",
    "managed_identity_token",
]

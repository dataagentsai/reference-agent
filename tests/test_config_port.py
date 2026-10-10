"""The `config` port's contract table: the same rows on every adapter (A6).

Each adapter is built through the registry, as an overlay builds it, over a
source the test holds and changes:

    environment-settings  a dotenv file (the process environment is read first;
                          these names are never set in it)
    static                the hook `values`, a dict the test changes
    app-configuration     App Configuration's key-value REST API answered in
                          process (httpx MockTransport); no network, no Azure

Rows: a value read; a default when absent; an invalid value, or one its check
refuses, keeps the last good one; a change shows after a refresh, and after the
TTL without one; a dropped key reads as its default; an unknown key is refused.
And a value that cannot be used on the first load stops the start.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from agent_harness.adapters import ADAPTERS, Adapter, Wiring
from agent_harness.adapters.config import APP_CONFIGURATION_API, environment_name
from agent_harness.config.settings import Key, SettingRefused, UnknownSetting, between

LIMIT = Key("payout.automatic_limit_inr", Decimal, Decimal("25000"), check=between(1, 25000))
RETRIES = Key("payout.retries", int, 3)
GREETING = Key("chat.greeting", str, "hello")
KEYS = (LIMIT, RETRIES, GREETING)
UNDECLARED = Key("payout.automatic_limit_usd", Decimal, Decimal("300"))


@dataclass
class Store:
    """What an adapter reads from, as the test holds it: name -> raw value."""

    held: dict[str, str] = field(default_factory=dict)
    asked: list[httpx.Request] = field(default_factory=list)


class Source:
    """One adapter's source: how the test writes to it and builds over it."""

    name = ""

    def settings(self, tmp_path: Path) -> dict[str, Any]:
        return {}

    def hooks(self, store: Store, tmp_path: Path) -> dict[str, Any]:
        return {}

    def write(self, store: Store, tmp_path: Path) -> None:
        """Make the source hold `store.held` (the dotenv file; others read it live)."""


class EnvironmentSource(Source):
    name = "environment-settings"

    def settings(self, tmp_path: Path) -> dict[str, Any]:
        return {"dotenv": "test.env"}

    def write(self, store: Store, tmp_path: Path) -> None:
        lines = (f"{environment_name(k)}={v}" for k, v in store.held.items())
        (tmp_path / "test.env").write_text("\n".join(lines) + "\n")


class StaticSource(Source):
    name = "static"

    def hooks(self, store: Store, tmp_path: Path) -> dict[str, Any]:
        return {"values": store.held}


class AppConfigurationSource(Source):
    name = "app-configuration"
    endpoint = "https://appcs-test.azconfig.io"

    def settings(self, tmp_path: Path) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "label": "dev"}

    def hooks(self, store: Store, tmp_path: Path) -> dict[str, Any]:
        def answer(request: httpx.Request) -> httpx.Response:
            store.asked.append(request)
            if request.headers.get("authorization") != "Bearer mi-token":
                return httpx.Response(401)
            if request.url.params.get("label") != "dev":
                return httpx.Response(404)
            key = request.url.path.removeprefix("/kv/")
            if key not in store.held:
                return httpx.Response(404, json={"title": "Not Found"})
            body = {"key": key, "label": "dev", "value": store.held[key], "etag": "e1"}
            return httpx.Response(200, content=json.dumps(body))

        return {"transport": httpx.MockTransport(answer), "token": lambda: "mi-token"}


SOURCES = [EnvironmentSource(), StaticSource(), AppConfigurationSource()]


@asynccontextmanager
async def built(
    source: Source, store: Store, tmp_path: Path, ttl_s: float = 3600
) -> AsyncIterator[Any]:
    chosen: Adapter = ADAPTERS.load("config", source.name)
    settings = {k: s.default for k, s in chosen.settings.items()}
    settings |= {**source.settings(tmp_path), "ttl_s": ttl_s}
    hooks = {"keys": KEYS, **source.hooks(store, tmp_path)}
    source.write(store, tmp_path)
    async with chosen.build(Wiring(settings, {}, hooks, "test", tmp_path)) as settings_port:
        yield settings_port


Change = Callable[[dict[str, str]], None]


def put(name: str, value: str) -> Change:
    return lambda held: held.__setitem__(name, value)


def drop(name: str) -> Change:
    return lambda held: held.pop(name, None)


L = LIMIT.name
# (row, held at start, then changed, refresh how, the key asked, the value or the refusal)
ROWS: list[tuple[str, dict[str, str], Change | None, str, Key[Any], object]] = [
    ("a value is read", {L: "20000"}, None, "", LIMIT, Decimal("20000")),
    ("an int is read as an int", {RETRIES.name: "5"}, None, "", RETRIES, 5),
    ("a default when absent", {}, None, "", LIMIT, Decimal("25000")),
    (
        "an unreadable value keeps the last good one",
        {L: "20000"},
        put(L, "twenty thousand"),
        "refresh",
        LIMIT,
        Decimal("20000"),
    ),
    (
        "a value its check refuses keeps the last good one",
        {L: "20000"},
        put(L, "30000"),
        "refresh",
        LIMIT,
        Decimal("20000"),
    ),
    (
        "a change shows after a refresh",
        {L: "20000"},
        put(L, "15000"),
        "refresh",
        LIMIT,
        Decimal(15000),
    ),
    ("a change waits for the TTL", {L: "20000"}, put(L, "15000"), "", LIMIT, Decimal("20000")),
    ("a change shows after the TTL", {L: "20000"}, put(L, "15000"), "ttl", LIMIT, Decimal("15000")),
    ("a dropped key reads as its default", {L: "20000"}, drop(L), "refresh", LIMIT, Decimal(25000)),
    ("an unknown key is refused", {L: "20000"}, None, "", UNDECLARED, UnknownSetting),
]


@pytest.mark.discharges("AHC-0022", "AHC-0003")
@pytest.mark.parametrize("source", SOURCES, ids=[s.name for s in SOURCES])
@pytest.mark.parametrize(
    ("row", "held", "change", "refresh", "key", "expected"), ROWS, ids=[r[0] for r in ROWS]
)
async def test_the_config_port(
    tmp_path: Path,
    source: Source,
    row: str,
    held: dict[str, str],
    change: Change | None,
    refresh: str,
    key: Key[Any],
    expected: object,
) -> None:
    store = Store(dict(held))
    async with built(source, store, tmp_path) as settings:
        if change is not None:
            change(store.held)
            source.write(store, tmp_path)
        if refresh == "refresh":
            settings.refresh()
        if refresh == "ttl":
            settings.ttl_s = 0
        if isinstance(expected, type):
            with pytest.raises(expected):
                settings.get(key)
            return
        assert settings.get(key) == expected, row
        assert type(settings.get(key)) is key.kind


# (row, held at start) — nothing good to keep yet, so nothing starts
FIRST_LOAD: list[tuple[str, dict[str, str]]] = [
    ("an unreadable value", {L: "lots"}),
    ("a value its check refuses", {L: "0"}),
    ("an int that is not one", {RETRIES.name: "3.5"}),
]


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize("source", SOURCES, ids=[s.name for s in SOURCES])
@pytest.mark.parametrize(("row", "held"), FIRST_LOAD, ids=[r[0] for r in FIRST_LOAD])
async def test_a_bad_value_on_the_first_load_stops_the_start(
    tmp_path: Path, source: Source, row: str, held: dict[str, str]
) -> None:
    with pytest.raises(SettingRefused):
        async with built(source, Store(dict(held)), tmp_path):
            pass


@pytest.mark.discharges("AHC-0022")
async def test_app_configuration_asks_with_the_identity_and_the_label(tmp_path: Path) -> None:
    store = Store({L: "20000"})
    async with built(AppConfigurationSource(), store, tmp_path) as settings:
        settings.get(LIMIT)
    asked = {r.url.path: r for r in store.asked}
    sent = asked["/kv/payout.automatic_limit_inr"]
    assert sent.url.host == "appcs-test.azconfig.io"
    assert sent.url.params["label"] == "dev"
    assert sent.url.params["api-version"] == APP_CONFIGURATION_API
    assert sent.headers["authorization"] == "Bearer mi-token"


@pytest.mark.discharges("AHC-0022")
async def test_an_unreachable_store_keeps_the_last_good_value(tmp_path: Path) -> None:
    store = Store({L: "20000"})
    async with built(AppConfigurationSource(), store, tmp_path) as settings:
        source = settings.source

        def down(names: Any) -> Any:
            raise httpx.ConnectError("no route to appcs-test")

        settings.source = down
        settings.refresh()
        assert settings.get(LIMIT) == Decimal("20000")
        assert "could not be read" in settings.problems[0]
        settings.source = source


@pytest.mark.discharges("AHC-0022")
def test_a_default_its_own_check_refuses_is_a_bug_at_import() -> None:
    with pytest.raises(ValueError, match="default"):
        Key("payout.automatic_limit_inr", Decimal, Decimal("30000"), check=between(1, 25000))


SWITCH = Key("agent.enabled", bool, True)
# (how the store writes it, what it reads as; None: refused)
BOOLEANS = [
    ("true", True),
    ("False", False),
    (" 0 ", False),
    ("on", True),
    ("off", False),
    ("paused", None),
    ("", None),
]


@pytest.mark.discharges("AHC-0022")
@pytest.mark.parametrize(("raw", "read"), BOOLEANS, ids=[repr(b[0]) for b in BOOLEANS])
def test_a_bool_is_read_from_a_few_spellings_and_nothing_else(raw: str, read: bool | None) -> None:
    """claims-fnol-azure A13: `agent.enabled`. A value that is not plainly a bool
    is refused, never guessed: a typo must not switch an agent on or off."""
    if read is None:
        with pytest.raises(ValueError, match="not true or false"):
            SWITCH.parse(raw)
    else:
        assert SWITCH.parse(raw) is read

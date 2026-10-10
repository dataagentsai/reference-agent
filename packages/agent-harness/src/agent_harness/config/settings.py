"""The `config` port: named, typed values that may change while the app runs.

L15. A threshold a business owner moves (a payout limit, a flag) is not a
constant in the image and not a setting read once at start: the Azure stack
binds `config: app-configuration` so that a change applies without a redeploy
(claims-fnol-azure A6, Tier 5's exercise). The port is small on purpose:

    Key        one value: the source key it is stored under
               (`payout.automatic_limit_inr`), its type (str, int, Decimal or
               bool — `agent.enabled`, A13),
               a declared default for when the source does not hold it, and a
               check that says why a value is not acceptable.
    Settings   the port: `get(key)` the current value; `refresh()` re-read now.
    Cached     what every adapter is: a catalogue of declared keys over a
               `Source` (the adapter's one job: names in, the raw values it
               holds out), re-read when older than `ttl_s`.

**Read once per decision, and record it.** The catalog's config port asks for
one snapshot per unit of work (AHC-0003): a caller reads the value once where
it decides, and puts the value it used on the record of that decision. A
refresh between two decisions is how a change applies to the next one.

**A bad value never replaces a good one.** On the first load an unreadable
value, a source that cannot be reached, or a value its check refuses is a
`SettingRefused`, and nothing starts. After that the same faults keep the last
good value and are kept in `problems` and logged, so a typo in the store does
not stop payouts or loosen them. A key the source no longer holds reads as its
default, which is a declared statement, not a fault.

**An unknown key is refused** (`UnknownSetting`): a value nobody declared has no
type, no default and no check, so asking for one is a bug, found where it is.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from agent_harness.contracts.failures import AgentFailure, Fault

log = logging.getLogger(__name__)


_BOOLEANS = {"true": True, "1": True, "yes": True, "on": True}
_BOOLEANS |= {"false": False, "0": False, "no": False, "off": False}
"""How a bool is written in a store or an environment: nothing else is read as one."""


class SettingRefused(AgentFailure):
    """A value could not be read, or its check refused it, on the first load."""

    fault = Fault.MISCONFIGURED


class UnknownSetting(SettingRefused):
    """A key the catalogue does not declare was asked for."""


@dataclass(frozen=True)
class Key[T: (str, int, Decimal, bool)]:
    """One named, typed value and what it is when the source does not hold it."""

    name: str
    kind: type[T]
    default: T
    check: Callable[[T], str | None] | None = None
    """Why a value is not acceptable, or `None`; the default must pass it."""

    def __post_init__(self) -> None:
        if self.kind not in (str, int, Decimal, bool):
            raise TypeError(f"{self.name}: a setting is a str, an int, a Decimal or a bool")
        why = self.check(self.default) if self.check else None
        if why is not None:
            raise ValueError(f"{self.name}: the default {self.default!r} is refused: {why}")

    def parse(self, raw: object) -> T:
        """The raw value as this key's type and checked, or `ValueError` saying why."""
        text = str(raw).strip()
        value: Any = _converted(self.name, self.kind, text)
        why = self.check(value) if self.check else None
        if why is not None:
            raise ValueError(f"{self.name}={text!r} is refused: {why}")
        return cast(T, value)


def _converted(name: str, kind: type, text: str) -> Any:
    """`text` as `kind`, or `ValueError` saying why it is not one."""
    if kind is Decimal:
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"{name}={text!r} is not a number") from None
        if not number.is_finite():
            raise ValueError(f"{name}={text!r} is not a finite number")
        return number
    if kind is bool:
        if text.lower() not in _BOOLEANS:
            raise ValueError(f"{name}={text!r} is not true or false")
        return _BOOLEANS[text.lower()]
    if kind is int:
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{name}={text!r} is not a whole number") from None
    return text


def between(low: Decimal | int, high: Decimal | int) -> Callable[[Any], str | None]:
    """A check: at least `low` and at most `high`."""

    def check(value: Any) -> str | None:
        return None if low <= value <= high else f"must be between {low} and {high}"

    return check


class Settings(Protocol):
    """The port: a declared key in, its current value out."""

    def get[T: (str, int, Decimal, bool)](self, key: Key[T]) -> T: ...

    def refresh(self) -> None: ...


Source = Callable[[Sequence[str]], Mapping[str, object]]
"""An adapter's reader: the declared names in, the raw values it holds out
(a name it does not hold is left out). Raises when it cannot be reached."""


@dataclass
class Cached:
    """Every adapter's product: declared keys over a source, re-read after `ttl_s`."""

    keys: Iterable[Key[Any]]
    source: Source
    ttl_s: float = 30.0
    where: str = "config"
    """The adapter's name, in every refusal."""
    clock: Callable[[], float] = time.monotonic
    problems: list[str] = field(default_factory=list)
    """What the last refresh could not use, while the last good values held."""
    _declared: dict[str, Key[Any]] = field(init=False, default_factory=dict)
    _values: dict[str, Any] = field(init=False, default_factory=dict)
    _read_at: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self._declared = {k.name: k for k in self.keys}
        try:
            self._values = self._read()
        except Exception as exc:  # noqa: BLE001 — nothing good to keep yet: nothing starts
            raise SettingRefused(f"{self.where}: {exc}") from None
        self._read_at = self.clock()

    def get[T: (str, int, Decimal, bool)](self, key: Key[T]) -> T:
        if self._declared.get(key.name) != key:
            known = ", ".join(sorted(self._declared)) or "none"
            raise UnknownSetting(f"{self.where}: {key.name!r} is not declared; declared: {known}")
        if self.clock() - self._read_at >= self.ttl_s:
            self.refresh()
        return cast(T, self._values[key.name])

    def refresh(self) -> None:
        """Re-read every key; a fault keeps the last good value and is kept."""
        self._read_at = self.clock()
        try:
            raw = self.source(list(self._declared))
        except Exception as exc:  # noqa: BLE001 — any fault of the source keeps the values
            self._kept([f"{self.where} could not be read: {exc}"])
            return
        problems: list[str] = []
        for name, key in self._declared.items():
            try:
                self._values[name] = key.parse(raw[name]) if name in raw else key.default
            except ValueError as exc:
                problems.append(f"{exc}; kept {self._values[name]!r}")
        self._kept(problems)

    def _read(self) -> dict[str, Any]:
        raw = self.source(list(self._declared))
        return {n: k.parse(raw[n]) if n in raw else k.default for n, k in self._declared.items()}

    def _kept(self, problems: list[str]) -> None:
        self.problems = problems
        for problem in problems:
            log.warning("config: %s", problem)


def defaults(*keys: Key[Any]) -> Cached:
    """The declared defaults and nothing else: for code with no config port
    bound (a test, the gates' binding). Built through the same rules, so an
    unknown key is refused here too."""
    return Cached(keys, lambda _names: {}, ttl_s=float("inf"), where="the declared defaults")


__all__ = [
    "Cached",
    "Key",
    "SettingRefused",
    "Settings",
    "Source",
    "UnknownSetting",
    "between",
    "defaults",
]

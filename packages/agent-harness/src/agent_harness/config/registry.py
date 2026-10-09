"""Names to implementations, chosen from configuration: the one plug-in table.

L15. A stack profile says `model: apim-ai-gateway`; an environment overlay says
`model: scripted`. Something has to turn that word into a constructed thing,
and if it is an `if name == ...` chain then adding a cloud means editing the
chain. Here a name is registered once, as a module path string, and looked up.

**Lazy.** A registration is `"package.module:attribute"`, imported only when that
name is chosen, so choosing `scripted` never imports Pydantic AI and an optional
SDK stays optional (the import contracts confine each SDK to one adapter module;
this keeps a process from loading the ones it did not pick).

**Fails at startup, listing what exists.** An unknown name is a configuration
mistake, and the person who made it needs the list of right answers, not a
`KeyError`.

The same table holds every kind of plug-in — the ports' adapters
(`agent_harness.adapters`) and the evaluators' kinds (`agent_harness.evals`) —
so there is one way to add one.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping
from typing import Any

from agent_harness.contracts.failures import AgentFailure, Fault


class UnknownName(AgentFailure):
    """A configuration named a plug-in nobody registered."""

    fault = Fault.MISCONFIGURED


class NotBuilt(AgentFailure):
    """A registered name whose implementation does not exist yet. Raised only
    when a configuration actually chooses it."""

    fault = Fault.MISCONFIGURED


class Registry:
    """Plug-in names per slot (a port, or the evaluators' kinds), each a lazy target."""

    def __init__(self, what: str, table: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self.what = what
        """What the slots are called in an error: "port", "evaluator kind"."""
        self._table: dict[str, dict[str, str]] = {
            slot: dict(names) for slot, names in (table or {}).items()
        }

    def register(self, slot: str, name: str, target: str) -> None:
        """Add `name` under `slot`. `target` is `"module:attribute"`. Registering
        a name twice is refused: two meanings for one word in a YAML is a bug."""
        module, sep, attribute = target.partition(":")
        if not (module and sep and attribute):
            raise ValueError(f"{target!r}: expected 'package.module:attribute'")
        held = self._table.setdefault(slot, {})
        if held.get(name, target) != target:
            raise ValueError(f"{self.what} {slot!r} already has {name!r} ({held[name]})")
        held[name] = target

    def slots(self) -> tuple[str, ...]:
        return tuple(sorted(self._table))

    def names(self, slot: str) -> tuple[str, ...]:
        return tuple(sorted(self._table.get(slot, {})))

    def target(self, slot: str, name: str) -> str:
        """The registration, or `UnknownName` listing the known ones."""
        if slot not in self._table:
            raise UnknownName(f"no {self.what} {slot!r}; known: {', '.join(self.slots())}")
        try:
            return self._table[slot][name]
        except KeyError:
            known = ", ".join(self.names(slot))
            raise UnknownName(f"{self.what} {slot!r} has no {name!r}; known: {known}") from None

    def load(self, slot: str, name: str) -> Any:
        """Import and return the registered attribute. Only now is its module,
        and whatever SDK it imports, loaded."""
        module, _, attribute = self.target(slot, name).partition(":")
        return getattr(importlib.import_module(module), attribute)

    def merged(self, extra: Iterable[tuple[str, str, str]]) -> Registry:
        """A copy with `(slot, name, target)` added — an agent's own adapters
        beside the library's, without touching the library's table."""
        copy = Registry(self.what, self._table)
        for slot, name, target in extra:
            copy.register(slot, name, target)
        return copy


__all__ = ["NotBuilt", "Registry", "UnknownName"]

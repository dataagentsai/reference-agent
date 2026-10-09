"""Evaluator kinds that are named and not built: the slots later tiers fill.

Registered so `evaluators.yaml` speaks one vocabulary from the start, and so a
YAML that uses one fails at startup saying when it arrives, rather than as an
unknown name. Each gets its own adapter module when built (Azure's imports the
Azure SDK there and nowhere else), and its registration moves to point at it.
Each declares its fields now, so a misspelt one is refused today.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from agent_harness.config.registry import NotBuilt


class _Stub:
    kind = ""
    arrives = ""
    fields: frozenset[str] = frozenset()

    @classmethod
    def build(cls, name: str, spec: Mapping[str, Any], catalogue: Mapping[str, Any]) -> NoReturn:
        raise NotBuilt(f"evaluator {name!r}: kind {cls.kind!r} is not built yet ({cls.arrives})")


class GuardrailLog(_Stub):
    """A guardrail's own verdict, read from its log (Content Safety at APIM)."""

    kind, arrives = "guardrail_log", "Tier 3/12"
    fields = frozenset({"source", "categories", "threshold"})


class Presidio(_Stub):
    """Personal data in a reply, found by Presidio's analyzers."""

    kind, arrives = "presidio", "Tier 3/12"
    fields = frozenset({"entities", "language", "threshold"})


class AzureEvaluator(_Stub):
    """An Azure AI Foundry built-in evaluator (`builtin.*`), local SDK or cloud run."""

    kind, arrives = "azure", "Tier 3/12"
    fields = frozenset({"name", "judge", "threshold"})


class OpenModel(_Stub):
    """An open-weight judge or checker (MiniCheck and the like) on a free host."""

    kind, arrives = "open_model", "Tier 3/12"
    fields = frozenset({"model", "threshold", "expected_ms"})


__all__ = ["AzureEvaluator", "GuardrailLog", "OpenModel", "Presidio"]

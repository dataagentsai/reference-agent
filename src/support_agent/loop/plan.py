"""What the model asked for, turned into calls the harness will make.

Pure, and deliberately holding no state: the trace belongs to the run and the
counting belongs here. Two rules live in this file, and both exist because of
where they are applied rather than what they compute.

**A call's identity is its name and its arguments, with the keys sorted.**
`{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` are the same call, and a detector that
thought otherwise would never fire — which is the failure mode of every
oscillation check somebody wrote against unsorted JSON.

**Circling is judged on that identity, not on the tool.** Reading three different
orders is a trajectory. Reading one order three times is a model that has stopped
making progress and not noticed, and it will spend the whole step budget before
anything else catches it.
"""

from __future__ import annotations

import json
from collections import Counter


def signature(name: str, arguments: dict[str, object]) -> tuple[str, str]:
    """A call's identity for oscillation purposes: the tool and its arguments."""
    return name, json.dumps(arguments, sort_keys=True, default=str)


def circling(counts: Counter[tuple[str, str]], call: tuple[str, str], threshold: int) -> bool:
    """Whether this exact call has now been made `threshold` times."""
    return counts[call] >= threshold


__all__ = ["circling", "signature"]

"""Resolving a harness profile's `extends`: the stack it inherits, flattened.

L15. The composition (`agent_harness.adapters`) reads a profile to learn which
adapter each port is bound to, and a profile names most of them only through the
stack it extends (`clean-ai-engineering/stacks/*.yaml`). So the library resolves
`extends` itself, with the catalog's rules (`ai-harness-catalog/tools/resolve.js`,
which is normative: where the two disagree, the catalog is right). The reference
agent's `evals/profile.py` is this module, and its test pins the two
implementations against each other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

OBJECT_KEYS = ("harness", "decisions", "thresholds")
LIST_KEYS = ("accepted_gaps", "not_applicable", "x_untested")


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def chain(path: Path, seen: tuple[Path, ...] = ()) -> list[tuple[Path, dict[str, Any]]]:
    """The profile and everything it extends, baseline first.

    `extends` is relative to the file that declares it, so a stack published in
    another repository is reached by a relative path — which is the normal case
    rather than the exotic one.
    """
    full = path.resolve()
    if full in seen:
        loop = " -> ".join(p.name for p in (*seen, full))
        raise ValueError(f"extends loops: {loop}")
    if not full.is_file():
        origin = f" (extended from {seen[-1].name})" if seen else ""
        raise FileNotFoundError(f"no such profile: {path}{origin}")
    doc: dict[str, Any] = yaml.safe_load(full.read_text()) or {}
    if not doc.get("extends"):
        return [(full, doc)]
    parent = (full.parent / doc["extends"]).resolve()
    return [*chain(parent, (*seen, full)), (full, doc)]


def merge(base: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """One child onto one baseline. Neither argument is modified."""
    out = {**base, **child}
    out.pop("extends", None)

    for key in OBJECT_KEYS:
        if _is_object(base.get(key)) or _is_object(child.get(key)):
            out[key] = {**(base.get(key) or {}), **(child.get(key) or {})}
    for key in LIST_KEYS:
        if base.get(key) is not None or child.get(key) is not None:
            out[key] = [*(child.get(key) or []), *(base.get(key) or [])]

    # The only two-level merge, and the level is the point: a child adds
    # `x_scopes` to a port without restating the adapter it is not changing.
    if _is_object(base.get("bindings")) or _is_object(child.get("bindings")):
        bindings = dict(base.get("bindings") or {})
        for port, spec in (child.get("bindings") or {}).items():
            bindings[port] = (
                {**bindings[port], **spec}
                if _is_object(bindings.get(port)) and _is_object(spec)
                else spec
            )
        out["bindings"] = bindings
    return out


def resolve(path: Path) -> dict[str, Any]:
    """The flattened profile — what the agent actually declares."""
    links = chain(path)
    doc = links[0][1]
    for _, child in links[1:]:
        doc = merge(doc, child)
    return doc


__all__ = ["chain", "merge", "resolve"]

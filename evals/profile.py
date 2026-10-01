"""Resolving `extends` — the same rules the catalog's own tool applies.

T-033. The profile stopped being complete on its own the day it started
inheriting: every product it runs on is named once, in the stack file, and this
is what flattens the two into the document everything else reads.

**Why this exists twice.** The normative implementation is the catalog's
(`ai-harness-catalog/tools/resolve.js`), because `extends` is a property of the
published format and belongs with the schema that declares it. This repository
cannot call it — a Python test suite shelling out to Node for a twenty-line
merge would make the catalog a runtime dependency of every test run. So the
rules are implemented twice and **pinned against each other**: the tool is run
once, in a test marked `tooling`, and its output must equal this module's. Two
implementations that agree because something checks are a different thing from
two that agree because nobody looked.

The merge rules are stated in `resolve.js`'s header and repeated here only as
far as the code shows them. Where the two disagree, the catalog is right.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

OBJECT_KEYS = ("harness", "decisions", "thresholds")
LIST_KEYS = ("accepted_gaps", "not_applicable", "x_untested")


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def chain(path: Path, seen: tuple[Path, ...] = ()) -> list[tuple[Path, dict]]:
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
    doc = yaml.safe_load(full.read_text()) or {}
    if not doc.get("extends"):
        return [(full, doc)]
    parent = (full.parent / doc["extends"]).resolve()
    return [*chain(parent, (*seen, full)), (full, doc)]


def merge(base: dict, child: dict) -> dict:
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


def resolve(path: Path) -> dict:
    """The flattened profile — what the agent actually declares."""
    links = chain(path)
    doc = links[0][1]
    for _, child in links[1:]:
        doc = merge(doc, child)
    return doc


__all__ = ["chain", "merge", "resolve"]

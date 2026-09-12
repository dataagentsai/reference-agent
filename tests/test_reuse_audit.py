"""G0.10's floor: every module is placed in a layer, and the placement is checked.

The audit's value is the prediction it makes *before* a second agent exists —
measured afterwards it is a rationalisation. What this test protects is the
smaller thing that makes the prediction survive: a module added tomorrow has to
be placed, and a module that moves has to be re-placed, or the classification
quietly stops describing the code it claims to describe.
"""

from __future__ import annotations

import pytest
from evals.reuse import LAYERS, classify, modules


@pytest.mark.tooling
def test_every_module_is_placed_in_a_layer() -> None:
    placed, present = classify(), modules()
    assert sorted(present - set(placed)) == [], "unclassified — which layer does it belong to?"
    assert sorted(set(placed) - present) == [], "classified and gone — the audit is stale"


@pytest.mark.tooling
def test_a_module_belongs_to_exactly_one_layer() -> None:
    seen: dict[str, str] = {}
    for layer, members in LAYERS.items():
        for module in members:
            assert module not in seen, f"{module} is both {seen[module]} and {layer}"
            seen[module] = layer


@pytest.mark.tooling
def test_the_domain_measurement_agrees_with_the_classification() -> None:
    """The floor measurement and the reading should not contradict each other.

    A module carrying many entity words that was called mechanism is the
    interesting disagreement: either the reading is wrong, or the words are
    incidental — and the second needs saying rather than assuming.
    """
    import ast
    import re

    from evals.reuse import SRC

    entity = re.compile(
        r"\b(order|orders|order_id|refund\w*|open_return\w*|returned|cancel\w*|"
        r"deliver(y|ed|ies)|shipped|parcel|carrier|coupon|discount|voucher|fraud|"
        r"final_sale|days_since_delivery)\b",
        re.I,
    )
    english = re.compile(r"in the order|the order (they|asked|it)|ordered by|order of", re.I)
    placed = classify()

    loud = []
    for path in SRC.rglob("*.py"):
        source = path.read_text()
        tree = ast.parse(source)
        spans: set[int] = set()
        for node in ast.walk(tree):
            kinds = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            if isinstance(node, kinds) and ast.get_docstring(node, clean=False) is not None:
                body = node.body[0]
                spans.update(range(body.lineno, (body.end_lineno or body.lineno) + 1))
        code = [
            line
            for n, line in enumerate(source.splitlines(), 1)
            if n not in spans and line.strip() and not line.strip().startswith("#")
        ]
        hits = [line for line in code if entity.search(line) and not english.search(line)]
        name = str(path.relative_to(SRC))
        if placed.get(name) == "mechanism" and len(hits) > 8:
            loud.append(f"{name}: {len(hits)} lines name an entity, and it is called mechanism")

    assert loud == [], "\n".join(loud)

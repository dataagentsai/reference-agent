"""The patterns this agent uses were entailed, not chosen — and the test is the
direction that can fail.

That every pattern in the library appears in the view is nearly circular: the
library was extracted from this reference, and the reference implements what its
specification says. **The check that can fail is the other way round** — a
pattern the reference demonstrably uses that no statement entails is a *decision*,
and a decision belongs in the profile with its source, not in a derived view
pretending to be inevitable.

So the assertion is: nothing in the library is un-entailed here. When that stops
being true, the pattern that broke it is the interesting one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from evals.pattern_view import entailed

LIBRARY = Path(__file__).resolve().parents[2] / "ai-harness-catalog" / "patterns"


def library_ids() -> set[str]:
    found: set[str] = set()
    for path in sorted(LIBRARY.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        found |= {p["id"] for p in doc.get("patterns", [])}
    return found


@pytest.mark.skipif(not LIBRARY.is_dir(), reason="the pattern library is a sibling checkout")
@pytest.mark.discharges("B13")
def test_every_pattern_this_agent_uses_is_entailed_by_its_specification() -> None:
    chosen = library_ids() - set(entailed())
    assert chosen == set(), (
        "these patterns are used and no statement forces them — each is a decision, "
        f"and belongs in the profile with its source: {sorted(chosen)}"
    )


@pytest.mark.skipif(not LIBRARY.is_dir(), reason="the pattern library is a sibling checkout")
@pytest.mark.tooling
def test_the_view_names_only_patterns_that_exist() -> None:
    """A view citing a pattern nobody wrote reads as coverage and is a typo."""
    invented = set(entailed()) - library_ids()
    assert invented == set(), (
        f"the view names patterns the library does not hold: {sorted(invented)}"
    )


@pytest.mark.tooling
def test_every_entailment_names_the_statement_that_forces_it() -> None:
    """A pattern with no reason is an assertion, and the reason is the whole point."""
    for pattern, reasons in entailed().items():
        assert reasons, f"{pattern} is entailed by nothing in particular"
        assert all(r.strip() for r in reasons), pattern

"""Every mechanism file says why it exists.

T-012 asked which statement requires each of the 46 files a second agent would
keep. The verdicts are authored — a reason is judgement and cannot be measured —
so the only thing a test can hold is that **none is missing**.

That is the same guard `evals/reuse.py` uses on its own classification: a module
with no layer fails the test, so the split cannot rot quietly as modules are
added. Here a module with no *reason* fails, for the same reason. A document
listing 46 of 51 files is worse than none, because the five it omits look
examined.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from evals.reuse import AGENT, classify

DOC = Path(__file__).parent.parent / "docs" / "WHY-EACH-FILE.md"


@pytest.mark.tooling
def test_every_mechanism_file_has_a_recorded_reason() -> None:
    text = DOC.read_text(encoding="utf-8")
    mechanism = sorted(n for n, layer in classify().items() if layer == "mechanism")
    missing = [n for n in mechanism if f"`{n}`" not in text]
    assert missing == [], (
        f"{len(missing)} mechanism file(s) have no entry in {DOC.name}: {', '.join(missing)}. "
        "A new module in the layer a second agent keeps needs a reason recorded beside the "
        "others, or the document quietly stops covering what it claims to."
    )


@pytest.mark.tooling
def test_the_document_names_no_file_that_is_not_mechanism() -> None:
    """The other direction, so a file that moves layer is noticed.

    A module reclassified from mechanism to per-agent still has its reason
    recorded here, which would read as a claim about the wrong 77%.
    """
    text = DOC.read_text(encoding="utf-8")
    placed = classify()
    # Since T-019 the agent's files are named `support_agent/...` and the
    # document names library paths, so a file that left the library is looked
    # for by the path it had there — unless the library has a file of that name.
    stale = [
        name
        for name, layer in placed.items()
        if layer != "mechanism"
        and f"`{name.removeprefix(AGENT)}`" in text
        and placed.get(name.removeprefix(AGENT)) != "mechanism"
        and name.removeprefix(AGENT) != "__init__.py"
    ]
    assert stale == [], (
        f"{DOC.name} records a reason for {', '.join(stale)}, which is no longer mechanism. "
        "Either the classification moved or the reason belongs somewhere else."
    )

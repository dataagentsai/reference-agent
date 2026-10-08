"""What a watch rule's verdict is evidence of, and the verdict itself.

A rule (`watch.rules`) decides whether a turn is wrong in one way. This module
says what that decision is worth to a coverage report: which AAC obligations
the rule evidences (`EVIDENCE`, read from each rule's pattern in AAC's
`patterns/` and cut to what this shape owes), how its check decides (M1 or M5),
and — on a verdict — whether the score's value is the verdict or not.

Split from the rules so the rules stay a list of checks, and because the table
here is a reading of the catalog that changes when the catalog does, which the
checks do not. The verdict's shape is the harness's (`agent_harness.watch.verdicts`),
re-exported here; the table is this agent's.
"""

from __future__ import annotations

from agent_harness.watch.verdicts import Evidence, Finding, Severity, Verdict

EVIDENCE: dict[str, Evidence] = {
    # Read from each rule's pattern (AAC patterns/*.yaml, `caught_by`), cut to
    # the obligations this subject owes as A6 and that the rule's check actually
    # bears on. Narrowest honest set: a finding marks every obligation named
    # here as failing, so an obligation the rule does not decide is left out.
    #
    # The mechanism is how the check decides: M1 for a deterministic assertion
    # over the captured words (a pattern on the reply or the input), M5 for an
    # assertion over the span record alone. No rule here is model-graded; one
    # that is says M3, and owes what a judge owes (AAC-0084..0086, 0090).
    "W-01": Evidence(("AAC-0029",), "M1"),
    "W-02": Evidence(("AAC-0110",), "M1"),
    "W-03": Evidence(("AAC-0006",), "M5"),
    # A run stopped by a limit is the limit working (AAC-0055) and its pattern's
    # other obligation is A9's; the rule's finding is a ticket to read, not a
    # verdict on either.
    "W-04": Evidence((), "M5"),
    "W-05": Evidence(("AAC-0053",), "M5"),
    "W-06": Evidence(("AAC-0053",), "M5"),
    "W-07": Evidence(("AAC-0007",), "M5"),
    "W-08": Evidence(("AAC-0008",), "M5"),
    "W-09": Evidence(("AAC-0112",), "M1"),
    "W-10": Evidence(("AAC-0112",), "M5"),
    # AAC-0020 is A1's and AAC-0088 A10's: owed by neither shape here.
    "W-11": Evidence((), "M1"),
    "W-12": Evidence(("AAC-0002",), "M5"),
    "W-13": Evidence(("AAC-0053",), "M1"),
    "W-14": Evidence(("AAC-0113",), "M1"),
    "W-15": Evidence(("AAC-0052",), "M1"),
    "W-16": Evidence(("AAC-0029",), "M1"),
    "W-17": Evidence(("AAC-0002",), "M1"),
    # AAC-0038 is A4's.
    "W-18": Evidence((), "M1"),
    "W-19": Evidence(("AAC-0058",), "M1"),
    "W-20": Evidence(("AAC-0105",), "M5"),
    "W-21": Evidence(("AAC-0012",), "M5"),
    # The conversation rules' obligations (AAC-0037, 0039, 0042) are A4's.
    "C-01": Evidence((), "M1"),
    "C-02": Evidence((), "M1"),
    "C-03": Evidence(("AAC-0103",), "M5"),
}
"""What each rule's verdict is evidence of, for an AAC coverage report."""

BEYOND_THE_PATTERN: dict[tuple[str, str], str] = {
    ("W-01", "AAC-0029"): (
        "a status the reply states that the store's result contradicts is an unsupported"
        " claim (AAC-0029, since 30025b3: a tool's results are the context, and a stated"
        " status is a claim). AACP-0028 lists AAC-0110, which is about claimed actions,"
        " and AAC-0113, about a superseded read: neither is what W-01 decides"
    ),
    ("W-16", "AAC-0029"): (
        "an order id the reply names that no result returned is AAC-0029's identifier"
        " clause. AACP-0030 lists AAC-0110 and AAC-0024; the second is A2's"
    ),
}
"""Obligations a rule evidences that its pattern does not list, and why — each
one a pattern the catalog could widen (REVIEW R-020)."""


__all__ = ["BEYOND_THE_PATTERN", "EVIDENCE", "Evidence", "Finding", "Severity", "Verdict"]

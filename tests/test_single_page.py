"""The nine parts survive being put in one document.

Merging nine separately-designed pages is not concatenation: each brought its
own palette, its own `:root`, its own element ids and its own top-level script.
Every test here stands for a way the merge can look finished and be broken —
the page renders, nothing throws, and something is silently wrong.

The CSS one is not hypothetical. Comments sitting in front of a rule are part
of its prelude, and the prose in them contains commas; splitting the selector
list on those commas tore a comment in half, left its `*/` in the middle of a
selector, and the browser discarded the whole rule. Every part with a comment
above its token block lost its light-mode colours that way, and the page still
rendered — on the tokens of whichever part happened to come before it.
"""

from __future__ import annotations

import collections
import re

import pytest
from scripts.single_page import build, scope_css

pytestmark = pytest.mark.tooling

# In reading order, which is the order build() assembles them in.
PARTS = ("now", "tutorial", "agent", "review", "flow", "escalation", "context", "runs", "stacks")
FAST = [p for p in PARTS if p != "runs"]


@pytest.fixture(scope="module")
def page() -> str:
    # --fast: the scenario runs are part three's content, not its assembly, and
    # running 29 of them to check that ids are unique is a slow way to do it.
    return build(fast=True)


def _style(page: str) -> str:
    return re.search(r"<style>(.*?)</style>", page, re.S).group(1)


def test_every_part_is_present(page: str) -> None:
    found = re.findall(r'class="part part-(\w+)"', page)
    assert found == FAST


def test_no_two_elements_claim_the_same_anchor(page: str) -> None:
    ids = re.findall(r'\bid="([^"]+)"', page)
    repeated = {k: v for k, v in collections.Counter(ids).items() if v > 1}
    assert repeated == {}


def test_every_link_in_the_page_lands_somewhere(page: str) -> None:
    ids = set(re.findall(r'\bid="([^"]+)"', page))
    targets = set(re.findall(r'href="#([^"]+)"', page))
    assert targets - ids == set()


def test_the_contents_list_reaches_every_part(page: str) -> None:
    contents = re.search(r'<div class="contents">(.*?)</div>\n', page, re.S).group(1)
    lists = re.findall(r"<ol>(.*?)</ol>", contents, re.S)
    assert len(lists) == len(FAST)
    assert all(re.findall(r'<a href="#', block) for block in lists)


@pytest.mark.parametrize("part", FAST)
def test_a_part_keeps_its_own_tokens(page: str, part: str) -> None:
    """Light and both dark states, defined on the part rather than the document.

    A part whose `:root` block was dropped still renders — it inherits whatever
    the part before it set — which is exactly why this is asserted rather than
    looked at.
    """
    css = re.sub(r"/\*.*?\*/", " ", _style(page), flags=re.S)
    assert re.search(rf"(?:^|\}})\s*\.part-{part}\s*\{{[^}}]*--", css, re.S), "light"
    assert re.search(rf'\[data-theme="dark"\]\s*\.part-{part}\s*\{{[^}}]*--', css), "toggle"
    assert f':not([data-theme="light"]) .part-{part}' in css, "system preference"


def test_no_part_styles_the_document(page: str) -> None:
    """Every rule a part contributed is confined to that part's subtree."""
    css = _style(page)
    contributed = re.sub(r"/\*.*?\*/", " ", css[css.find("/* ==== ") :], flags=re.S)
    loose = [
        sel.strip()
        for sel in re.findall(r"(?:^|\}|\{)\s*([^@{}]{1,200}?)\{", contributed)
        if ".part-" not in sel
    ]
    assert loose == []


def test_a_comment_before_a_rule_does_not_eat_the_selector() -> None:
    """The bug above, reduced: the comma lives inside a comment, not a selector."""
    out = scope_css("/* one, two */ :root{--x:1}", ".part-x")
    assert ".part-x{--x:1}" in out.replace(" ", "")
    assert "*/ :root" not in out


def test_scoping_keeps_the_theme_switch_reaching_in() -> None:
    out = scope_css(':root[data-theme="dark"]{--x:2}', ".part-x")
    assert ':root[data-theme="dark"] .part-x' in out


def test_each_script_is_closed_over(page: str) -> None:
    """Two parts declare consts at the top level; without a closure the second
    to load throws on a duplicate declaration and takes its part with it."""
    body = re.search(r"<script>(.*)</script>", page, re.S).group(1)
    for part in ("agent", "stacks"):
        marker = f"/* ---- {part} ---- */"
        assert marker in body
        assert body[body.index(marker) :].lstrip().splitlines()[1] == "(function(){"

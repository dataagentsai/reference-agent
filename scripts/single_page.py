"""One page: the agent, the twin that tests it, the review, and the stack behind it.

There were nine HTML files. Four of them mattered and two of those were generated
from the code, so a hand-merge would have frozen every measured number at the
value it held on the day of the merge — the precise failure `review_view.py` was
written to end. This assembles instead.

Parts, one file, in the order a reader needs them. First, **the agent now**
(`docs/parts/now.source.html`): the flow and the stack after Tier 1, which the
older parts predate. Second, **the agent harness tutorial**, read from
`agent-harness-tutorial/index.html` itself rather than vendored, so the page
has every question as it stands. Then:

1. **The agent** — what it is, end to end. Authored prose, vendored at
   `docs/parts/agent.source.html`.
2. **The architecture** — read off the code at generation time by
   `scripts/review_view.py`. Every number here is measured, never remembered.
3. **The twin** — every scenario in `scenarios/` run against the agent by
   `scripts/run_view.py`, and what each one asserted.
4. **The stack** — fifteen agents, fifteen stacks, and what changes under a
   cloud. Authored prose, vendored at `docs/parts/stacks.source.html`.

**Why the parts keep their own styling.** Each of the four was designed on its
own terms, and flattening four palettes into one house style would be a large
hand-edit that improves nothing a reader can see. Instead every part's CSS is
scoped to its own subtree: `:root` becomes the part's element, so its custom
properties land on the part rather than the document, and every other selector
is prefixed. Four designs survive intact inside one document. Element ids that
collide across parts are renamed, in the markup and in the scripts that read
them, and a collision that cannot be renamed is a build failure rather than a
silently broken page.

    uv run python scripts/single_page.py            # the whole page
    uv run python scripts/single_page.py --fast     # skip the scenario runs
"""

from __future__ import annotations

import argparse
import asyncio
import html
import pathlib
import re
import sys
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEST = ROOT / "docs" / "SUPPORT-AGENT.html"
PARTS = ROOT / "docs" / "parts"

e = html.escape


# ---------------------------------------------------------------- CSS scoping


def _css_items(css: str) -> list[tuple]:
    """Split CSS into top-level items, respecting strings, comments and nesting.

    Returns ``("stmt", text)`` for anything ending in a semicolon (`@import`,
    `@charset`) and ``("rule", prelude, body)`` for anything with a block.
    """
    items: list[tuple] = []
    buf = ""
    i, n = 0, len(css)
    while i < n:
        c = css[i]
        if css.startswith("/*", i):
            j = css.find("*/", i + 2)
            j = n if j < 0 else j + 2
            buf += css[i:j]
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and css[j] != c:
                j += 2 if css[j] == "\\" else 1
            buf += css[i : j + 1]
            i = j + 1
            continue
        if c == ";":
            if buf.strip():
                items.append(("stmt", buf + c))
            buf = ""
            i += 1
            continue
        if c == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                if css.startswith("/*", j):
                    k = css.find("*/", j + 2)
                    j = n if k < 0 else k + 2
                    continue
                d = css[j]
                if d in "\"'":
                    k = j + 1
                    while k < n and css[k] != d:
                        k += 2 if css[k] == "\\" else 1
                    j = k + 1
                    continue
                if d == "{":
                    depth += 1
                elif d == "}":
                    depth -= 1
                j += 1
            items.append(("rule", buf, css[i + 1 : j - 1]))
            buf = ""
            i = j
            continue
        buf += c
        i += 1
    if buf.strip():
        items.append(("stmt", buf))
    return items


def _split_selectors(prelude: str) -> list[str]:
    """Split a selector list on commas that are not inside brackets or strings."""
    out, buf, depth = [], "", 0
    i, n = 0, len(prelude)
    while i < n:
        c = prelude[i]
        if c in "\"'":
            j = i + 1
            while j < n and prelude[j] != c:
                j += 2 if prelude[j] == "\\" else 1
            buf += prelude[i : j + 1]
            i = j + 1
            continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if c == "," and depth == 0:
            out.append(buf)
            buf = ""
            i += 1
            continue
        buf += c
        i += 1
    out.append(buf)
    return [s for s in (x.strip() for x in out) if s]


# ":root", "html" and "body" all name the page itself. Inside a part they must
# name the part's own element, or the part's tokens would leak to the document
# and the last part loaded would win every variable.
_ROOT_HEAD = re.compile(
    r"^(:root|html|body)((?:\[[^\]]*\]|:not\([^)]*\)|::?[-\w]+(?:\([^)]*\))?|\.[-\w]+|#[-\w]+)*)"
    r"(\s+.*)?$"
)

_NESTED_AT = re.compile(r"^\s*@(media|supports|container|layer|scope)\b", re.I)
_OPAQUE_AT = re.compile(
    r"^\s*@(keyframes|-webkit-keyframes|font-face|page|property|counter-style|font-feature-values)\b",
    re.I,
)


def _scope_selector(sel: str, scope: str) -> str:
    sel = sel.strip()
    if not sel:
        return sel
    if sel.startswith("@"):
        return sel
    if sel in (":root", "html", "body"):
        return scope
    if sel == "*":
        # The part's own element is styled too, or box-sizing would miss it.
        return f"{scope}, {scope} *"
    m = _ROOT_HEAD.match(sel)
    if m:
        compound, rest = m.group(2) or "", (m.group(3) or "").strip()
        if compound:
            # ":root[data-theme=dark]" keeps naming the document, but what it
            # sets now lands on the part: the theme switch still reaches in.
            anchor = f"{m.group(1)}{compound}"
            return f"{anchor} {scope} {rest}".rstrip()
        return f"{scope} {rest}".rstrip() if rest else scope
    return f"{scope} {sel}"


def scope_css(css: str, scope: str) -> str:
    """Confine a stylesheet to one subtree, preserving what it actually says."""
    out: list[str] = []
    for item in _css_items(css):
        if item[0] == "stmt":
            text = item[1].strip()
            # @import and @charset are document-level and cannot be scoped;
            # the shell hoists font links instead, so drop them here.
            if not text.lower().startswith(("@import", "@charset")):
                out.append(text)
            continue
        raw, body = item[1], item[2]
        # A comment sitting in front of a rule is part of the prelude, and the
        # prose in it contains commas. Split on those and the comment is torn
        # in two, its `*/` lands mid-selector, and the browser throws the whole
        # rule away — which is how a part quietly lost its light-mode tokens.
        kept = "".join(re.findall(r"/\*.*?\*/", raw, re.S))
        prelude = re.sub(r"/\*.*?\*/", " ", raw, flags=re.S).strip()
        if kept:
            out.append(kept)
        if not prelude:
            continue
        if _OPAQUE_AT.match(prelude):
            # Keyframes and font-faces have no selectors to scope. Their names
            # are global, so the caller namespaces them before we get here.
            out.append(f"{prelude.strip()}{{{body}}}")
            continue
        if _NESTED_AT.match(prelude):
            out.append(f"{prelude.strip()}{{{scope_css(body, scope)}}}")
            continue
        sels = ", ".join(_scope_selector(s, scope) for s in _split_selectors(prelude))
        out.append(f"{sels}{{{body}}}")
    return "\n".join(out)


def namespace_keyframes(css: str, js: str, prefix: str) -> tuple[str, str]:
    """Animation names are global. Give each part its own, in CSS and in JS."""
    names = set(re.findall(r"@(?:-webkit-)?keyframes\s+([-\w]+)", css))
    for name in sorted(names, key=len, reverse=True):
        tag = f"{prefix}-{name}"
        css = re.sub(rf"(@(?:-webkit-)?keyframes\s+){re.escape(name)}\b", rf"\g<1>{tag}", css)
        css = re.sub(
            rf"(animation(?:-name)?\s*:[^;}}]*?)\b{re.escape(name)}\b",
            rf"\g<1>{tag}",
            css,
        )
        js = re.sub(rf"(['\"]){re.escape(name)}\1", rf"\g<1>{tag}\g<1>", js)
    return css, js


# ------------------------------------------------------------- part extraction

_DROP = re.compile(r"<!doctype[^>]*>|</?html[^>]*>|</?head[^>]*>|</?body[^>]*>|<meta[^>]*>", re.I)

# What a reader can jump to: a prose section, or one scenario's collapsed run.
_LANDMARK = re.compile(r'<(section|details|h2)\b[^>]*\bid="([^"]+)"[^>]*>', re.I)
_HEADING = re.compile(r"<h2[^>]*>(.*?)</h2>|<summary[^>]*>(.*?)</summary>", re.S | re.I)


class Part:
    """One source page, taken apart so it can be put back inside another."""

    def __init__(self, key: str, title: str, standfirst: str, source: str):
        self.key = key
        self.title = title
        self.standfirst = standfirst
        self.scope = f".part-{key}"

        self.fonts = re.findall(r'<link\b[^>]*rel="stylesheet"[^>]*>', source, re.I)
        self.css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", source, re.S))
        self.js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", source, re.S))

        body = re.sub(r"<style[^>]*>.*?</style>", "", source, flags=re.S)
        body = re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.S)
        body = re.sub(r"<link\b[^>]*>", "", body, flags=re.I)
        body = re.sub(r"<title[^>]*>.*?</title>", "", body, flags=re.S | re.I)
        self.body = _DROP.sub("", body).strip()
        self._mint_ids()

        self.ids = re.findall(r'\bid="([^"]+)"', self.body)

    def _mint_ids(self) -> None:
        """Give every section an anchor, for the parts that never had one.

        Three of the sources were written as single pages with no contents
        list, so their sections carry no id. Nothing can link to a section
        that has no name — not the contents list here, and not a reader
        sending someone else a link to one part of the argument.
        """
        out, at = [], 0
        for m in re.finditer(r"<section\b([^>]*)>", self.body):
            out.append(self.body[at : m.start()])
            at = m.end()
            if "id=" in m.group(1):
                out.append(m.group(0))
                continue
            head = _HEADING.search(self.body, m.end())
            raw = (head.group(1) or head.group(2) or "") if head else ""
            text = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
            # Headings here open with their own number — "01 When" — which is
            # position, not name, and position is the thing most likely to move.
            text = re.sub(r"^\d+\s*", "", text).lower()
            slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text)).strip("-")
            out.append(f'<section id="{self.key}-{slug or len(out)}"{m.group(1)}>')
        out.append(self.body[at:])
        self.body = "".join(out)

    # -- ids ---------------------------------------------------------------

    def rename_id(self, old: str, new: str) -> None:
        """Rename one element id everywhere it is referred to."""
        before = self.body
        self.body = re.sub(rf'\bid="{re.escape(old)}"', f'id="{new}"', self.body)
        self.body = re.sub(rf'href="#{re.escape(old)}"', f'href="#{new}"', self.body)
        self.body = re.sub(
            rf'\b(aria-controls|aria-labelledby|aria-describedby|for)="{re.escape(old)}"',
            rf'\g<1>="{new}"',
            self.body,
        )
        if self.body == before:
            raise SystemExit(f"single_page: id {old!r} in {self.key} could not be renamed")
        # Scripts address ids as string literals; a rename that misses them
        # leaves a page that looks right and does nothing.
        self.js = re.sub(rf"(['\"]){re.escape(old)}\1", rf"\g<1>{new}\1", self.js)
        self.js = re.sub(rf"(['\"])#{re.escape(old)}\1", rf"\g<1>#{new}\1", self.js)
        self.ids = [new if i == old else i for i in self.ids]

    # -- assembly ----------------------------------------------------------

    def styled(self) -> str:
        css, self.js = namespace_keyframes(self.css, self.js, self.key)
        return scope_css(css, self.scope)

    def script(self) -> str:
        if not self.js.strip():
            return ""
        # Two of the four parts declare consts at the top level. Without a
        # closure the second one to load throws and takes its part with it.
        return f"\n/* ---- {self.key} ---- */\n(function(){{\n{self.js}\n}})();\n"

    def entries(self) -> list[tuple[str, str]]:
        """(id, heading) for everything a reader would want to jump to.

        The four parts name their landmarks differently — a section whose
        heading is wrapped in a numbering div, a section whose h2 is a direct
        child, and one collapsed `details` per scenario. Anchor on whatever
        carries the id, then take the first heading inside it — or, for a part
        written as flat headings with no sections, the heading itself.
        """
        out = []
        marks = list(_LANDMARK.finditer(self.body))
        for k, m in enumerate(marks):
            end = marks[k + 1].start() if k + 1 < len(marks) else len(self.body)
            start = m.start() if m.group(1).lower() == "h2" else m.end()
            head = _HEADING.search(self.body, start, end)
            if not head:
                continue
            raw = head.group(1) or head.group(2) or ""
            text = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
            if text:
                out.append((m.group(2), text))
        return out


# ------------------------------------------------------------------- sources


def authored(key: str, title: str, standfirst: str, path: pathlib.Path | None = None) -> Part:
    """A hand-written part. Vendored under `docs/parts/` unless `path` names the
    file it is written in, which is read as it stands on every build."""
    path = path or PARTS / f"{key}.source.html"
    if not path.exists():
        raise SystemExit(f"single_page: missing authored source {path}")
    return Part(key, title, standfirst, path.read_text(encoding="utf-8"))


def review_part() -> Part:
    """The architecture review, regenerated now rather than remembered."""
    from scripts.review_html import render
    from scripts.review_view import gather

    return Part(
        "review",
        "The architecture, read off the code",
        "Measured when this page was generated — module sizes, ports, the rules at "
        "each position, which context handlers exist. Only the explanations are written.",
        render(gather()),
    )


def runs_part(fast: bool) -> Part | None:
    """Every scenario in scenarios/, run against the agent."""
    if fast:
        return None
    from scripts.run_view import capture
    from scripts.run_view_html import render

    names = sorted(p.stem for p in (ROOT / "scenarios").glob("*.yaml"))

    async def go():
        runs = []
        for name in names:
            try:
                runs.append(await capture(name, False))
            except Exception as exc:  # noqa: BLE001 — one bad scenario keeps the rest
                print(f"  skipped {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return runs

    runs = asyncio.run(go())
    if not runs:
        return None
    return Part(
        "runs",
        "The twin, and what it found",
        f"{len(runs)} scenarios run against the agent, turn by turn: what the person "
        "typed, which route it took, what the model was asked, what changed in the "
        "world, and what the scenario asserted.",
        render(runs, live_model=False),
    )


# --------------------------------------------------------------------- shell

SHELL = """
:root{--s-ink:#16181d;--s-dim:#646b79;--s-rule:#e2e5ea;--s-bg:#fbfaf8;--s-card:#fff;
 --s-accent:#2f5d50;--s-shade:#f3f4f7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
 --s-ink:#e6e8ec;--s-dim:#9aa2b1;--s-rule:#2a2e36;--s-bg:#111318;--s-card:#181b21;
 --s-accent:#6fc0a6;--s-shade:#20242b}}
:root[data-theme="dark"]{--s-ink:#e6e8ec;--s-dim:#9aa2b1;--s-rule:#2a2e36;
 --s-bg:#111318;--s-card:#181b21;--s-accent:#6fc0a6;--s-shade:#20242b}
html{scroll-behavior:smooth}
body{margin:0;background:var(--s-bg);color:var(--s-ink);
 font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.shell{max-width:980px;margin:0 auto;padding:64px 28px 40px}
.shell .eyebrow{font:600 11px/1 ui-monospace,monospace;letter-spacing:.14em;
 text-transform:uppercase;color:var(--s-accent);margin:0 0 16px}
.shell h1{font:600 40px/1.1 "Iowan Old Style",Palatino,Georgia,serif;margin:0 0 14px;
 letter-spacing:-.02em;text-wrap:balance}
.shell .lede{color:var(--s-dim);font-size:17px;max-width:64ch;margin:0 0 8px}
.contents{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));
 gap:22px;margin:44px 0 0;padding:26px 0 0;border-top:1px solid var(--s-rule)}
.contents section{min-width:0}
.contents h2{font:600 10px/1 ui-monospace,monospace;letter-spacing:.12em;
 text-transform:uppercase;color:var(--s-accent);margin:0 0 11px}
.contents ol{margin:0;padding:0 0 0 2px;list-style:none;
 font-size:13.5px;line-height:1.45}
.contents li{margin:0 0 7px;display:flex;gap:8px}
.contents li span{font:600 10px/1.5 ui-monospace,monospace;color:var(--s-dim);flex:none}
.contents a{color:var(--s-ink);text-decoration:none;border-bottom:1px solid transparent}
.contents a:hover{border-bottom-color:var(--s-accent);color:var(--s-accent)}
.partbar{position:sticky;top:0;z-index:40;background:var(--s-bg);
 border-bottom:1px solid var(--s-rule);padding:9px 28px;
 display:flex;gap:7px;align-items:center;flex-wrap:wrap;
 font:600 11px/1 ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase}
.partbar a{color:var(--s-dim);text-decoration:none;padding:6px 9px;border-radius:5px}
.partbar a:hover{background:var(--s-shade);color:var(--s-ink)}
.partbar .sp{flex:1}
.partbar button{font:inherit;letter-spacing:inherit;text-transform:inherit;
 background:var(--s-shade);color:var(--s-dim);border:1px solid var(--s-rule);
 border-radius:5px;padding:6px 10px;cursor:pointer}
.part{border-top:1px solid var(--s-rule)}
.partmark{max-width:980px;margin:0 auto;padding:56px 28px 0}
.partmark .n{font:600 11px/1 ui-monospace,monospace;letter-spacing:.14em;
 text-transform:uppercase;color:var(--s-accent)}
.partmark h2{font:600 30px/1.15 "Iowan Old Style",Palatino,Georgia,serif;
 margin:12px 0 10px;letter-spacing:-.015em;color:var(--s-ink);text-wrap:balance}
.partmark p{color:var(--s-dim);max-width:62ch;margin:0;font-size:15.5px}
.shell-foot{max-width:980px;margin:0 auto;padding:40px 28px 90px;
 border-top:1px solid var(--s-rule);color:var(--s-dim);font-size:13px}
.shell-foot code{font-family:ui-monospace,monospace}
@media (max-width:560px){.shell{padding:40px 18px 30px}.shell h1{font-size:31px}
 .partmark{padding:38px 18px 0}.partbar{padding:8px 14px}.shell-foot{padding:30px 18px 70px}}
"""

TOGGLE = """
(function(){
  var b=document.getElementById('shell-theme'); if(!b) return;
  function cur(){var t=document.documentElement.getAttribute('data-theme');
    if(t) return t;
    return matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light';}
  function paint(){b.textContent = cur()==='dark' ? 'Light' : 'Dark';}
  b.addEventListener('click',function(){
    document.documentElement.setAttribute('data-theme', cur()==='dark'?'light':'dark');
    paint();
  });
  paint();
})();
"""


BEFORE_TIER_1 = (
    " Written on 13 September, before Keycloak, Chatwoot, Temporal, Saleor and "
    "Langfuse: where it describes identity, approvals, escalation stores or the "
    "shop, the first part is what is true now."
)
"""Said on every part written before the agent moved onto real products, so a
reader who arrives at one of them knows which details the first part replaces."""


def build(fast: bool) -> str:
    parts = [
        authored(
            "now",
            "The agent now: the flow and the stack",
            "After Tier 1. What changed, the path a message takes through the real "
            "products, three messages step by step, the stack, and where it sits "
            "in the goal.",
        ),
        authored(
            "tutorial",
            "Agent harness tutorial",
            "What an agent harness is, what goes in one, and how to build it, one "
            "question at a time — from the kinds of AI application down to this "
            "agent's function stack.",
            path=ROOT / "agent-harness-tutorial" / "index.html",
        ),
        authored(
            "agent",
            "The agent, end to end",
            "What it is, what it can do, what happens when a message arrives, and "
            "every piece of the code in the order a message meets them." + BEFORE_TIER_1,
        ),
        review_part(),
        authored(
            "flow",
            "Escalation, as it works now",
            "What escalation costs, the path a turn takes, what triggers it, why "
            "the order of those triggers matters, and what it emits." + BEFORE_TIER_1,
        ),
        authored(
            "escalation",
            "Escalation, rebuilt",
            "Queue or no queue, when to escalate and in which tier, over- and "
            "under-escalation, the data model, and what happens when nobody is free."
            + BEFORE_TIER_1,
        ),
        authored(
            "context",
            "The context budget",
            "When context has to be handled, the seven handlers, the kinds of "
            "compaction, which one a given situation calls for, and what to build first.",
        ),
        runs_part(fast),
        authored(
            "stacks",
            "The stack it was chosen from",
            "Fifteen agents, fifteen stacks. The four frameworks, what each is really "
            "selling, when to use none of them, and what changes under a cloud.",
        ),
    ]
    parts = [p for p in parts if p is not None]

    # A part that repeats an id is already a broken page: the anchor and the
    # script disagree about which element they mean, and renaming would only
    # hide it. Say so and stop, rather than emit a document with the defect
    # copied into it.
    for part in parts:
        repeated = sorted({i for i in part.ids if part.ids.count(i) > 1})
        if repeated:
            raise SystemExit(
                f"single_page: {part.key} repeats id(s) {', '.join(repeated)} — "
                f"two elements claim the same anchor. Fix the source, not this script."
            )

    # Ids must also be unique across the whole document. Rename the later
    # part's, so the earlier part's anchors keep the names a reader may have
    # bookmarked.
    seen: dict[str, str] = {}
    for part in parts:
        for ident in list(part.ids):
            if ident in seen:
                part.rename_id(ident, f"{part.key}-{ident}")
            else:
                seen[ident] = part.key

    css = "\n".join(f"/* ==== {p.key} ==== */\n{p.styled()}" for p in parts)
    js = "".join(p.script() for p in parts)

    fonts, fseen = [], set()
    for p in parts:
        for link in p.fonts:
            if link not in fseen:
                fseen.add(link)
                fonts.append(link)

    contents = "".join(
        f"<section><h2>{i + 1:02d} · {e(p.title)}</h2><ol>"
        + "".join(
            f'<li><span>{j + 1:02d}</span><a href="#{e(pid)}">{e(text)}</a></li>'
            for j, (pid, text) in enumerate(p.entries())
        )
        + "</ol></section>"
        for i, p in enumerate(parts)
    )

    bar = "".join(f'<a href="#part-{p.key}">{e(p.title.split(",")[0])}</a>' for p in parts)

    body = "".join(
        f'<div class="part part-{p.key}" id="part-{p.key}">'
        f'<div class="partmark"><div class="n">Part {i + 1:02d}</div>'
        f"<h2>{e(p.title)}</h2><p>{e(p.standfirst)}</p></div>"
        f"{p.body}</div>"
        for i, p in enumerate(parts)
    )

    made = datetime.now(UTC).strftime("%d %B %Y, %H:%M UTC")
    counts = " · ".join(f"{len(p.entries())} sections in {p.key}" for p in parts)

    return f"""<title>The Support Agent</title>
{"".join(fonts)}
<style>{SHELL}
{css}</style>
<nav class="partbar">{bar}<span class="sp"></span>
<button id="shell-theme" type="button">Dark</button></nav>
<div class="shell">
<p class="eyebrow">One page · generated {e(made)}</p>
<h1>The support agent, its twin, and the stack behind it</h1>
<p class="lede">Four documents that were four files. What the agent is, how the
code is actually held together, every scenario the twin runs against it, and the
stack it was chosen from.</p>
<p class="lede">Parts two and three are measured, not remembered: every number,
path, rule name and scenario verdict in them was read off the code when this page
was generated. Parts one and four are authored prose, kept as written.</p>
<div class="contents">{contents}</div>
</div>
{body}
<footer class="shell-foot">Generated by <code>scripts/single_page.py</code> ·
{e(counts)} · never hand-edited</footer>
<script>{TOGGLE}{js}</script>"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the one page.")
    ap.add_argument("--fast", action="store_true", help="skip the scenario runs (part three)")
    args = ap.parse_args()
    page = build(args.fast)
    DEST.write_text(page, encoding="utf-8")
    print(f"{DEST}  {len(page):,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

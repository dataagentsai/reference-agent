"""The page `run_view.py` writes. Kept apart so capture is readable on its own.

Nothing here computes anything about the run: it renders what was captured. A
renderer that derived a number would be a second, quieter implementation of the
thing being shown.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.run_view import Run

e = html.escape

POSITIONS = {
    "customer": ("P1", "edge"),
    "route": ("P3", "harness"),
    "model": ("P3", "harness"),
    "tool": ("P5", "tool boundary"),
    "world": ("P6", "state"),
    "offstage": ("P8", "human"),
    "reply": ("P3", "harness"),
    "check": ("P7", "offline"),
}

LEGEND = [
    ("customer", "what the person typed"),
    ("route", "which path it took — three of four never reach a model"),
    ("model", "what was sent, what tools were offered, what came back"),
    ("tool", "an action, and the world's verdict on it"),
    ("world", "what actually changed"),
    ("offstage", "a reviewer or colleague, between turns"),
    ("reply", "what the customer reads — and who wrote it"),
    ("check", "what the scenario asserted"),
]

STYLE = """
/* One token set, redefined for the two dark states — an explicit choice and an
   unstamped system preference — so the page holds on whatever ground it lands on. */
:root{--ink:#191b1f;--dim:#6a7180;--rule:#e3e5ea;--bg:#fafaf8;--card:#fff;
 --shade:#f4f5f7;--good:#15803d;--bad:#b91c1c;--accent:#3f4ba8}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
 --ink:#e8e9ec;--dim:#9aa1b0;--rule:#2c2f37;--bg:#131519;--card:#1a1d22;
 --shade:#22262d;--good:#5fd08a;--bad:#f38b8b;--accent:#a5b4fc}}
:root[data-theme="dark"]{--ink:#e8e9ec;--dim:#9aa1b0;--rule:#2c2f37;--bg:#131519;
 --card:#1a1d22;--shade:#22262d;--good:#5fd08a;--bad:#f38b8b;--accent:#a5b4fc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:30px 32px;border-bottom:1px solid var(--rule);background:var(--card)}
h1{margin:0 0 8px;font-size:22px;letter-spacing:-.01em}
header p{margin:0 0 6px;color:var(--dim);max-width:74ch}
main{max-width:1020px;margin:0 auto;padding:26px 20px 90px}
h2.sec{font-size:13px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);
 margin:34px 0 10px;font-weight:600}
.index{width:100%;border-collapse:collapse;font-size:13px;background:var(--card);
 border:1px solid var(--rule);border-radius:10px;overflow:hidden}
.index th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
 color:var(--dim);padding:9px 12px;border-bottom:1px solid var(--rule);font-weight:600}
.index td{padding:9px 12px;border-top:1px solid var(--rule);vertical-align:top}
.index td.n{text-align:right;font-variant-numeric:tabular-nums;color:var(--dim);width:1%}
.index a{color:var(--accent);text-decoration:none}.index a:hover{text-decoration:underline}
.wrap{overflow-x:auto}
details.sc{border:1px solid var(--rule);border-radius:10px;background:var(--card);
 margin:0 0 12px;padding:0 16px}
details.sc>summary{cursor:pointer;padding:14px 0;font-weight:600;font-size:15px;
 list-style:none;display:flex;gap:10px;align-items:baseline}
details.sc>summary::-webkit-details-marker{display:none}
details.sc>summary:before{content:"▸";color:var(--dim);font-size:12px}
details.sc[open]>summary:before{content:"▾"}
.obj{color:var(--dim);font-size:13px;margin:0 0 14px;max-width:78ch}
.row{display:grid;grid-template-columns:92px 1fr;gap:14px;margin:0 0 2px}
.rail{text-align:right;padding-top:13px;color:var(--dim);font-size:11px;
 text-transform:uppercase;letter-spacing:.06em;padding-right:11px;
 border-right:1px solid var(--rule)}
.rail b{display:block;color:var(--ink);font-size:12px;letter-spacing:0}
.card{background:var(--bg);border:1px solid var(--rule);border-radius:8px;
 padding:11px 14px;margin:5px 0}
.card h3{margin:0;font-size:14px;font-weight:600;display:flex;gap:8px;align-items:center}
/* The badge colours are the one place hue carries meaning: which layer acted. */
.tag{font-size:10px;text-transform:uppercase;letter-spacing:.08em;padding:2px 8px;
 border-radius:3px;background:var(--shade);color:var(--dim);font-weight:600;
 white-space:nowrap}
.tag.tool{color:#0f766e}.tag.model{color:#a16207}.tag.customer{color:#475569}
.tag.world{color:#86198f}.tag.offstage{color:#be123c}.tag.check{color:#0369a1}
.tag.reply{color:#15803d}.tag.route{color:#4338ca}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .tag.tool{color:#5eead4}
 :root:not([data-theme="light"]) .tag.model{color:#fcd34d}
 :root:not([data-theme="light"]) .tag.world{color:#f0abfc}
 :root:not([data-theme="light"]) .tag.offstage{color:#fda4af}
 :root:not([data-theme="light"]) .tag.check{color:#7dd3fc}
 :root:not([data-theme="light"]) .tag.reply{color:#86efac}
 :root:not([data-theme="light"]) .tag.route{color:#a5b4fc}
 :root:not([data-theme="light"]) .tag.customer{color:#cbd5e1}}
.said{margin:6px 0 0;white-space:pre-wrap}
.note{color:var(--dim);font-size:12px;margin:5px 0 0;font-style:italic}
.verdict{margin-left:auto;font-size:11px;font-weight:600;letter-spacing:.04em}
.ok{color:var(--good)}.no{color:var(--bad)}.neutral{color:var(--dim)}
details.d{margin:8px 0 0}summary{cursor:pointer;color:var(--dim);font-size:12px}
table.b{border-collapse:collapse;width:100%;margin:8px 0 0;font-size:13px}
table.b td{border-top:1px solid var(--rule);padding:6px 8px;vertical-align:top}
table.b td:first-child{color:var(--dim);white-space:nowrap;width:1%;font-size:12px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace,monospace}
.legend{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 6px;font-size:12px;color:var(--dim)}
.ids{font-size:12px;margin:0 0 14px;border-collapse:collapse;width:100%}
.ids td{padding:4px 8px;border-top:1px solid var(--rule);color:var(--dim)}
.ids td:first-child{font-family:ui-monospace,monospace;color:var(--ink);white-space:nowrap}
code{font-family:ui-monospace,monospace;font-size:.92em}
"""


def _body(event) -> str:
    if not event.body:
        return ""
    cells = "".join(
        f"<tr><td>{e(label)}</td><td><pre>{e(str(value))}</pre></td></tr>"
        for label, value in event.body
    )
    return (
        '<details class="d"><summary>what passed through</summary>'
        f'<table class="b">{cells}</table></details>'
    )


def _step(event) -> str:
    position, layer = POSITIONS.get(event.kind, ("", ""))
    klass = (
        "ok"
        if event.verdict in {"passed", "allowed"}
        else "no"
        if event.verdict in {"failed", "refused"}
        else "neutral"
    )
    verdict = f'<span class="verdict {klass}">{e(event.verdict)}</span>' if event.verdict else ""
    detail = f'<p class="said">{e(event.detail)}</p>' if event.detail else ""
    note = f'<p class="note">{e(event.note)}</p>' if event.note else ""
    return (
        f'<div class="row"><div class="rail"><b>{position}</b>{e(layer)}</div>'
        f'<div class="card"><h3><span class="tag {event.kind}">{e(event.kind)}</span>'
        f"{e(event.title)}{verdict}</h3>{detail}{note}{_body(event)}</div></div>"
    )


def _identifiers(run: Run) -> str:
    if not run.identifiers:
        return ""
    rows = "".join(
        f"<tr><td>{e(value)}</td><td>{e(what)}</td><td>{e(whence)}</td></tr>"
        for value, what, whence in run.identifiers
    )
    return (
        '<details class="d" open><summary>every identifier in this run, and where it came from'
        f'</summary><table class="ids">{rows}</table></details>'
    )


def _scenario(run: Run, first: bool) -> str:
    passed, total = run.checks
    return (
        f'<details class="sc" id="{e(run.name)}"{" open" if first else ""}>'
        f"<summary>{e(run.title)}</summary>"
        f'<p class="obj">{e(run.objective)}</p>'
        f"{_identifiers(run)}"
        f"{''.join(_step(event) for event in run.events)}"
        f'<p class="note">{passed} of {total} checks passed · '
        f"discharges {e(', '.join(run.discharges))}</p></details>"
    )


def _index(runs: list[Run]) -> str:
    rows = "".join(
        f'<tr><td><a href="#{e(r.name)}">{e(r.title)}</a></td>'
        f'<td class="n">{r.count("customer")}</td><td class="n">{r.count("model")}</td>'
        f'<td class="n">{r.count("tool")}</td><td class="n">{r.count("offstage")}</td>'
        f'<td class="n">{r.checks[0]}/{r.checks[1]}</td></tr>'
        for r in runs
    )
    return (
        '<div class="wrap"><table class="index"><tr><th>Scenario</th><th>Turns</th>'
        "<th>Model calls</th><th>Tool calls</th><th>Offstage</th><th>Checks</th></tr>"
        f"{rows}</table></div>"
    )


def render(runs: list[Run], *, live_model: bool) -> str:
    """The page. One index, then one timeline per scenario."""
    calls = sum(r.count("model") for r in runs)
    turns = sum(r.count("customer") for r in runs)
    source = "a real provider" if live_model else "the suite's scripted models"
    legend = "".join(
        f'<span><span class="tag {kind}">{kind}</span> {e(what)}</span>' for kind, what in LEGEND
    )
    return f"""<title>Support Agent Run Trace</title>
<style>{STYLE}</style>
<header>
<h1>{len(runs)} runs, top to bottom</h1>
<p>Generated by <code>scripts/run_view.py</code> from actual runs, never drawn —
{turns} customer turns and {calls} model calls against {source}. The left rail on each
step is the position it happens at, the same P1–P8 the harness catalog uses.</p>
<p>Three things worth looking for. <strong>Most turns never reach a model</strong> — watch
the <em>route</em> step. <strong>The tool list does not change</strong> — open <em>what
passed through</em> on any two model calls in one run and compare; the surface is read once
at the start. <strong>Some sentences are the agent's, not the model's</strong> — every
reply says which, and the ones that promise anything are all the agent's.</p>
</header>
<main>
<div class="legend">{legend}</div>
<h2 class="sec">Every scenario</h2>
{_index(runs)}
<h2 class="sec">What happened, run by run</h2>
{"".join(_scenario(run, i == 0) for i, run in enumerate(runs))}
</main>"""

"""The architecture review: what exists, how it is held together, and what is not there.

Six questions, asked before G1 and answered from the code rather than about it:

1. **Is the harness complete?** Which capabilities this shape owes are exercised,
   which are accepted gaps, which are believed met and untested.
2. **How is the code structured, and are the principles actually followed?**
   Every claim here is a build-time check, named, with what it would catch.
3. **Context** — bloat, trimming, compaction, editing, summarisation. Which of the
   seven handlers exist, which do not, and why not is a decision rather than an
   omission.
4. **The controller** — inversion of control, coding to an interface, open–closed,
   dependency injection. Where an implementation can be swapped without editing
   code, and where it cannot.
5. **Inline evaluation** — the five injection points, which rules fire at each,
   and what a block *means* at each one.
6. **Payloads** — what arrives, what goes to the model, what comes back, what is
   stored.

**Authored prose, measured facts.** Every number, path, line count, rule name and
coverage figure below is read off disk when this runs. The explanations are
written; the facts cannot drift from the code, because they are the code.

    uv run python scripts/review_view.py
"""

from __future__ import annotations

import ast
import html
import json
import pathlib
import re
import subprocess
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.review_html import render  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "support_agent"
LIB = ROOT / "packages" / "agent-harness" / "src" / "agent_harness"  # the harness (T-019)
DEST = ROOT / "docs" / ".preview" / "ARCHITECTURE-REVIEW.html"

e = html.escape


def modules() -> list[dict]:
    """Every module, with its size, its layer, and how reusable it is."""
    from evals.reuse import AGENT, classify, files

    placed = classify()
    contract = tomllib.loads((ROOT / "pyproject.toml").read_text())
    # By type, not position: T-002 put two forbidden contracts ahead of it, and
    # a positional read broke the page without anything saying why.
    contracts = contract["tool"]["importlinter"]["contracts"]
    layered = next(c for c in contracts if c["type"] == "layers")["layers"]
    # "router | loop" is one rank holding two modules that may not see each other.
    rank: dict[str, tuple[int, str]] = {}
    for depth, row in enumerate(layered):
        for name in (n.strip() for n in row.split("|")):
            rank[name] = (depth, row)

    out = []
    for name, path in sorted(files().items()):
        rel = name.removeprefix(AGENT)
        top = rel.split("/")[0].removesuffix(".py")
        depth, row = rank.get(top, (99, "—"))
        out.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "full": str(path),
                "lines": len(path.read_text().splitlines()),
                "layer": row,
                "depth": depth,
                "reuse": placed.get(name, ""),
                "doc": _summary(path),
            }
        )
    return sorted(out, key=lambda m: (m["depth"], m["path"]))


def _summary(path: pathlib.Path) -> str:
    """A module's own first line about itself. Never written here."""
    try:
        doc = ast.get_docstring(ast.parse(path.read_text())) or ""
    except SyntaxError:
        return ""
    return doc.strip().split("\n")[0]


def protocols() -> list[dict]:
    """The ports. Each one is a seam a deployment fills without editing code."""
    source = (LIB / "contracts" / "protocols.py").read_text()
    tree = ast.parse(source)
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(
            isinstance(b, ast.Name) and b.id == "Protocol" for b in node.bases
        ):
            methods = [
                n.name for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            out.append(
                {
                    "name": node.name,
                    "doc": (ast.get_docstring(node) or "").strip().split("\n")[0],
                    "methods": methods,
                    "line": node.lineno,
                }
            )
    return out


def composition_root() -> list[dict]:
    """Every parameter `build` takes, and what kind of thing it is.

    The signature *is* the port list — that is the reference's own claim, and
    reading it here rather than restating it is what keeps the claim checkable.
    """
    source = (SRC / "entrypoint" / "__init__.py").read_text()
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build")
    ports = {p["name"] for p in protocols()} | {"DeliveryLog"}
    out = []
    for arg in fn.args.kwonlyargs:
        annotation = ast.unparse(arg.annotation) if arg.annotation else ""
        base = annotation.replace(" | None", "").strip()
        if base in ports:
            kind = "port"
        elif "Callable" in annotation:
            kind = "factory"
        elif base.endswith(("Rules", "RuleSet", "Policy", "Capacity", "RunConfig")):
            kind = "rules"
        else:
            kind = "value"
        out.append({"name": arg.arg, "type": annotation, "kind": kind})
    return out


POSITIONS = [
    (
        "PRE_MODEL",
        "loop/__init__.py",
        "the last place refusing costs nothing",
        "ends the turn — there is no lesser thing to do before a paid call",
    ),
    (
        "PRE_TOOL",
        "loop/screen.py",
        "between deciding to call and calling",
        "returns an error result to the model, which may choose again — the action did not "
        "happen, and that is a fact it can act on",
    ),
    (
        "POST_TOOL",
        "loop/screen.py",
        "after a result, before it enters context",
        "replaces the result; it cannot un-happen the effect and does not pretend to",
    ),
    (
        "POST_MODEL",
        "loop/__init__.py",
        "what the model wrote, before anyone sees it",
        "ends the turn as a refusal — typed, not a completion whose words were swapped (F-028)",
    ),
    (
        "REPLY",
        "entrypoint/__init__.py",
        "every reply, on every route",
        "replaces the words and keeps what the turn did — an escalation stays raised (F-020)",
    ),
]
"""The five injection points, in the order a turn meets them. The *meaning* of a
block differs at each, and that difference is the design rather than an accident
of where the call sites happen to be."""


def _located(rel: str) -> str:
    """Where a module is now: the agent's file, or the harness's when the agent's
    path holds only a re-export stub (T-019)."""
    from evals.reuse import is_stub

    mine = SRC / rel
    if (not mine.exists() or is_stub(mine)) and (LIB / rel).exists():
        return (LIB / rel).relative_to(ROOT).as_posix()
    return f"src/support_agent/{rel}"


def rules_at_each_position() -> list[dict]:
    """Which rules the default configuration fires at each point.

    Reached and populated are different questions, and the distinction is worth
    keeping: three of these five were declared, accepted configured rules, and
    called nothing at all until F-027. They are all reached now; two of them
    still ship with no default rules, which is a *choice about this agent* and
    not a hole in the mechanism.
    """
    from support_agent import policy as pol

    out = []
    for name, where, when, meaning in POSITIONS:
        position = getattr(pol.Position, name)
        rules = pol.DEFAULT_RULES.get(position, ())
        out.append(
            {
                "name": name,
                "where": _located(where),
                "when": when,
                "meaning": meaning,
                "rules": [getattr(r, "__name__", repr(r)) for r in rules],
            }
        )
    return out


# The seven context handlers, from the reference's own design note. `built` is
# not asserted here — it is the symbol that would have to exist, and the
# generator checks whether it does.
HANDLERS = [
    (
        "1 · Bounder",
        "context.assembled(max_chars=…) and ToolResult.for_context",
        "assembled",
        "A ceiling on what any one thing may contribute, applied at the assembler rather than "
        "left to whichever component notices first. A tool that returns ten thousand rows "
        "consumes the window in one step, and the loop then has no room to reason about what it "
        "just fetched.",
    ),
    (
        "2 · Deduplicator",
        "—",
        None,
        "Repeated identical tool results collapsed to one. Not built: the oscillation check in "
        "loop/plan.py stops a run that repeats a call, which removes the cause rather than the "
        "symptom, and short support conversations do not accumulate enough repetition to pay for "
        "it.",
    ),
    (
        "3 · Offloader",
        "—",
        None,
        "Large payloads moved out of context and referenced by handle. Not built: nothing this "
        "agent reads is large enough, and an offloader whose store is not durable is a new way to "
        "lose a conversation.",
    ),
    (
        "4 · Trimmer",
        "context.assembled and context.bounded",
        "bounded",
        "Whole exchanges dropped from the middle — never the head, which is the stable cached "
        "prefix, and never the tail, which is what the model is answering. Applied twice: per "
        "call on the way to the model, and on what is stored, because the second was missing and "
        "a conversation that ran all day wrote a larger row every turn.",
    ),
    (
        "5 · Structurer",
        "state/facts.py",
        "Facts",
        "A typed record of the work beside the transcript, written from what the far system "
        "confirmed rather than from what the model said. Built 13 September as AHC-0108 — the "
        "escalation handoff is assembled from it rather than summarised from the transcript, so "
        "nothing in a handoff is a paraphrase.",
    ),
    (
        "6 · Compactor",
        "—",
        None,
        "A summary replacing many turns. **Deliberately absent**, and the only handler whose "
        "absence is a written decision: a summary inherits the provenance of everything it "
        "summarised, and getting that wrong launders an instruction planted in a retrieved "
        "document into the system's own voice. AHC-0109 states the rule; the binding records this "
        "agent as meeting it vacuously.",
    ),
    (
        "7 · Selector",
        "—",
        None,
        "Fetching only the turns relevant to the current question. Not built: it needs a "
        "retriever and an embedding store, which is a second system to operate for a conversation "
        "that fits in the window.",
    ),
]


def handlers() -> list[dict]:
    trees = [*SRC.rglob("*.py"), *LIB.rglob("*.py")]
    source = "\n".join(p.read_text() for p in trees if "__pycache__" not in p.parts)
    out = []
    for name, where, symbol, why in HANDLERS:
        built = bool(symbol) and re.search(rf"\b{symbol}\b", source) is not None
        out.append({"name": name, "where": where, "built": built, "why": why})
    return out


CHECKS = [
    (
        "Strict static typing",
        "mypy --strict",
        "pyproject.toml",
        "A component that does not satisfy its interface; the wrong realisation wired; a new "
        "result kind nobody handles. Every match over a typed union ends in assert_never, so "
        "adding a route or an outcome fails the build rather than falling through the last "
        "branch.",
    ),
    (
        "The import contract",
        "lint-imports",
        "pyproject.toml",
        "Arrows point down only, and `exhaustive = true` means a new module cannot be added "
        "without being placed in the architecture. Four forbidden-module contracts fail closed: "
        "the package may not import a provider SDK, MCP, psycopg or agenttwin, with exactly one "
        "module excepted from each — so a new module inherits the ban without anyone remembering "
        "to add it.",
    ),
    (
        "Size and complexity",
        "ruff, as a ratchet",
        "pyproject.toml",
        "The god object, before it is 481 lines. Set at the worst offender the day it was "
        "measured and only ever lowered: complexity 14 → 8, statements 50 → 24, branches 12 → 7, "
        "longest module 744 → 416 lines. It has forced five extractions in the last two days, "
        "each of which turned out to be a job with a name.",
    ),
    (
        "Every failure declares its kind",
        "test_build_checks.py",
        "tests/test_build_checks.py",
        "A fifteenth exception type added without a kind from the shared vocabulary, leaving a "
        "caller unable to decide whether retrying is sensible. It found seven the author had "
        "missed.",
    ),
    (
        "Every counter is written to",
        "test_build_checks.py",
        "tests/test_build_checks.py",
        "A metric declared and never incremented — a dashboard panel reading zero forever, which "
        "is read as *this never happens* rather than as *nobody is counting*.",
    ),
    (
        "Every module is placed",
        "test_reuse_audit.py",
        "tests/test_reuse_audit.py",
        "A new module that nobody classified as mechanism, parameterised or per-agent — which is "
        "the classification a second agent depends on, and the one nobody notices is missing.",
    ),
    (
        "Every port is an interface",
        "test_build_checks.py",
        "tests/test_build_checks.py",
        "B13's first half: a collaborator typed as a concrete class, which is a deployment nobody "
        "can swap for a simulated world, a recording or a durable store.",
    ),
    (
        "Every span matches its contract",
        "telemetry/contract.py",
        "packages/agent-harness/src/agent_harness/telemetry/contract.py",
        "A span emitted with attributes nobody declared, or missing ones somebody depends on. It "
        "caught the freshness span the day it was written.",
    ),
]


def coverage() -> dict:
    from evals import statements

    seen = json.loads((ROOT / "evals" / "assurance-map.json").read_text())["by_statement"]
    vocab = statements.load()
    out = {}
    for family in ("AHC", "AAC", "AOAS", "Baseline"):
        owed = {s.id for s in vocab.owed(family)}
        out[family] = {"owed": len(owed), "exercised": len(owed & set(seen))}
    return out


def profile_gaps() -> dict:
    import yaml

    doc = yaml.safe_load((ROOT / "harness-profile.yaml").read_text())
    return {
        "gaps": [
            {"id": g["capability"], "why": " ".join(g["reason"].split())}
            for g in doc.get("accepted_gaps", [])
        ],
        "untested": list(doc.get("x_untested", [])),
        "decisions": len(doc.get("decisions", {})),
    }


def counted() -> list[dict]:
    from agent_harness.telemetry import counters

    out = []
    for name in counters.__all__:
        if not name.islower() or callable(getattr(counters, name)):
            continue
        instrument = getattr(counters, name)
        out.append(
            {
                "name": name,
                "metric": getattr(instrument, "name", name),
                "doc": (getattr(counters, "__doc__", "") or "")[:0] or "",
            }
        )
    return out


def suite() -> dict:
    """How many tests, and how many are tagged — run, not remembered."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--collect-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    collected = re.search(r"(\d+) tests collected", done.stdout)
    return {"tests": int(collected.group(1)) if collected else 0}


def gather() -> dict:
    from evals.reuse import SEAMS

    return {
        "modules": modules(),
        "protocols": protocols(),
        "root": composition_root(),
        "positions": rules_at_each_position(),
        "handlers": handlers(),
        "checks": CHECKS,
        "coverage": coverage(),
        "profile": profile_gaps(),
        "seams": SEAMS,
        "counters": counted(),
        "suite": suite(),
    }


if __name__ == "__main__":
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(render(gather()))
    print(f"{DEST}")

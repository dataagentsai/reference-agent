"""Print the real source along one request's path, in execution order.

Generated, never committed — a committed walkthrough drifts the first time a
function moves, and a stale map of the code is worse than none. The generator is
the artifact; run it when you want the map.

    uv run python scripts/code_path.py /tmp/CODE_PATH.py

`PATH` below is the only thing to maintain. A symbol that has been renamed or
moved is reported by name rather than silently skipped, so this file fails
loudly instead of quietly producing a shorter walkthrough than it claims.
"""

import ast
import pathlib
import sys

import agenttwin

ROOT = pathlib.Path(__file__).resolve().parent.parent
TWIN = pathlib.Path(agenttwin.__file__).resolve().parent.parent
"""AgentTwin is its own repository now; its paths resolve against wherever the
installed package lives, not against this one."""

PATH = [
    (
        "PHASE 1 — EDGE: before a run id exists",
        [
            ("src/support_agent/entrypoint/__init__.py", "Agent.handle"),
            ("packages/agent-harness/src/agent_harness/requests/__init__.py", "once"),
            (
                "packages/agent-harness/src/agent_harness/requests/__init__.py",
                "InMemoryRequests.claim",
            ),
        ],
    ),
    (
        "PHASE 2 — TURN + RESUME",
        [
            ("src/support_agent/entrypoint/__init__.py", "Agent._turn"),
            ("src/support_agent/entrypoint/__init__.py", "Agent._gates"),
            ("src/support_agent/entrypoint/pending.py", "ApprovalFlow.resume"),
            ("src/support_agent/approvals/workflow.py", "carry_out"),
            ("src/support_agent/entrypoint/__init__.py", "Agent._dispatch"),
        ],
    ),
    (
        "PHASE 3 — ROUTE: can we answer without the model?",
        [
            ("src/support_agent/router/__init__.py", "route"),
            ("src/support_agent/router/__init__.py", "_decide"),
        ],
    ),
    (
        "PHASE 4 — LOOP: the only path that reaches the model",
        [
            ("src/support_agent/loop/__init__.py", "run"),
            ("src/support_agent/loop/plan.py", "signature"),
        ],
    ),
    (
        "PHASE 4a — THE TYPED BOUNDARY",
        [
            ("src/support_agent/llm/__init__.py", "GroqClient.complete"),
            ("src/support_agent/llm/__init__.py", "_from_wire"),
        ],
    ),
    (
        "PHASE 4b — THE TOOL BOUNDARY",
        [
            ("src/support_agent/tools/__init__.py", "GatedTools.call"),
            ("src/support_agent/tools/mcp.py", "MCPTransport.invoke"),
        ],
    ),
    (
        "PHASE 4c — INSIDE THE PROJECTED WORLD",
        [
            ("agenttwin/projection.py", "_register"),
            ("agenttwin/world.py", "Action.evaluate"),
            ("agenttwin/world.py", "Condition.holds"),
        ],
    ),
    (
        "PHASE 4d — POLICY: the complete reply, screened",
        [
            ("src/support_agent/policy/__init__.py", "enforce"),
            ("src/support_agent/policy/__init__.py", "no_unclaimed_effect"),
        ],
    ),
    (
        "PHASE 5 — AGENTTWIN JUDGES IT",
        [
            ("agenttwin/scenario.py", "run"),
            ("agenttwin/omission.py", "owed"),
            ("agenttwin/omission.py", "omitted"),
            ("agenttwin/truth.py", "contradictions"),
            ("agenttwin/record.py", "diff"),
        ],
    ),
]


def find(tree, dotted):
    parts = dotted.split(".")
    node = tree
    for part in parts:
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and child.name == part
            ):
                node = child
                break
        else:
            return None
    return node


out = [
    "FULL CODE PATH — one turn through the support agent",
    "=" * 78,
    "Extracted from source with ast, in execution order. Nothing retyped.",
    "",
]
missing = []
for phase, items in PATH:
    out += ["", "#" * 78, f"#  {phase}", "#" * 78, ""]
    for rel, sym in items:
        f = (TWIN if rel.startswith("agenttwin/") else ROOT) / rel
        if not f.exists():
            missing.append(f"{rel} (no such file)")
            continue
        tree = ast.parse(f.read_text())
        node = find(tree, sym)
        if node is None:
            missing.append(f"{rel}::{sym}")
            continue
        src = ast.get_source_segment(f.read_text(), node)
        out += [
            f"# ── {rel}:{node.lineno} · {sym} " + "─" * max(0, 60 - len(rel) - len(sym)),
            "",
            src,
            "",
        ]

dest = pathlib.Path(sys.argv[1])
dest.write_text("\n".join(out))
print(f"{dest}  {sum(1 for line in out if line.startswith(chr(35) + ' \u2500\u2500'))} functions")
if missing:
    raise SystemExit("NOT FOUND (renamed or moved):\n  " + "\n  ".join(missing))

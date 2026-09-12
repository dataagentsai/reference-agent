"""The offline scripts still run.

Both broke silently during G0.4: they reach into the agent by name — one wraps
functions to trace a conversation, the other prints them — and a refactor that
moved `_resume` into `ApprovalFlow` left them pointing at nothing. Nothing ran
them, so nothing said. The scripts that need a network or a provider key
(`run_server.py`, `first_real_call.py`) are not here; the first is type-checked
as a composition root instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# (name, arguments after the script path, what its output must contain)
SCRIPTS = [
    (
        "trace_conversation — a refund is for the order's total, not the 24000 said",
        ["scripts/trace_conversation.py"],
        "issue_refund 4999",
    ),
    (
        "code_path — every symbol on the path still exists",
        ["scripts/code_path.py", "{out}"],
        "functions",
    ),
    (
        "build_manifest — every input to a generation is pinned",
        ["scripts/build_manifest.py"],
        "inputs pinned",
    ),
    (
        "scenario_coverage — which statements a conversation has exercised",
        ["scripts/scenario_coverage.py"],
        "statements reached",
    ),
]


@pytest.mark.tooling
@pytest.mark.parametrize(("name", "argv", "expect"), SCRIPTS, ids=[s[0] for s in SCRIPTS])
def test_an_offline_script_runs(name: str, argv: list[str], expect: str, tmp_path: Path) -> None:
    args = [a.format(out=tmp_path / "out.txt") for a in argv]
    done = subprocess.run(
        [sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert expect in done.stdout, done.stdout[-2000:]

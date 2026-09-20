"""The window-level rules parse, and page on the series they are for (T-055).

`promtool` is Prometheus's own checker, run from the image compose runs, so the
rules are checked by the thing that will evaluate them. Skipped with no Docker.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.tooling

RULES = Path(__file__).resolve().parents[1] / "deploy" / "prometheus" / "rules"
IMAGE = "prom/prometheus:v3.5.0"


def promtool(*args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("docker") is None:
        pytest.skip("no docker")
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        pytest.skip("docker is not running")
    return subprocess.run(
        ["docker", "run", "--rm", "-v", f"{RULES}:/r", "--entrypoint", "promtool", IMAGE, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


# (why, the promtool command)
CHECKS = [
    ("the rules parse", ("check", "rules", "/r/agent.yml")),
    (
        "each rule pages on its own series and not on the canary's",
        ("test", "rules", "/r/agent.test.yml"),
    ),
]


@pytest.mark.parametrize(("why", "command"), CHECKS, ids=[c[0] for c in CHECKS])
@pytest.mark.discharges("AAC-0114", "AAC-0116")
def test_the_alert_rules(why: str, command: tuple[str, ...]) -> None:
    ran = promtool(*command)
    assert ran.returncode == 0, ran.stdout + ran.stderr

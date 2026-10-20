"""The conformance plugin.

A test declares what it discharges with a marker; the run collects outcomes and
prints a report at the end naming what was covered, what failed, and what was
never exercised.

    @pytest.mark.discharges("AAC-0055", "AHC-0041", "Q-STEPS")
    async def test_the_step_budget_terminates(): ...
    @pytest.mark.scored          # model-driven: pass-rate, not pass/fail
    @pytest.mark.tooling         # tests an instrument, not the agent
    @pytest.mark.documents_gap("why")   # asserts a statement is missing

`discharges` takes an id from any spec in the family — see `evals/statements.py`
for the shapes. An id that names nothing fails collection. `pytest
--assurance-map` also writes the generated map to `evals/`.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from evals import assurance_map as amap
from evals import statements

from support_agent.conformance import Report

_report: Report | None = None
_vocab: statements.Vocabulary | None = None
_outcomes: list[amap.Outcome] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--assurance-map",
        action="store_true",
        help="write the generated Assurance Map to evals/",
    )


def pytest_configure(config: pytest.Config) -> None:
    global _report, _vocab
    config.addinivalue_line(
        "markers",
        "discharges(*ids): statement ids this test verifies — AAC-, AHC-, B#, or the AOAS's own",
    )
    config.addinivalue_line(
        "markers", "scored: a model-driven case — reported as a pass rate, never as pass/fail"
    )
    config.addinivalue_line(
        "markers",
        "tooling: tests an instrument, not the agent — kept out of the untagged remainder",
    )
    config.addinivalue_line(
        "markers",
        "unwired: tests a component the agent never calls — its ids are not counted",
    )
    config.addinivalue_line(
        "markers",
        "documents_gap(why): asserts a statement is missing — listed with its reason, "
        "never counted as an untagged oversight",
    )
    _report = Report()
    _vocab = statements.load()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Fail before running anything if a test claims a statement that does not exist.

    Also puts each test's claims on its JUnit record, as properties — the
    convention the gates read (`clean-ai-engineering/tools/gates`), so the
    features gate can judge any implementation's suite without importing it:
    `discharges` (comma-separated ids), and `tooling` or `unwired` when set.
    """
    for item in items:
        marker = item.get_closest_marker("discharges")
        if marker is not None:
            item.user_properties.append(("discharges", ",".join(marker.args)))
            if aac := _aac_claim(item, marker.args):
                item.user_properties.append(("aac", aac))
        for flag in ("tooling", "unwired"):
            if item.get_closest_marker(flag) is not None:
                item.user_properties.append((flag, "true"))
    if _vocab is None:
        return
    bad = {}
    for item in items:
        marker = item.get_closest_marker("discharges")
        if marker is not None and (unknown := _vocab.unknown(marker.args)):
            bad[item.nodeid.split("[", 1)[0]] = unknown
    if bad:
        detail = "\n".join(f"  {test}: {', '.join(ids)}" for test, ids in sorted(bad.items()))
        raise pytest.UsageError(f"discharges names statements that do not exist:\n{detail}")


def _aac_claim(item: pytest.Item, ids: tuple[str, ...]) -> str:
    """Recorded twice, once for each reader: `discharges` for the gates, `aac`
    for AAC's junit adapter, which reads only that name (`statements.aac_claim`)."""
    marked = {m for m in statements.NOT_EVIDENCE if item.get_closest_marker(m) is not None}
    return statements.aac_claim(ids, marked)


def _gap_of(item: pytest.Item) -> str:
    marker = item.get_closest_marker("documents_gap")
    if marker is None:
        return ""
    return str(marker.args[0]) if marker.args else "unstated"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    result = outcome.get_result()
    if result.when != "call" or _report is None:
        return
    marker = item.get_closest_marker("discharges")
    ids = tuple(marker.args) if marker is not None else ()
    _outcomes.append(
        amap.Outcome(
            nodeid=item.nodeid,
            ids=ids,
            passed=result.passed,
            tooling=item.get_closest_marker("tooling") is not None,
            unwired=item.get_closest_marker("unwired") is not None,
            gap=_gap_of(item),
            skipped=result.skipped,
        )
    )
    if result.skipped:
        # A test that did not run is not evidence either way: the obligation stays
        # "not exercised" rather than turning into a failure nobody can find.
        return
    scored = item.get_closest_marker("scored") is not None
    for obligation_id in ids:
        if statements.family(obligation_id) == "AAC":
            _report.record(obligation_id, item.nodeid, passed=result.passed, scored=scored)


@pytest.fixture(autouse=True)
def _spans_conform_to_the_contract():
    """Every test's spans are checked against the span contract.

    Enforced suite-wide rather than per-test, because AAC-0011's "complete trace"
    is only meaningful if completeness is checked everywhere it is emitted. A
    contract asserted in the three tests that thought to look is not a contract.
    """
    from support_agent import telemetry as tel

    yield
    exporter = getattr(tel, "_LAST_EXPORTER", None)
    if exporter is None:
        return
    violations = tel.validate(exporter.get_finished_spans())
    assert not violations, "span contract violated:\n  " + "\n  ".join(violations[:12])


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:
    if _report is None or config.getoption("quiet", 0) > 1:
        return
    terminalreporter.write(_report.render())
    if _vocab is not None and _outcomes:
        built = amap.build(_outcomes, _vocab)
        terminalreporter.write_line(amap.summary(built))
        if config.getoption("assurance_map"):
            amap.write(built, Path(__file__).resolve().parents[1] / "evals")
            terminalreporter.write_line("assurance map written to evals/ASSURANCE-MAP.md")


# --------------------------------------------------------------------------- #
# A throwaway PostgreSQL, and DBOS launched on it (test_dbos_waits,
# test_port_approval). The cluster lives in pytest's temporary directory on a
# free port, started once per module and stopped in teardown. With
# `AGENT_HARNESS_TEST_PG` set to a URL, that server is used instead; with
# neither a URL nor `initdb` on the path, every test using it is skipped.
# --------------------------------------------------------------------------- #

PG_BINARIES = [Path("/opt/homebrew/bin"), *Path("/opt/homebrew/opt").glob("postgresql*/bin")]


def _pg_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for directory in PG_BINARIES:
        if (directory / name).exists():
            return str(directory / name)
    return None


def _pg_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _pg_run(command: list[str]) -> None:
    # A C locale, or the server refuses to start ("postmaster became
    # multithreaded during startup") wherever the shell's locale is unset.
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    done = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    if done.returncode != 0:
        pytest.fail(f"{Path(command[0]).name} failed: {done.stdout[-800:]}{done.stderr[-800:]}")


@pytest.fixture(scope="module")
def postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A server URL with no database in it; each row makes its own."""
    given = os.environ.get("AGENT_HARNESS_TEST_PG")
    if given:
        yield given.rstrip("/")
        return
    initdb, pg_ctl = _pg_binary("initdb"), _pg_binary("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("no PostgreSQL: set AGENT_HARNESS_TEST_PG or put initdb on the path")
    data = tmp_path_factory.mktemp("pg") / "data"
    _pg_run(
        [initdb, "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8", "--no-locale"]
    )
    port = _pg_port()
    options = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''"
    log = data.parent / "server.log"
    _pg_run([pg_ctl, "-D", str(data), "-o", options, "-l", str(log), "-w", "start"])
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}"
    finally:
        _pg_run([pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"])


@pytest.fixture
def database(postgres: str) -> Iterator[str]:
    """DBOS launched on a fresh database for one row, and shut down after it."""
    pytest.importorskip("dbos", reason="the Azure extra (agent-harness[azure]) is not installed")
    from agent_harness.state import dbos as box

    url = f"{postgres}/waits_{uuid.uuid4().hex[:10]}"
    box.launch(url, name="agent-harness-test")
    try:
        yield url
    finally:
        box.shutdown()
        _pg_drop(postgres, url.rsplit("/", 1)[1])


def _pg_drop(server: str, name: str) -> None:
    """Drop the row's database: on a server given by `AGENT_HARNESS_TEST_PG` it
    would otherwise outlive the run (on the throwaway cluster it goes anyway)."""
    import psycopg

    with psycopg.connect(f"{server}/postgres", autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

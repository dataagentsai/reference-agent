"""Redaction and trace assertions.

The second half of this file is the first working example of AAC's M5 — trace
assertion — running with no backend, in milliseconds. That mechanism is the only
way to test *how* a system reached an answer rather than what it said.
"""

from __future__ import annotations

import pytest

from support_agent import telemetry as tel


@pytest.fixture
def exporter():
    ex = tel.configure(capture_payloads=False)
    yield ex
    ex.clear()


# --------------------------------------------------------------------------- #
# One redaction point. AAC-0095 — logged prompts and responses are redacted.
# --------------------------------------------------------------------------- #

REDACTION_CASES = [
    ("card number", "my card is 4111111111111111 ok", "[card]"),
    ("email", "write to basant@example.com please", "[email]"),
    ("indian mobile", "call me on 9876543210", "[phone]"),
    ("api key", "the key is sk-abcd1234efgh", "[secret]"),
    ("groq key", "gsk_ABCDEFGH12345678", "[secret]"),
]


@pytest.mark.parametrize(
    ("name", "raw", "marker"), REDACTION_CASES, ids=[c[0] for c in REDACTION_CASES]
)
@pytest.mark.discharges("AAC-0095", "AAC-0006")
def test_redaction_replaces_sensitive_spans(name: str, raw: str, marker: str) -> None:
    out = tel.redact(raw)
    assert marker in out


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("order id is not a card", "order 4111 was delivered"),
        ("ordinary prose survives", "your parcel arrives on Tuesday"),
    ],
    ids=["short number", "prose"],
)
@pytest.mark.discharges("AHC-0019")
def test_redaction_leaves_ordinary_text_alone(name: str, raw: str) -> None:
    assert tel.redact(raw) == raw


# India's identity numbers (claims-fnol-azure A12): (what, raw, exactly what is exported).
# 234567890124 and 499812345673 carry a correct Verhoeff check digit; 234567890125 does not.
INDIAN_IDS = [
    ("aadhaar, whole", "my aadhaar is 234567890124", "my aadhaar is [aadhaar]"),
    ("aadhaar, in fours", "aadhaar 2345 6789 0124 ok", "aadhaar [aadhaar] ok"),
    ("aadhaar, hyphens", "id 4998-1234-5673.", "id [aadhaar]."),
    ("pan", "PAN ABCPE1234F please", "PAN [pan] please"),
    ("pan, lower case", "pan is abcpe1234f", "pan is [pan]"),
    ("checksum fails: not aadhaar", "ref 234567890125", "ref 234567890125"),
    ("starts with 1: not aadhaar", "ref 134567890124", "ref 134567890124"),
    ("mixed separators: not aadhaar", "2345 6789-0124", "2345 6789-0124"),
    ("a spaced card is not aadhaar", "4111 1111 1111 1111", "4111 1111 1111 1111"),
    ("claim reference", "claim CLM-010003 is open", "claim CLM-010003 is open"),
    ("policy number", "policy POL-010004", "policy POL-010004"),
    ("amount", "a payout of ₹25,000 (Rs 25000)", "a payout of ₹25,000 (Rs 25000)"),
    ("pan-shaped, wrong fourth letter", "code ABCDE1234F", "code ABCDE1234F"),
]


@pytest.mark.parametrize(("name", "raw", "exported"), INDIAN_IDS, ids=[c[0] for c in INDIAN_IDS])
@pytest.mark.discharges("AAC-0095", "AHC-0019")
def test_aadhaar_and_pan_are_masked_and_nothing_else_is(name: str, raw: str, exported: str) -> None:
    assert tel.redact(raw) == exported


@pytest.mark.discharges("AHC-0019")
def test_an_agent_registers_its_own_patterns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import re

    from agent_harness.telemetry import redaction

    monkeypatch.setattr(redaction, "_registered", [])
    licence = (re.compile(r"\bDL-\d{6}\b"), "[licence]")
    redaction.register(licence)
    redaction.register(licence)
    assert redaction.registered() == (licence,)
    out = tel.redact("licence DL-123456, card 4111111111111111")
    assert out == "licence [licence], card [card]"


@pytest.mark.discharges("AHC-0019")
def test_redaction_bounds_length() -> None:
    out = tel.redact("x" * 9000, limit=100)
    assert len(out) < 200
    assert "truncated" in out


@pytest.mark.discharges("AAC-0095")
def test_payload_capture_is_off_by_default(exporter) -> None:
    """A default that leaks is a default that ships."""
    with tel.span("gen_ai.chat") as s:
        tel.set_payload(s, "prompt", "my card is 4111111111111111")
    attrs = tel.attributes_of(exporter.get_finished_spans()[0])
    assert "prompt" not in attrs


@pytest.mark.discharges("AAC-0095", "AAC-0006", "AHC-0019")
def test_payload_capture_redacts_when_enabled() -> None:
    ex = tel.configure(capture_payloads=True)
    with tel.turn_scope("run_redacts"), tel.span("gen_ai.chat") as s:
        tel.set_payload(s, "prompt", "my card is 4111111111111111")
    attrs = tel.attributes_of(ex.get_finished_spans()[0])
    assert "4111111111111111" not in attrs["prompt"]
    assert "[card]" in attrs["prompt"]
    tel.configure(capture_payloads=False)


def _in_turn(switch_off) -> None:
    with tel.turn_scope("run_in"), tel.span("capture.probe") as s:
        switch_off()
        tel.set_payload(s, "prompt", "my card is 4111111111111111")


def _after_turn(switch_off) -> None:
    with tel.turn_scope("run_before"):
        pass
    switch_off()
    with tel.span("capture.probe") as s:
        tel.set_payload(s, "prompt", "my card is 4111111111111111")


# (why, what runs with capture on, whether switched off on the way, whether kept)
# F-084: the decision was set on the context and never taken back, so it
# outlived its turn and survived capture being switched off.
SCOPE = [
    ("inside a turn chosen for capture", _in_turn, False, True),
    ("switched off during the turn", _in_turn, True, False),
    ("work after a captured turn", _after_turn, False, False),
    ("switched off after a captured turn", _after_turn, True, False),
]


@pytest.mark.parametrize(("why", "work", "off", "kept"), SCOPE, ids=[c[0] for c in SCOPE])
@pytest.mark.discharges("AAC-0095", "AHC-0019", "AHC-0114")
def test_a_capture_decision_lasts_exactly_as_long_as_its_turn(
    why: str, work, off: bool, kept: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = tel.configure(capture_payloads=True)
    work(lambda: off and monkeypatch.setattr(tel, "_CAPTURE_PAYLOADS", False))
    probe = next(s for s in exporter.get_finished_spans() if s.name == "capture.probe")
    assert ("prompt" in tel.attributes_of(probe)) is kept
    assert not tel.capturing(), "nothing is left switched on for whatever runs next"
    tel.configure(capture_payloads=False)


# --------------------------------------------------------------------------- #
# M5 — trace assertion. Structure, ordering, attributes, step count.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_span_carries_the_attributes_a_verdict_needs(exporter) -> None:
    with tel.span(
        "gen_ai.chat",
        **{
            tel.GEN_AI_REQUEST_MODEL: "llama-3.3-70b-versatile",
            tel.RUN_ID: "run_abc",
            tel.CONFIG_FINGERPRINT: "deadbeefdeadbeef",
            tel.RESOLUTION: "mock",
        },
    ) as s:
        tel.set_usage(s, input_tokens=120, output_tokens=45)

    attrs = tel.attributes_of(exporter.get_finished_spans()[0])
    assert attrs[tel.GEN_AI_REQUEST_MODEL] == "llama-3.3-70b-versatile"
    assert attrs[tel.GEN_AI_INPUT_TOKENS] == 120
    assert attrs[tel.RESOLUTION] == "mock"
    assert attrs[tel.CONFIG_FINGERPRINT] == "deadbeefdeadbeef"


@pytest.mark.tooling
def test_trajectory_is_reconstructable_from_the_trace(exporter) -> None:
    """AAC-0060 — the full trajectory is reconstructable. Ordering and step
    count are assertable without reading a word of the transcript."""
    for step in range(3):
        with tel.span("agent.step", **{tel.STEP: step}):
            with tel.span("gen_ai.chat"):
                pass
            with tel.span("agent.tool", **{tel.GEN_AI_TOOL_NAME: "get_order"}):
                pass

    spans = exporter.get_finished_spans()
    steps = [s for s in spans if s.name == "agent.step"]
    tools = [s for s in spans if s.name == "agent.tool"]

    assert len(steps) == 3
    assert [tel.attributes_of(s)[tel.STEP] for s in steps] == [0, 1, 2]
    assert all(tel.attributes_of(s)[tel.GEN_AI_TOOL_NAME] == "get_order" for s in tools)


@pytest.mark.discharges("B8", "B11")
def test_errors_are_recorded_and_still_raised(exporter) -> None:
    """Observability never changes control flow."""
    with pytest.raises(ValueError, match="boom"), tel.span("agent.step"):
        raise ValueError("boom")

    finished = exporter.get_finished_spans()[0]
    assert finished.status.status_code.name == "ERROR"
    assert finished.events


# --------------------------------------------------------------------------- #
# The collector, added alongside rather than instead.
# --------------------------------------------------------------------------- #


@pytest.mark.tooling
def test_a_collector_is_added_without_displacing_the_in_memory_exporter() -> None:
    """The one line T-016 called the cheapest thing on the page.

    Spans went to a Python list and nowhere else while `telemetry`'s own
    docstring said a composition root adds an OTLP processor alongside. This is
    that processor, and the property worth pinning is *alongside*: the in-memory
    exporter is what this suite asserts against, and a deployment gaining a
    backend must not make the tests blind.

    **The provider is shut down at the end and that is not tidiness.** A
    `BatchSpanProcessor` pointed at a collector that is not listening retries in
    a background thread — correct in a deployment, where a collector restarting
    should not lose a trace, and intolerable in a suite, where it is noise on
    every later test. The first draft of this test left it running.
    """
    exporter = tel.configure()
    added = tel.export_to("http://127.0.0.1:1/v1/traces")
    try:
        assert added is True
        with tel.span("agent.turn", **{tel.RUN_ID: "run_otlp"}):
            pass
        names = [s.name for s in exporter.get_finished_spans()]
        assert "agent.turn" in names, "the in-memory exporter stopped seeing spans"
    finally:
        tel._PROVIDER.shutdown()
        tel.configure()


@pytest.mark.tooling
def test_export_to_is_a_no_op_before_a_provider_exists() -> None:
    """So a caller need not order `configure` and `export_to`.

    Returning False rather than raising: a missing provider is a caller that did
    not configure one, and a composition root forced to guard every telemetry
    call would grow a branch nobody tests.
    """
    tel._PROVIDER = None
    try:
        assert tel.export_to("http://127.0.0.1:1/v1/traces") is False
    finally:
        tel.configure()

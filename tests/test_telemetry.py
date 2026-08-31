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
def test_redaction_leaves_ordinary_text_alone(name: str, raw: str) -> None:
    assert tel.redact(raw) == raw


def test_redaction_bounds_length() -> None:
    out = tel.redact("x" * 9000, limit=100)
    assert len(out) < 200
    assert "truncated" in out


def test_payload_capture_is_off_by_default(exporter) -> None:
    """A default that leaks is a default that ships."""
    with tel.span("gen_ai.chat") as s:
        tel.set_payload(s, "prompt", "my card is 4111111111111111")
    attrs = tel.attributes_of(exporter.get_finished_spans()[0])
    assert "prompt" not in attrs


def test_payload_capture_redacts_when_enabled() -> None:
    ex = tel.configure(capture_payloads=True)
    with tel.span("gen_ai.chat") as s:
        tel.set_payload(s, "prompt", "my card is 4111111111111111")
    attrs = tel.attributes_of(ex.get_finished_spans()[0])
    assert "4111111111111111" not in attrs["prompt"]
    assert "[card]" in attrs["prompt"]
    tel.configure(capture_payloads=False)


# --------------------------------------------------------------------------- #
# M5 — trace assertion. Structure, ordering, attributes, step count.
# --------------------------------------------------------------------------- #


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


def test_errors_are_recorded_and_still_raised(exporter) -> None:
    """Observability never changes control flow."""
    with pytest.raises(ValueError, match="boom"), tel.span("agent.step"):
        raise ValueError("boom")

    finished = exporter.get_finished_spans()[0]
    assert finished.status.status_code.name == "ERROR"
    assert finished.events

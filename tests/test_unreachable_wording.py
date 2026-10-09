"""The words a customer reads when the far end cannot be reached are the agent's.

claims-fnol-azure F-14: the library said "our order system" to a policyholder of
a motor insurer, because the sentence was a library constant. The library's
default is now neutral, and each agent passes its own — this shop still says
exactly what it said before.
"""

from __future__ import annotations

import pytest

from agent_harness import loop as agent_loop
from agent_harness.channel import SIGN_IN, Channel
from agent_harness.contracts import Failed, Identity, ToolUnavailable
from agent_harness.loop.ends import UNREACHABLE
from support_agent import binding
from support_agent import identity as ident


class Down:
    """A tool surface that cannot be opened."""

    async def list_tools(self, identity: Identity) -> object:
        raise ToolUnavailable("connection refused")

    async def call(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("never reached")


class Silent:
    """A model that must not be asked: the run ends before the first step."""

    async def complete(self, request: object) -> object:
        raise AssertionError("the model was asked")


CASES = [
    # (who is speaking, unreachable passed, what the customer reads)
    ("the library, unconfigured", None, UNREACHABLE),
    ("this shop", binding.UNREACHABLE, "I cannot reach our order system right now."),
    (
        "another agent",
        "I cannot reach our claims system right now.",
        "I cannot reach our claims system right now.",
    ),
]


@pytest.mark.parametrize(("who", "given", "said"), CASES, ids=[c[0] for c in CASES])
async def test_an_unreachable_far_end_is_named_in_the_agents_words(
    who: str, given: str | None, said: str
) -> None:
    extra = {} if given is None else {"unreachable": given}
    result, _ = await agent_loop.run(
        "where is my order AB-10001?",
        identity=Identity(customer_id="C-1042", scopes=ident.CUSTOMER_SCOPES),
        llm=Silent(),  # type: ignore[arg-type]
        tools=Down(),  # type: ignore[arg-type]
        system_prompt="You are a support agent.",
        **extra,  # type: ignore[arg-type]
    )
    assert isinstance(result, Failed)
    assert result.customer_message == said


WORDS = [
    # (constant, words the library must not hold)
    ("loop.ends.UNREACHABLE", UNREACHABLE, ("order", "refund", "shop")),
    ("channel.SIGN_IN", SIGN_IN, ("order", "refund", "shop")),
]


@pytest.mark.parametrize(("name", "text", "domain"), WORDS, ids=[w[0] for w in WORDS])
def test_the_librarys_own_words_hold_no_domain(
    name: str, text: str, domain: tuple[str, ...]
) -> None:
    assert not [word for word in domain if word in text.lower()], name


def test_the_channel_takes_the_agents_sign_in_words() -> None:
    fields = Channel.__dataclass_fields__
    assert fields["sign_in"].default == SIGN_IN
    assert "{portal}" in binding.SIGN_IN and "orders" in binding.SIGN_IN

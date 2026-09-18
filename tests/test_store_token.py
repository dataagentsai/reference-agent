"""The store's token expires, and the store client signs in again (T-017).

Found in the first person's session against Saleor: its access tokens last
minutes, the client held the first one for ever, and five minutes in every
call failed — so the agent handed a customer to a colleague over a
cancellation the store would have allowed. No store needed: this drives the
client's retry with scripted answers.
"""

from __future__ import annotations

import pytest

from order_system.store import Saleor, StoreUnavailable

EXPIRED = StoreUnavailable(
    '[{"message": "Signature has expired", "code": "ExpiredSignatureError"}]'
)
REFUSED = StoreUnavailable('[{"message": "You do not have permission"}]')
OK = {"shop": {"name": "Saleor"}}

# (why, what the store answers to each call in turn, the result, sign-ins after the first)
CASES = [
    ("a live token is used as it is", [OK], OK, 0),
    ("an expired token is replaced once, and the call repeated", [EXPIRED, OK], OK, 1),
    ("any other refusal is not a reason to sign in again", [REFUSED], REFUSED, 0),
    ("an expiry straight after signing in is not retried for ever",
     [EXPIRED, EXPIRED], EXPIRED, 1),
]  # fmt: skip


@pytest.mark.parametrize(("why", "answers", "result", "resigned"), CASES, ids=[c[0] for c in CASES])
async def test_an_expired_store_token_is_renewed_once(
    monkeypatch: pytest.MonkeyPatch, why: str, answers: list, result: object, resigned: int
) -> None:
    api = Saleor("http://store.test/graphql/", "admin@example.com", "pw", token="first")
    queue = list(answers)
    sign_ins: list[str] = []

    def post(self: Saleor, query: str, variables: dict) -> dict:
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sign_in(self: Saleor) -> None:
        sign_ins.append("again")
        self.token = f"token-{len(sign_ins)}"

    monkeypatch.setattr(Saleor, "_post", post)
    monkeypatch.setattr(Saleor, "_sign_in", sign_in)

    if isinstance(result, Exception):
        with pytest.raises(StoreUnavailable) as raised:
            await api("{ shop { name } }")
        assert raised.value is result
    else:
        assert await api("{ shop { name } }") == result
    assert len(sign_ins) == resigned

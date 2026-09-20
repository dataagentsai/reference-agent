"""The seeder signs in again when its token expires (F-042, second time).

Saleor's admin token lasts minutes and the suite seeds a namespace per
scenario, so a long run outlives it. Four shadow scenarios failed that way at
forty minutes — the store client had been taught to re-sign in and the seeder
had not. Offline: the one call that talks to Saleor is replaced, so the rows
here are about *when* a retry happens, not about Saleor.
"""

from __future__ import annotations

from typing import Any

import pytest
from deploy.saleor import seed as seeding

EXPIRED = '[{"message": "Signature has expired", "code": "ExpiredSignatureError"}]'
REFUSED = '[{"message": "You do not have permission"}]'

# (why, what the first call raises, whether credentials are kept, calls expected)
ROWS = [
    ("a stale token is signed in again and retried", EXPIRED, True, 2),
    ("without credentials there is nothing to retry with", EXPIRED, False, 1),
    ("any other refusal is the operator's to see", REFUSED, True, 1),
]


@pytest.mark.parametrize(("why", "raises", "credentials", "calls"), ROWS, ids=[r[0] for r in ROWS])
@pytest.mark.discharges("AAC-0009")
def test_the_seeder_re_signs_in_only_for_an_expired_token(
    monkeypatch: pytest.MonkeyPatch, why: str, raises: str, credentials: bool, calls: int
) -> None:
    api = seeding.Api(
        "http://saleor.test/graphql/",
        token="stale",
        credentials=("admin@example.test", "local-dev-only") if credentials else None,
    )
    seen: list[str] = []

    def once(self: seeding.Api, query: str, **variables: Any) -> dict[str, Any]:
        seen.append(self.token or "")
        if len(seen) == 1:
            raise seeding.SaleorError(raises)
        return {"shop": {"name": "fine"}}

    monkeypatch.setattr(seeding.Api, "_call", once)
    monkeypatch.setattr(seeding, "_token", lambda *_: "fresh")

    if calls == 1:
        with pytest.raises(seeding.SaleorError):
            api("query { shop { name } }")
    else:
        assert api("query { shop { name } }") == {"shop": {"name": "fine"}}
        assert seen == ["stale", "fresh"], "the retry went out under the new token"
    assert len(seen) == calls

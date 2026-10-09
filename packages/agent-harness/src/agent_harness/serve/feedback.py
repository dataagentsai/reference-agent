"""POST /feedback — the customer's own verdict on a conversation (AHC-0112).

    {"conversation_id": "cnv_…", "value": "up" | "down"}

The one outcome that is stated rather than inferred. It is recorded as a span on
the conversation and nothing else: the watch attaches it to the last turn before
it and counts it, so there is one place outcomes are counted, not two.

The conversation must be the caller's own, and a conversation that is not is
answered as not found, exactly as `/chat` answers it.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agent_harness import telemetry as tel
from agent_harness.contracts import ConversationId
from agent_harness.state import Conversation

VALUES = frozenset({"up", "down"})


async def feedback(request: Request) -> Response:
    from agent_harness.serve import BadRequest, _customer

    state = request.app.state
    try:
        who = _customer(request, state.verify)
        body: Any = json.loads(await request.body() or b"{}")
    except BadRequest as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status)
    except ValueError:
        return JSONResponse({"error": "body is not JSON"}, status_code=400)
    if not isinstance(body, dict) or body.get("value") not in VALUES:
        return JSONResponse({"error": "value must be 'up' or 'down'"}, status_code=400)
    cid = body.get("conversation_id")
    if not isinstance(cid, str) or not cid:
        return JSONResponse({"error": "conversation_id is required"}, status_code=400)
    previous = await state.store.latest(ConversationId(cid))
    if previous is None or Conversation.decode(previous).customer_id != who.customer_id:
        return JSONResponse({"error": "no such conversation"}, status_code=404)
    attributes = {tel.SESSION_ID: cid, tel.USER_ID: who.customer_id, tel.FEEDBACK: body["value"]}
    with tel.span("agent.feedback", **attributes):
        pass
    return JSONResponse({"recorded": body["value"]}, status_code=202)


__all__ = ["VALUES", "feedback"]

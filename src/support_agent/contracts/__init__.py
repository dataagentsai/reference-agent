"""Typed boundaries — the bottom layer.

Imports nothing from this package and depends only on the standard library and
pydantic. Everything else may depend on it; it may depend on nothing. That is
the whole of its job.
"""

from support_agent.contracts.domain import (
    Intent,
    OrderStatus,
    SideEffectClass,
    TerminationReason,
)
from support_agent.contracts.ids import (
    ConversationId,
    IdempotencyKey,
    Identity,
    RunId,
    new_conversation_id,
    new_run_id,
)
from support_agent.contracts.model import (
    Message,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
    ToolCall,
    Usage,
)
from support_agent.contracts.protocols import Clock, LLMClient, Store, ToolClient
from support_agent.contracts.results import (
    Agentic,
    Completed,
    Direct,
    Escalate,
    Escalated,
    Failed,
    NeedsApproval,
    Refuse,
    Refused,
    Route,
    TurnResult,
)
from support_agent.contracts.tools import (
    Approval,
    MissingIdempotencyKey,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolUnavailable,
    UnknownTool,
)

__all__ = [
    "Agentic",
    "Approval",
    "Clock",
    "Completed",
    "ConversationId",
    "Direct",
    "Escalate",
    "Escalated",
    "Failed",
    "IdempotencyKey",
    "Identity",
    "Intent",
    "LLMClient",
    "Message",
    "MissingIdempotencyKey",
    "ModelRequest",
    "ModelResponse",
    "ModelUnavailable",
    "NeedsApproval",
    "OrderStatus",
    "Refuse",
    "Refused",
    "Route",
    "RunId",
    "SideEffectClass",
    "Store",
    "TerminationReason",
    "ToolCall",
    "ToolClient",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolUnavailable",
    "TurnResult",
    "UnknownTool",
    "Usage",
    "new_conversation_id",
    "new_run_id",
]

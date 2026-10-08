"""The harness's configuration: the ceilings that can stop a call.

L15. Which models an agent approves, what its settings are called and what goes
into its fingerprint are the agent's own. What every agent's loop is bounded by
is not, and lives here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Budgets(BaseModel):
    """Ceilings that can actually stop a call.

    Two positions, deliberately. `max_output_tokens` is a per-call backstop at
    P3; `max_steps` and `max_cost_usd` are per-task and enforced at P4, because
    only the loop can see that fourteen calls are one runaway task rather than
    fourteen tasks. AAC-0093 and AAC-0008 are different obligations for exactly
    this reason.
    """

    model_config = ConfigDict(frozen=True)

    max_steps: int = 12
    max_cost_usd: float = 0.50
    max_output_tokens: int = 4096
    max_tool_result_chars: int = 8000
    """Tool results are bounded before they enter context — AAC-0105."""
    max_turn_seconds: int = 60
    """Wall clock for one turn (AHC-0096), checked before each step."""
    max_tool_calls_per_step: int = 8
    """How many tool calls one step may plan (AHC-0097). A step budget counts
    steps, so without this one step asking for fifty look-ups — or fifty
    writes — spends one of the twelve and does fifty things."""
    max_tool_calls_per_turn: int = 24
    """How many tool calls one turn may plan across all its steps (AHC-0097).
    Reaching either bound stops the turn before the step's calls run, as
    `tool_call_budget_exhausted` — never a silent cut to the first N (AHC-0059)."""
    max_retries_per_unit: int = 4
    """AHC-0024: retries across the whole turn, every call's together (F-087)."""


__all__ = ["Budgets"]

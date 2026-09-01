"""AgentTwin — a twin of the agent's world, not of the agent.

The agent under test is real. Its environment is the twin.

Deliberately outside `support_agent`, and the import contract forbids the agent
from importing this package at all. A system that can see its own simulator is a
system whose test results mean nothing.
"""

from agenttwin.actor import Determinism, Rule, ScriptedActor, StateMachineActor, Transcript
from agenttwin.loader import load
from agenttwin.perturbation import ChannelError, Slow, StaleRead, Timeline, perturbed
from agenttwin.projection import Live, project
from agenttwin.record import RunRecord, diff
from agenttwin.scenario import Scenario
from agenttwin.scenario import run as run_scenario
from agenttwin.world import World

__all__ = [
    "ChannelError",
    "Determinism",
    "Live",
    "Rule",
    "RunRecord",
    "Scenario",
    "ScriptedActor",
    "Slow",
    "StaleRead",
    "StateMachineActor",
    "Timeline",
    "Transcript",
    "World",
    "diff",
    "load",
    "perturbed",
    "project",
    "run_scenario",
]

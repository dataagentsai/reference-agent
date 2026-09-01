"""AgentTwin — a twin of the agent's world, not of the agent.

The agent under test is real. Its environment is the twin.

Deliberately outside `support_agent`, and the import contract forbids the agent
from importing this package at all. A system that can see its own simulator is a
system whose test results mean nothing.
"""

from agenttwin.loader import load
from agenttwin.projection import Live, project
from agenttwin.record import RunRecord, diff
from agenttwin.world import World

__all__ = ["Live", "RunRecord", "World", "diff", "load", "project"]

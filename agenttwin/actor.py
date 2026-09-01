"""Parties that act on their own.

Tier 2. Everything so far drove the agent with a fixed question. An actor decides
what to say *next* based on what the agent just said, which is how a scenario
reaches states nobody thought to write down.

## Determinism is declared, never inferred

DD3, in code. Three classes, and the trade is explicit:

`SCRIPTED` — a fixed list of turns. Free, exactly reproducible, and only ever
walks the path someone already imagined.

`STATE_MACHINE` — reacts to the agent's reply through declared rules. Still free,
still exactly reproducible, and **can reach states no script contains**, because
the branch it takes depends on what the agent actually did. This is the useful
middle and where most value lives.

`MODEL_DRIVEN` — a model plays the customer. Finds what neither of the above
would, costs a call per turn, and destroys reproducibility.

**A run's determinism class is the weakest of its actors**, and the run record
states it. A reader must never have to guess whether a result can be reproduced.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class Determinism(StrEnum):
    SCRIPTED = "scripted"
    STATE_MACHINE = "state_machine"
    MODEL_DRIVEN = "model_driven"


ORDER = [Determinism.SCRIPTED, Determinism.STATE_MACHINE, Determinism.MODEL_DRIVEN]


def weakest(*classes: Determinism) -> Determinism:
    """The weakest link. A world with one model-driven actor is model-driven."""
    return max(classes, key=ORDER.index) if classes else Determinism.SCRIPTED


@dataclass
class Turn:
    said: str
    heard: str = ""


class ScriptedActor:
    """Says the same things in the same order, whatever it hears.

    Honest about its limitation: an agent that answers perfectly and an agent
    that ignores the question entirely produce the same transcript from a script.
    """

    determinism = Determinism.SCRIPTED

    def __init__(self, turns: Sequence[str], *, name: str = "customer") -> None:
        self.name = name
        self._turns = list(turns)
        self._position = 0
        self.heard: list[str] = []

    def next(self, reply: str) -> str | None:
        if reply:
            self.heard.append(reply)
        if self._position >= len(self._turns):
            return None
        turn = self._turns[self._position]
        self._position += 1
        return turn


@dataclass
class Rule:
    """If the agent's reply matches, say this next."""

    when: re.Pattern[str]
    say: str
    label: str = ""


class StateMachineActor:
    """Reacts to what the agent actually said.

    Deterministic and free, and still able to reach somewhere a script cannot —
    because the branch depends on the agent's behaviour rather than on a plan
    made before the run.

    It knows only what a customer knows: its own goal and the replies it has
    received. Giving an actor visibility of the world would let a scenario pass
    because the actor steered around a defect.
    """

    determinism = Determinism.STATE_MACHINE

    def __init__(
        self,
        opening: str,
        rules: Sequence[Rule],
        *,
        name: str = "customer",
        max_turns: int = 6,
        persistence: str | None = None,
    ) -> None:
        self.name = name
        self.opening = opening
        self.rules = list(rules)
        self.max_turns = max_turns
        self.persistence = persistence
        """What to say when nothing matched. `None` ends the conversation.

        A persistent customer is the interesting one: real people do not accept
        the first refusal, and an agent that holds a policy for one turn and
        concedes on the third has failed in a way no single-turn test sees.
        """
        self.turns = 0
        self.path: list[str] = []
        self.heard: list[str] = []

    def next(self, reply: str) -> str | None:
        if reply:
            self.heard.append(reply)
        if self.turns >= self.max_turns:
            return None
        self.turns += 1

        if self.turns == 1:
            self.path.append("opening")
            return self.opening

        for rule in self.rules:
            if rule.when.search(reply):
                self.path.append(rule.label or rule.say[:24])
                return rule.say

        if self.persistence is None:
            self.path.append("done")
            return None
        self.path.append("persist")
        return self.persistence


class ModelActor:
    """A model plays the customer.

    Not built. The seam is declared so a scenario can say what it would cost:
    a call per turn, and a run that cannot be replayed. Worth having when the
    question is "what would somebody actually say", and never worth having in a
    regression suite.
    """

    determinism = Determinism.MODEL_DRIVEN

    def __init__(self, *_: object, **__: object) -> None:
        raise NotImplementedError(
            "a model-driven actor trades replay for realism; build it when a "
            "scenario needs unscripted phrasing, not for a regression suite"
        )


@dataclass
class Transcript:
    """What was said, by whom, in order."""

    turns: list[Turn] = field(default_factory=list)

    def add(self, said: str, heard: str) -> None:
        self.turns.append(Turn(said=said, heard=heard))

    def render(self) -> str:
        out = []
        for i, turn in enumerate(self.turns, 1):
            out.append(f"  {i}. customer: {turn.said}")
            out.append(f"     agent:    {turn.heard}")
        return "\n".join(out)


__all__ = [
    "Determinism",
    "ModelActor",
    "Rule",
    "ScriptedActor",
    "StateMachineActor",
    "Transcript",
    "Turn",
    "weakest",
]

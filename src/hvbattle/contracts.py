"""Stable contracts between battle sessions, runners, and client policies."""

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class ArenaOption:
    """One explicit Arena start option in the order exposed by the server."""

    battle_id: int
    token: str | None = None


@dataclass(frozen=True, slots=True)
class GrindfestOption:
    """One explicit GrindFest start option exposed by the server."""

    battle_id: int


@dataclass(frozen=True, slots=True)
class BattleCompleted:
    """Immutable summary; zero round values mean metadata was not observed."""

    is_isekai: bool
    decision_count: int
    final_round: int
    total_rounds: int


@dataclass(frozen=True, slots=True)
class BattleStopped:
    """Strategy yield summary; zero round values mean metadata was not observed."""

    is_isekai: bool
    decision_count: int
    current_round: int
    total_rounds: int


class BattleInterruptedError(RuntimeError):
    """The page left battle without positive completion evidence."""


class BattleActionOutcomeUnknownError(RuntimeError):
    """A submitted action did not produce authoritative completion evidence."""


class TurnDecision(StrEnum):
    """What the single-battle runner should do after a strategy decision."""

    ACTED = "acted"
    IDLE = "idle"
    STOP = "stop"

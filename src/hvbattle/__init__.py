"""Public battle-domain API."""

from .contracts import (
    ArenaOption,
    BattleActionKind,
    BattleActionOutcomeUnknownError,
    BattleActionRecoveryEvidence,
    BattleCompleted,
    BattleInterruptedError,
    BattleRecoveryExhaustedError,
    BattleStopped,
    BattleTurnPhase,
    BattleTurnState,
    GrindfestOption,
    RingOfBloodOption,
    RingOfBloodSnapshot,
    RingOfBloodStartOutcome,
    TurnDecision,
)
from .control_panel import BaseControlPanel, ControlPanel, NullControlPanel
from .hv_battle import BattleDriver
from .hv_battle_ponychart import PonyChartResolutionError, preload_ponychart_classifier
from .runner import BattleRunner
from .session import BattleSession
from .strategy import BattleLifecycle, BattleStrategy

__all__ = [
    "ArenaOption",
    "BaseControlPanel",
    "BattleActionKind",
    "BattleActionOutcomeUnknownError",
    "BattleActionRecoveryEvidence",
    "BattleCompleted",
    "BattleDriver",
    "BattleInterruptedError",
    "BattleRecoveryExhaustedError",
    "BattleLifecycle",
    "BattleRunner",
    "BattleSession",
    "BattleStopped",
    "BattleTurnPhase",
    "BattleTurnState",
    "BattleStrategy",
    "ControlPanel",
    "GrindfestOption",
    "NullControlPanel",
    "PonyChartResolutionError",
    "RingOfBloodOption",
    "RingOfBloodSnapshot",
    "RingOfBloodStartOutcome",
    "TurnDecision",
    "preload_ponychart_classifier",
]

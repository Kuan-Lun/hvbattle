"""Public battle-domain API."""

from .contracts import (
    ArenaOption,
    BattleActionOutcomeUnknownError,
    BattleCompleted,
    BattleInterruptedError,
    BattleStopped,
    BattleTurnPhase,
    BattleTurnState,
    GrindfestOption,
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
    "BattleActionOutcomeUnknownError",
    "BattleCompleted",
    "BattleDriver",
    "BattleInterruptedError",
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
    "TurnDecision",
    "preload_ponychart_classifier",
]

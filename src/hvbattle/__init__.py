"""Public battle-domain API."""

from hvbrowser.runtime import notify

from .contracts import (
    ArenaOption,
    BattleCompleted,
    BattleInterruptedError,
    BattleStopped,
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
    "BattleCompleted",
    "BattleDriver",
    "BattleInterruptedError",
    "BattleLifecycle",
    "BattleRunner",
    "BattleSession",
    "BattleStopped",
    "BattleStrategy",
    "ControlPanel",
    "GrindfestOption",
    "NullControlPanel",
    "PonyChartResolutionError",
    "TurnDecision",
    "notify",
    "preload_ponychart_classifier",
]

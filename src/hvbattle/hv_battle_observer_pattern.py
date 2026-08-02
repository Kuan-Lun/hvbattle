"""Compatibility imports for the former observer-pattern state module."""

from .battle_state import BattleStateStore, CombatLogTracker, OverviewMonsters

# The old implementation used these names for an observer graph.  Keep aliases
# for downstream imports while all new code uses the responsibility-based names.
BattleDashboard = BattleStateStore
LogEntry = CombatLogTracker

__all__ = [
    "BattleDashboard",
    "BattleStateStore",
    "CombatLogTracker",
    "LogEntry",
    "OverviewMonsters",
]

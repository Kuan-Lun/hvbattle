"""Client strategy protocols kept separate from result data contracts."""

from typing import Protocol

from .contracts import TurnDecision
from .session import BattleSession


class BattleStrategy(Protocol):
    """Client policy that performs at most one state-changing turn action."""

    async def take_turn(self, session: BattleSession, /) -> TurnDecision:
        """Act once, wait without acting, or return control to the caller."""


class BattleLifecycle(Protocol):
    """Optional strategy lifecycle implemented only by stateful policies."""

    async def on_battle_started(self, session: BattleSession, /) -> None:
        """Reset policy state before the first client-controlled turn."""

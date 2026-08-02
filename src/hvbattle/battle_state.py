"""Typed battle snapshot state and derived combat views."""

import asyncio
import re
from collections import Counter, deque
from dataclasses import dataclass, field

from hv_bie import parse_snapshot
from hv_bie.types import BattleSnapshot
from hvbrowser import HVDriver
from zendriver.core.connection import ProtocolException


@dataclass(slots=True)
class OverviewMonsters:
    """Derived indexes for the alive monsters in one parsed snapshot."""

    alive_monster: list[int] = field(default_factory=list)
    alive_system_monster: list[int] = field(default_factory=list)
    alive_monster_with_buff: dict[str, list[int]] = field(default_factory=dict)
    alive_monster_name: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, snapshot: BattleSnapshot) -> OverviewMonsters:
        alive = [monster for monster in snapshot.monsters.values() if monster.alive]
        buffs = {buff for monster in alive for buff in monster.buffs}
        return cls(
            alive_monster=[monster.slot_index for monster in alive],
            alive_system_monster=[
                monster.slot_index for monster in alive if monster.system_monster_type
            ],
            alive_monster_with_buff={
                buff: [monster.slot_index for monster in alive if buff in monster.buffs]
                for buff in buffs
            },
            alive_monster_name={monster.name: monster.slot_index for monster in alive},
        )


@dataclass(slots=True)
class CombatLogTracker:
    """Track round metadata and the occurrence-aware delta of battle log lines."""

    current_round: int = 0
    prev_round: int = 0
    total_round: int = 0
    prev_lines: deque[str] = field(default_factory=lambda: deque(maxlen=1000))
    current_lines: list[str] = field(default_factory=list)

    def _parse_round_info(self, lines: list[str]) -> None:
        for line in lines:
            match = re.search(r"Round (\d+) / (\d+)", line)
            if match is None:
                continue
            self.current_round = int(match.group(1))
            self.prev_round = self.current_round
            self.total_round = int(match.group(2))

    def update(self, snapshot: BattleSnapshot) -> None:
        lines = list(snapshot.log.lines)
        self.current_lines = []
        if not lines:
            return

        # Consume previous occurrences one by one so repeated, identical combat
        # messages remain observable rather than being lost to a membership test.
        previous_occurrences = Counter(self.prev_lines)
        for line in lines:
            if previous_occurrences[line] > 0:
                previous_occurrences[line] -= 1
            else:
                self.current_lines.append(line)

        self._parse_round_info(self.current_lines)
        self.prev_lines = deque(lines, maxlen=1000)


class BattleStateStore:
    """Own the latest parsed snapshot and all battle-scoped derived state."""

    def __init__(self, driver: HVDriver) -> None:
        self._driver = driver
        self.snap: BattleSnapshot | None = None
        self.overview_monsters = OverviewMonsters()
        self.log_entries = CombatLogTracker()

    async def _get_content(self, timeout: float = 10.0) -> str:
        """Read HTML with one bounded retry for zendriver's duplicate-id race."""
        try:
            return await asyncio.wait_for(
                self._driver.page.get_content(), timeout=timeout
            )
        except ProtocolException as error:
            if "duplicate" not in str(error).casefold():
                raise
            return await asyncio.wait_for(
                self._driver.page.get_content(), timeout=timeout
            )

    async def init(self) -> None:
        """Compatibility spelling for the first state refresh."""
        await self.update()

    async def inspect(self) -> BattleSnapshot:
        """Parse the page without consuming log or derived-view state."""
        return parse_snapshot(await self._get_content())

    async def update(self) -> None:
        snapshot = await self.inspect()
        self.snap = snapshot
        self.log_entries.update(snapshot)
        self.overview_monsters = OverviewMonsters.from_snapshot(snapshot)

    def reset(self) -> None:
        """Discard every piece of state whose lifetime is one battle."""
        self.snap = None
        self.overview_monsters = OverviewMonsters()
        self.log_entries = CombatLogTracker()

    def update_overview_monsters(self) -> None:
        """Compatibility helper for callers that assign ``snap`` in tests."""
        if self.snap is None:
            raise RuntimeError("No battle snapshot is available")
        self.overview_monsters = OverviewMonsters.from_snapshot(self.snap)


__all__ = ["BattleStateStore", "CombatLogTracker", "OverviewMonsters"]

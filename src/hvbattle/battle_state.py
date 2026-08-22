"""Typed battle snapshot state and derived combat views."""

import asyncio
import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field

from hv_bie import parse_snapshot
from hv_bie.types import BattleSnapshot
from hvbrowser import HVDriver
from hvbrowser.runtime import wait_for_zendriver
from zendriver.core.connection import ProtocolException

from ._timing import PROTOCOL_TIMEOUT_SECONDS, protocol_timeout


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

    @staticmethod
    def _validated_timeout(timeout: float) -> float:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
            or timeout > PROTOCOL_TIMEOUT_SECONDS
        ):
            raise ValueError("battle content timeout must be finite and in (0, 5]")
        return float(timeout)

    async def _get_content(
        self,
        timeout: float = PROTOCOL_TIMEOUT_SECONDS,
        *,
        _deadline_at: float | None = None,
    ) -> str:
        """Read HTML with one bounded retry for zendriver's duplicate-id race."""
        timeout = self._validated_timeout(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if _deadline_at is None else _deadline_at

        def remaining() -> float:
            return max(0.0, deadline - loop.time())

        operation_timeout = remaining()
        if operation_timeout <= 0:
            raise TimeoutError("Battle content deadline expired before its read")
        try:
            content = await wait_for_zendriver(
                self._driver.page.get_content(),
                timeout=protocol_timeout(operation_timeout),
                owner=self._driver.page,
            )
        except ProtocolException as error:
            if "duplicate" not in str(error).casefold():
                raise
            operation_timeout = remaining()
            if operation_timeout <= 0:
                raise TimeoutError(
                    "Battle content retry budget was exhausted"
                ) from error
            content = await wait_for_zendriver(
                self._driver.page.get_content(),
                timeout=protocol_timeout(operation_timeout),
                owner=self._driver.page,
            )
        if not isinstance(content, str):
            raise TypeError("Battle page content must be text")
        if remaining() <= 0:
            raise TimeoutError("Battle content result arrived after its deadline")
        return content

    async def inspect(
        self, *, timeout: float = PROTOCOL_TIMEOUT_SECONDS
    ) -> BattleSnapshot:
        """Parse the page without consuming log or derived-view state."""
        timeout = self._validated_timeout(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        snapshot = parse_snapshot(
            await self._get_content(timeout=timeout, _deadline_at=deadline)
        )
        if loop.time() >= deadline:
            raise TimeoutError("Battle snapshot parsing exceeded its deadline")
        return snapshot

    async def update(self, *, timeout: float = PROTOCOL_TIMEOUT_SECONDS) -> None:
        snapshot = await self.inspect(timeout=timeout)
        self.snap = snapshot
        self.log_entries.update(snapshot)
        self.overview_monsters = OverviewMonsters.from_snapshot(snapshot)

    def reset(self) -> None:
        """Discard every piece of state whose lifetime is one battle."""
        self.snap = None
        self.overview_monsters = OverviewMonsters()
        self.log_entries = CombatLogTracker()


__all__ = ["BattleStateStore", "CombatLogTracker", "OverviewMonsters"]

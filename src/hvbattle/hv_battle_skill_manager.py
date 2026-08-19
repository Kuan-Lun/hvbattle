from collections import defaultdict
from typing import Any

from hv_bie.types import BattleSnapshot
from hvbrowser import HVDriver

from ._zendriver import wait_for_zendriver
from .battle_state import BattleStateStore
from .hv_battle_action_manager import ElementActionManager


class SkillManager:
    def __init__(
        self,
        driver: HVDriver,
        state_store: BattleStateStore,
        element_action_manager: ElementActionManager,
    ) -> None:
        self.hvdriver = driver
        self.state_store = state_store
        self.element_action_manager = element_action_manager
        self.skills_cost: dict[str, int] = defaultdict(lambda: 1)

    @property
    def page(self) -> Any:
        return self.hvdriver.page

    def _snapshot(self) -> BattleSnapshot:
        snapshot = self.state_store.snap
        if snapshot is None:
            raise RuntimeError("No battle snapshot is available")
        return snapshot

    async def _select_pane_control(self, selector: str) -> Any:
        return await wait_for_zendriver(self.page.select(selector), timeout=2.0)

    async def _is_pane_visible(self, pane_id: str) -> bool:
        element = await self._select_pane_control(f"#{pane_id}")
        style: str = element.attrs.get("style", "")
        return style != "display: none;"

    async def open_skills_menu(self) -> None:
        await self.element_action_manager.click_until(
            lambda: self._select_pane_control("#ckey_skill"),
            lambda: self._is_pane_visible("pane_skill"),
        )

    async def open_spells_menu(self) -> None:
        await self.element_action_manager.click_until(
            lambda: self._select_pane_control("#ckey_skill"),
            lambda: self._is_pane_visible("pane_magic"),
        )

    async def _click_skill(self, element_id: str, iswait: bool) -> None:
        selector = f'[id="{element_id}"]'
        if iswait:
            await self.element_action_manager.click_and_wait_log_locator(selector)
        else:
            await self.element_action_manager.click_locator(selector)

    async def cast(self, key: str, iswait: bool = True) -> bool:
        if key not in self.get_skills_and_spells():
            return False

        ability = self.get_skills_and_spells()[key]

        self.skills_cost[key] = max(
            self.get_max_skill_mp_cost_by_name(key), self.skills_cost[key]
        )

        if ability.available:
            snapshot = self._snapshot()
            if key in snapshot.abilities.skills:
                await self.open_skills_menu()
            if key in snapshot.abilities.spells:
                await self.open_spells_menu()
            await self._click_skill(ability.element_id, iswait)
            return True
        else:
            return False

    def get_skills_and_spells(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        return snapshot.abilities.skills | snapshot.abilities.spells

    def get_max_skill_mp_cost_by_name(self, skill_name: str) -> int:
        if skill_name not in self.get_skills_and_spells():
            return -1  # Default cost if skill not found

        self.skills_cost[skill_name] = max(
            self.get_skills_and_spells()[skill_name].cost,
            self.skills_cost[skill_name],
        )
        return self.skills_cost[skill_name]

from typing import Any

from hv_bie.types import BattleSnapshot
from hvbrowser import HVDriver

from ._zendriver import wait_for_zendriver
from .battle_state import BattleStateStore
from .hv_battle_action_manager import ElementActionManager

GEM_ITEMS = {"mystic gem", "health gem", "mana gem", "spirit gem"}


class ItemProvider:
    def __init__(
        self,
        driver: HVDriver,
        state_store: BattleStateStore,
        element_action_manager: ElementActionManager,
    ) -> None:
        self.hvdriver: HVDriver = driver
        self.state_store = state_store
        self.element_action_manager = element_action_manager

    @property
    def page(self) -> Any:
        return self.hvdriver.page

    def _snapshot(self) -> BattleSnapshot:
        snapshot = self.state_store.snap
        if snapshot is None:
            raise RuntimeError("No battle snapshot is available")
        return snapshot

    async def _get_items_menu_element(self) -> Any:
        return await wait_for_zendriver(
            self.hvdriver.page.select("#ckey_items"), timeout=2.0
        )

    async def click_items_menu(self) -> None:
        # Resilient click to mitigate stale menu button
        await self.element_action_manager.click_resilient(self._get_items_menu_element)

    async def is_open_items_menu(self) -> bool:
        """Check if the items menu is open."""
        items_menu = await self._get_items_menu_element()
        items_src = await wait_for_zendriver(
            items_menu.apply("(el) => el.src || ''"), timeout=3.0
        )
        return "items_s.png" in items_src

    async def use(self, item: str) -> bool:
        snapshot = self._snapshot()
        if item not in snapshot.items.items:
            return False

        parsed_item = snapshot.items.items[item]
        if not parsed_item.available:
            return False

        if not parsed_item.element_id:
            return False

        if not await self.is_open_items_menu():
            await self.click_items_menu()
            # Wait for items pane to become visible
            await wait_for_zendriver(
                self.hvdriver.page.select("#pane_item", timeout=2), timeout=2.0
            )

        await self.element_action_manager.click_and_wait_log_locator(
            f'[id="{parsed_item.element_id}"]'
        )
        return True

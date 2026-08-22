from typing import Any

from hv_bie.types import BattleSnapshot
from hvbrowser import HVDriver
from hvbrowser.runtime import is_browser_generation_error, wait_for_zendriver

from ._timing import PROTOCOL_TIMEOUT_SECONDS, SemanticDeadline
from .battle_state import BattleStateStore
from .contracts import BattleInterruptedError
from .hv_battle_action_manager import ElementActionManager

GEM_ITEMS = {"mystic gem", "health gem", "mana gem", "spirit gem"}
_MENU_DEADLINE_SECONDS = PROTOCOL_TIMEOUT_SECONDS
_OPEN_ITEMS_MENU_JS = """
(() => {
    const control = document.getElementById('ckey_items');
    const pane = document.getElementById('pane_item');
    const visible = Boolean(
        pane && !pane.hidden
        && window.getComputedStyle(pane).display !== 'none'
    );
    const selected = Boolean(
        control && typeof control.src === 'string'
        && control.src.includes('items_s.png')
    );
    if (!control || !pane || typeof control.click !== 'function') {
        return {status: 'controls-missing', clicked: false};
    }
    if (visible || selected) return {status: 'open', clicked: false};
    control.click();
    const nowVisible = !pane.hidden
        && window.getComputedStyle(pane).display !== 'none';
    const nowSelected = typeof control.src === 'string'
        && control.src.includes('items_s.png');
    return {
        status: nowVisible || nowSelected ? 'open' : 'not-open',
        clicked: true,
    };
})()
"""
_ITEMS_MENU_STATE_JS = """
(() => {
    const control = document.getElementById('ckey_items');
    const pane = document.getElementById('pane_item');
    return Boolean(
        (pane && !pane.hidden
            && window.getComputedStyle(pane).display !== 'none')
        || (control && typeof control.src === 'string'
            && control.src.includes('items_s.png'))
    );
})()
"""


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

    async def click_items_menu(
        self,
        *,
        deadline: SemanticDeadline | None = None,
    ) -> None:
        active_deadline = deadline or SemanticDeadline.after(_MENU_DEADLINE_SECONDS)
        try:
            operation_timeout = active_deadline.protocol_timeout()
            result = await wait_for_zendriver(
                self.page.evaluate(_OPEN_ITEMS_MENU_JS),
                timeout=operation_timeout,
                owner=self.page,
            )
        except Exception as error:
            # The page-side script contains the only click.  An ambiguous CDP
            # result is never followed by a retry on this browser generation.
            if is_browser_generation_error(error):
                raise
            raise BattleInterruptedError(
                "Items menu mutation outcome is unknown",
                diagnostic_code="battle.items-menu-outcome-unknown",
            ) from error
        active_deadline.require_remaining("Items menu deadline expired")
        if not isinstance(result, dict) or result.get("status") != "open":
            raise BattleInterruptedError(
                "Items menu did not become visible after one atomic mutation",
                diagnostic_code="battle.items-menu-not-open",
            )

    async def is_open_items_menu(self) -> bool:
        """Check if the items menu is open."""
        result = await wait_for_zendriver(
            self.page.evaluate(_ITEMS_MENU_STATE_JS),
            timeout=PROTOCOL_TIMEOUT_SECONDS,
            owner=self.page,
        )
        if type(result) is not bool:
            raise ValueError("Items menu state returned an invalid value")
        return result

    async def use(self, item: str) -> bool:
        snapshot = self._snapshot()
        if item not in snapshot.items.items:
            return False

        parsed_item = snapshot.items.items[item]
        if not parsed_item.available:
            return False

        if not parsed_item.element_id:
            return False

        await self.click_items_menu(
            deadline=SemanticDeadline.after(_MENU_DEADLINE_SECONDS)
        )

        await self.element_action_manager.click_and_wait_log_locator(
            f'[id="{parsed_item.element_id}"]'
        )
        return True

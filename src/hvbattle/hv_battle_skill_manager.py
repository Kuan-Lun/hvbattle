import json
from collections import defaultdict
from typing import Any

from hv_bie.types import BattleSnapshot
from hvbrowser import HVDriver
from hvbrowser.runtime import is_browser_generation_error, wait_for_zendriver

from ._timing import PROTOCOL_TIMEOUT_SECONDS, SemanticDeadline
from .battle_state import BattleStateStore
from .contracts import BattleInterruptedError
from .hv_battle_action_manager import ElementActionManager

_MENU_DEADLINE_SECONDS = PROTOCOL_TIMEOUT_SECONDS


def _open_skillbook_pane_script(pane_id: str) -> str:
    return f"""
    (() => {{
        const control = document.getElementById('ckey_skill');
        const target = document.getElementById('{pane_id}');
        const visible = (element) => Boolean(
            element && !element.hidden
            && window.getComputedStyle(element).display !== 'none'
        );
        if (!control || !target || typeof control.click !== 'function') {{
            return {{status: 'controls-missing', clicks: 0}};
        }}
        if (visible(target)) return {{status: 'open', clicks: 0}};
        for (let clicks = 1; clicks <= 2; clicks += 1) {{
            control.click();
            if (visible(target)) return {{status: 'open', clicks}};
        }}
        return {{status: 'not-open', clicks: 2}};
    }})()
    """


def _click_skill_control_script(element_id: str) -> str:
    return f"""
    (() => {{
        const element = document.getElementById({json.dumps(element_id)});
        if (!element || typeof element.click !== 'function') {{
            return {{status: 'missing'}};
        }}
        element.click();
        return {{status: 'clicked'}};
    }})()
    """


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

    async def _open_skillbook_pane(self, pane_id: str) -> None:
        deadline = SemanticDeadline.after(_MENU_DEADLINE_SECONDS)
        try:
            operation_timeout = deadline.protocol_timeout()
            result = await wait_for_zendriver(
                self.page.evaluate(_open_skillbook_pane_script(pane_id)),
                timeout=operation_timeout,
                owner=self.page,
            )
        except Exception as error:
            # The atomic script may already have clicked.  Never issue a
            # second menu mutation when its acknowledgement is unknown.
            if is_browser_generation_error(error):
                raise
            raise BattleInterruptedError(
                "Skillbook menu mutation outcome is unknown",
                diagnostic_code="battle.skill-menu-outcome-unknown",
            ) from error
        deadline.require_remaining("Skillbook menu deadline expired")
        if not isinstance(result, dict) or result.get("status") != "open":
            raise BattleInterruptedError(
                "Skillbook menu did not expose the requested pane",
                diagnostic_code="battle.skill-menu-not-open",
            )

    async def open_skills_menu(self) -> None:
        await self._open_skillbook_pane("pane_skill")

    async def open_spells_menu(self) -> None:
        await self._open_skillbook_pane("pane_magic")

    async def _click_skill(self, element_id: str, iswait: bool) -> None:
        selector = f'[id="{element_id}"]'
        if iswait:
            await self.element_action_manager.click_and_wait_log_locator(selector)
        else:
            deadline = SemanticDeadline.after(_MENU_DEADLINE_SECONDS)
            try:
                operation_timeout = deadline.protocol_timeout()
                result = await wait_for_zendriver(
                    self.page.evaluate(_click_skill_control_script(element_id)),
                    timeout=operation_timeout,
                    owner=self.page,
                )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                raise BattleInterruptedError(
                    "Skill control mutation outcome is unknown",
                    diagnostic_code="battle.skill-control-outcome-unknown",
                ) from error
            deadline.require_remaining("Skill control mutation deadline expired")
            if not isinstance(result, dict) or result.get("status") != "clicked":
                raise BattleInterruptedError(
                    "Skill control disappeared before its single click",
                    diagnostic_code="battle.skill-control-missing",
                )

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

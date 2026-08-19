"""Hot-path browser reads must never hang forever.

zendriver's own timeout= kwarg on Tab.select()/xpath()/find_all() only
checks a deadline between polling iterations; a single hung underlying CDP
call never returns control to that check, so it is not a real bound. Every
call exercised here previously relied on that fake protection (or had none
at all) and is now wrapped in wait_for_zendriver, which bounds the call
itself via asyncio.wait() regardless of what zendriver is doing internally.
"""

import asyncio
import unittest
from typing import Any
from unittest.mock import Mock

from hvbattle._zendriver import ZendriverOperationTimeout
from hvbattle.hv_battle_item_provider import ItemProvider
from hvbattle.hv_battle_ponychart import PonyChart
from hvbattle.hv_battle_skill_manager import SkillManager
from hvbattle.session import BattleSession


class _HangingPage:
    """A page whose browser reads never resolve."""

    async def evaluate(self, expression: str) -> Any:
        await asyncio.Event().wait()

    async def xpath(self, expression: str, timeout: float = 2.0) -> Any:
        await asyncio.Event().wait()

    async def query_selector_all(self, selector: str) -> Any:
        await asyncio.Event().wait()

    async def select(self, selector: str, timeout: float = 10) -> Any:
        await asyncio.Event().wait()


class _HangingElement:
    async def apply(self, js_function: str) -> Any:
        await asyncio.Event().wait()


class BattleSessionHangProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_battle_phase_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(session._read_battle_phase(), timeout=5)

    async def test_has_battle_marker_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(session._has_battle_marker(), timeout=5)

    async def test_attack_monster_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(session.attack_monster(1), timeout=5)

    async def test_go_next_floor_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(session.go_next_floor(), timeout=5)


class PonyChartHangProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_times_out_instead_of_hanging_forever(self) -> None:
        pony_chart = object.__new__(PonyChart)
        pony_chart.hvdriver = Mock()
        pony_chart.hvdriver.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(pony_chart._check(), timeout=5)


class SkillManagerHangProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_pane_control_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        manager = object.__new__(SkillManager)
        manager.hvdriver = Mock()
        manager.hvdriver.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(
                manager._select_pane_control("#pane_skill"), timeout=5
            )


class ItemProviderHangProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_items_menu_element_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        provider = object.__new__(ItemProvider)
        provider.hvdriver = Mock()
        provider.hvdriver.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(provider._get_items_menu_element(), timeout=5)

    async def test_is_open_items_menu_times_out_when_apply_hangs(self) -> None:
        provider = object.__new__(ItemProvider)
        provider.hvdriver = Mock()
        provider._get_items_menu_element = _get_hanging_element

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(provider.is_open_items_menu(), timeout=5)


async def _get_hanging_element() -> _HangingElement:
    return _HangingElement()


if __name__ == "__main__":
    unittest.main()

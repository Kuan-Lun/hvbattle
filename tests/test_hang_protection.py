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
from unittest.mock import AsyncMock, Mock

from hvbattle import ArenaOption, GrindfestOption
from hvbattle._zendriver import ZendriverOperationTimeout
from hvbattle.battle_launcher import BattleLauncher
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

    async def select_all(self, selector: str, timeout: float = 10) -> Any:
        await asyncio.Event().wait()


class _RespondsOnceThenHangsPage:
    """A page whose first evaluate() succeeds, and every call after hangs.

    Models a launcher submission flow: the pre-submit URL check succeeds,
    but the browser stops responding to the actual form-submission call.
    """

    def __init__(self, first_result: Any) -> None:
        self._first_result = first_result
        self._calls = 0

    async def evaluate(self, expression: str) -> Any:
        self._calls += 1
        if self._calls == 1:
            return self._first_result
        await asyncio.Event().wait()


class _HangingElement:
    async def apply(self, js_function: str) -> Any:
        await asyncio.Event().wait()


class _SlowPage:
    """A page that takes a real, bounded number of seconds to answer.

    Models the page right after login navigates from Forums to the
    HentaiVerse realm root: still settling, not stuck. A production incident
    (2026-08-19) showed the first _has_battle_marker/_read_battle_phase call
    after that navigation can legitimately take several seconds; a timeout
    tight enough to reject that turned normal startup latency into a fatal,
    non-retried ZendriverOperationTimeout (retries are deliberately refused
    for that exception -- see session.py's inspect_battle_presence).
    """

    def __init__(self, delay_seconds: float, result: Any) -> None:
        self._delay_seconds = delay_seconds
        self._result = result

    async def evaluate(self, expression: str) -> Any:
        await asyncio.sleep(self._delay_seconds)
        return self._result

    async def xpath(self, expression: str, timeout: float = 2.0) -> Any:
        await asyncio.sleep(self._delay_seconds)
        return self._result


class BattleSessionHangProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_battle_phase_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(session._read_battle_phase(), timeout=15)

    async def test_read_battle_phase_tolerates_post_navigation_settling(
        self,
    ) -> None:
        """Regression test for the 2026-08-19 startup incident: a several-
        second-but-not-infinite delay right after login/realm navigation
        must succeed, not be misclassified as a fatal, non-retried hang."""

        session = object.__new__(BattleSession)
        session.page = _SlowPage(6.0, "active")

        phase = await asyncio.wait_for(session._read_battle_phase(), timeout=15)

        self.assertEqual(phase, "active")

    async def test_has_battle_marker_tolerates_post_navigation_settling(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = _SlowPage(3.0, True)

        has_marker = await asyncio.wait_for(session._has_battle_marker(), timeout=10)

        self.assertTrue(has_marker)

    async def test_has_battle_marker_returns_promptly_when_genuinely_absent(
        self,
    ) -> None:
        """Regression test for the 2026-08-19 startup incident's actual root
        cause: zendriver's xpath() retries in an enable/find/disable loop
        until its own timeout elapses whenever the element is absent -- the
        common case whenever no battle is running -- so it always burns the
        full timeout on the single most frequent startup check. evaluate()
        must answer immediately instead, not just "eventually, boundedly"."""

        session = object.__new__(BattleSession)
        session.page = _SlowPage(0.01, False)

        started = asyncio.get_running_loop().time()
        has_marker = await asyncio.wait_for(session._has_battle_marker(), timeout=2)
        elapsed = asyncio.get_running_loop().time() - started

        self.assertFalse(has_marker)
        self.assertLess(elapsed, 1.0)

    async def test_has_battle_marker_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(session._has_battle_marker(), timeout=15)

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

    async def test_wait_for_image_loaded_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        pony_chart = object.__new__(PonyChart)
        pony_chart.hvdriver = Mock()
        pony_chart.hvdriver.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(
                pony_chart._wait_for_image_loaded(timeout=0.2), timeout=5
            )

    async def test_save_pony_chart_image_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        pony_chart = object.__new__(PonyChart)
        pony_chart.hvdriver = Mock()
        pony_chart.hvdriver.page = _HangingPage()
        pony_chart._image_directory = None
        pony_chart._wait_for_image_loaded = AsyncMock(return_value=None)

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(pony_chart._save_pony_chart_image(), timeout=5)


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


class BattleLauncherHangProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_grindfest_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        client = Mock()
        client.page = _HangingPage()
        launcher = BattleLauncher(client, Mock())
        launcher._path_prefix = AsyncMock(return_value="")

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(
                launcher.start_grindfest(GrindfestOption(battle_id=1)), timeout=10
            )

    async def test_start_arena_submission_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        """The pre-submit URL check succeeds; the form-submission call hangs."""

        client = Mock()
        client.page = _RespondsOnceThenHangsPage(
            "https://hentaiverse.org/?s=Battle&ss=ar"
        )
        launcher = BattleLauncher(client, Mock())
        launcher._path_prefix = AsyncMock(return_value="")

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(
                launcher.start_arena(ArenaOption(battle_id=1, token=None)),
                timeout=10,
            )


async def _get_hanging_element() -> _HangingElement:
    return _HangingElement()


if __name__ == "__main__":
    unittest.main()

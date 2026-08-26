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
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from hvbrowser import Realm
from hvbrowser.runtime import ZendriverOperationTimeout
from zendriver import cdp

from hvbattle import ArenaOption, GrindfestOption
from hvbattle._timing import SemanticDeadline
from hvbattle.hv_battle_item_provider import ItemProvider
from hvbattle.hv_battle_skill_manager import SkillManager
from hvbattle.session import BattleSession
from hvbattle.testing import (
    TestingBattleLauncher as BattleLauncher,
)
from hvbattle.testing import (
    TestingPonyChart as PonyChart,
)


class _HangingPage:
    """A page whose browser reads never resolve."""

    async def send(self, command: object) -> Any:
        await asyncio.Event().wait()

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
    async def test_atomic_battle_presence_times_out_as_one_command(self) -> None:
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session.page = _HangingPage()

        with (
            patch("hvbattle.session.PROTOCOL_TIMEOUT_SECONDS", 0.02),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await asyncio.wait_for(session.is_in_battle(), timeout=1)

    async def test_read_battle_phase_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(session._read_battle_phase(), timeout=15)

    async def test_read_battle_phase_never_uses_navigation_as_read_budget(
        self,
    ) -> None:
        """A phase evaluate is one protocol read, not a lifecycle wait."""

        session = object.__new__(BattleSession)
        session.page = _SlowPage(6.0, "active")

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(session._read_battle_phase(), timeout=6)

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

        with (
            patch("hvbattle.hv_battle_ponychart.PROTOCOL_TIMEOUT_SECONDS", 0.02),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await asyncio.wait_for(pony_chart._check(), timeout=1)

    async def test_wait_for_image_loaded_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        pony_chart = object.__new__(PonyChart)
        pony_chart.hvdriver = Mock()
        pony_chart.hvdriver.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(
                pony_chart._wait_for_image_loaded(deadline=SemanticDeadline.after(0.2)),
                timeout=5,
            )

    async def test_capture_pony_chart_image_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        driver = Mock()
        driver.page = _HangingPage()
        pony_chart = PonyChart(driver)
        pony_chart._wait_for_image_loaded = AsyncMock(
            return_value=SimpleNamespace(
                source="https://hentaiverse.org/pony-chart.png",
                document_url="https://hentaiverse.org/battle",
                monitor_token="raw-monitor",
                width=640,
                height=480,
                rendered_width=640,
                rendered_height=480,
            )
        )
        pony_chart._wait_for_matching_network_requests = AsyncMock(
            return_value=(
                SimpleNamespace(
                    saw_request=True,
                    failure=None,
                    response_received=True,
                    finished=True,
                    is_image=True,
                    status=200,
                    mime_type="image/png",
                    request_id=cdp.network.RequestId("pony-request"),
                ),
            )
        )

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(
                pony_chart._capture_pony_chart_image(
                    deadline=SemanticDeadline.after(0.2)
                ),
                timeout=5,
            )


class SkillManagerHangProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_menu_open_times_out_instead_of_hanging_forever(self) -> None:
        manager = object.__new__(SkillManager)
        manager.hvdriver = Mock()
        manager.hvdriver.page = _HangingPage()

        with (
            patch("hvbattle.hv_battle_skill_manager._MENU_DEADLINE_SECONDS", 0.02),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await asyncio.wait_for(manager.open_skills_menu(), timeout=1)


class ItemProviderHangProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_items_menu_times_out_instead_of_hanging_forever(self) -> None:
        provider = object.__new__(ItemProvider)
        provider.hvdriver = Mock()
        provider.hvdriver.page = _HangingPage()

        with self.assertRaises(ZendriverOperationTimeout):
            await asyncio.wait_for(
                provider.click_items_menu(deadline=SemanticDeadline.after(0.02)),
                timeout=1,
            )

    async def test_is_open_items_menu_times_out_when_evaluate_hangs(self) -> None:
        provider = object.__new__(ItemProvider)
        provider.hvdriver = Mock(page=_HangingPage())

        with (
            patch("hvbattle.hv_battle_item_provider.PROTOCOL_TIMEOUT_SECONDS", 0.02),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await asyncio.wait_for(provider.is_open_items_menu(), timeout=1)


class BattleLauncherHangProtectionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _stub_lifecycle(launcher: BattleLauncher) -> None:
        lifecycle = Mock()
        lifecycle.enable = AsyncMock()
        lifecycle.trigger = Mock()
        lifecycle.wait = AsyncMock()
        lifecycle.close = Mock()
        launcher._main_document_lifecycle = Mock(return_value=lifecycle)

    async def test_start_grindfest_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        client = Mock()
        client.page = _HangingPage()
        launcher = BattleLauncher(client, Mock())
        self._stub_lifecycle(launcher)

        with (
            patch(
                "hvbattle.battle_launcher._NAVIGATION_MUTATION_TIMEOUT_SECONDS",
                0.02,
            ),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await asyncio.wait_for(
                launcher.start_grindfest(
                    GrindfestOption(battle_id=1),
                    expected_realm=Realm.PERSISTENT,
                ),
                timeout=1,
            )

    async def test_start_arena_submission_times_out_instead_of_hanging_forever(
        self,
    ) -> None:
        """The single atomic form snapshot/submission call hangs."""

        client = Mock()
        client.page = _HangingPage()
        launcher = BattleLauncher(client, Mock())
        self._stub_lifecycle(launcher)

        with (
            patch(
                "hvbattle.battle_launcher._NAVIGATION_MUTATION_TIMEOUT_SECONDS",
                0.02,
            ),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await asyncio.wait_for(
                launcher.start_arena(
                    ArenaOption(
                        battle_id=1,
                        token=None,
                        challenge_name=None,
                        exp_multiplier=None,
                    ),
                    expected_realm=Realm.PERSISTENT,
                ),
                timeout=1,
            )


if __name__ == "__main__":
    unittest.main()

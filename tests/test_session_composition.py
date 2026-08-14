import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hvbrowser import HentaiVerseSession, HVDriver, Realm

import hvbattle.session as session_module
from hvbattle import (
    BattleSession,
    RingOfBloodOption,
    RingOfBloodSnapshot,
    RingOfBloodStartOutcome,
)
from hvbattle.battle_state import BattleStateStore


class BattleSessionCompositionTests(unittest.IsolatedAsyncioTestCase):
    def test_session_uses_one_shared_battle_component_graph(self) -> None:
        browser = HVDriver(headless=True)
        hentaiverse = HentaiVerseSession(browser=browser)
        image_directory = Path("artifacts/pony_chart")

        session = BattleSession(
            hentaiverse=hentaiverse,
            ponychart_image_directory=str(image_directory),
        )

        actions = session.element_action_manager
        items = session._item_provider
        skills = session._skill_manager
        buffs = session._buff_manager
        self.assertIsNotNone(actions)
        self.assertIsNotNone(items)
        self.assertIsNotNone(skills)
        self.assertIsNotNone(buffs)
        assert actions is not None
        assert items is not None
        assert skills is not None
        assert buffs is not None
        self.assertIs(session.hentaiverse, hentaiverse)
        self.assertIs(session._launcher.browser, browser)
        self.assertIs(session._launcher.realm, hentaiverse.realm)
        self.assertIs(session._ponychart.hvdriver, browser)
        self.assertEqual(session._ponychart._image_directory, image_directory)
        self.assertIs(actions.hvdriver, browser)
        self.assertIs(items.state_store, session.battle_state)
        self.assertIs(skills.state_store, session.battle_state)
        self.assertIs(buffs.state_store, session.battle_state)
        self.assertIs(items.element_action_manager, actions)
        self.assertIs(skills.element_action_manager, actions)
        self.assertIs(buffs.element_action_manager, actions)
        self.assertIs(buffs._item_provider, items)
        self.assertIs(buffs._skill_manager, skills)
        self.assertIs(session.battle_recovery._actions, actions)
        self.assertIs(session.battle_recovery._state_store, session.battle_state)
        self.assertIs(
            actions._begin_dialog_observation.__self__,
            session.action_dialog_tracker,
        )
        self.assertIs(
            actions._get_dialog_category.__self__,
            session.action_dialog_tracker,
        )
        self.assertFalse(hasattr(browser, "_hvbattle_action_lock"))

        with self.assertRaisesRegex(AttributeError, "read-only compatibility"):
            session.battle_dashboard = BattleStateStore(browser)
        self.assertIs(items.state_store, session.battle_state)

    def test_injected_session_cannot_be_combined_with_browser_options(self) -> None:
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))

        with self.assertRaisesRegex(TypeError, "injected HentaiVerseSession"):
            BattleSession(hentaiverse.browser, hentaiverse=hentaiverse)
        with self.assertRaisesRegex(TypeError, "injected HentaiVerseSession"):
            BattleSession(headless=False, hentaiverse=hentaiverse)

    async def test_async_context_manages_composed_browser_lifecycle(self) -> None:
        events: list[str] = []
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(
            hentaiverse=hentaiverse,
            auto_accept_dialogs=True,
        )
        session._setup_alert_handler = AsyncMock(
            side_effect=lambda: events.append("alert-handler")
        )

        async def start(
            *,
            on_browser_ready: Callable[[], Awaitable[None]],
        ) -> None:
            events.append("browser-ready")
            self.assertEqual(on_browser_ready, session._on_browser_ready)
            await on_browser_ready()
            events.append("login")

        with (
            patch.object(
                session_module,
                "preload_ponychart_classifier",
                side_effect=lambda: events.append("preload"),
            ) as preload,
            patch.object(
                hentaiverse,
                "start",
                new=AsyncMock(side_effect=start),
            ) as start_session,
            patch.object(
                hentaiverse,
                "__aexit__",
                new=AsyncMock(),
            ) as close,
        ):
            async with session as entered:
                self.assertIs(entered, session)

        preload.assert_called_once_with()
        start_session.assert_awaited_once_with(
            on_browser_ready=session._on_browser_ready,
        )
        session._setup_alert_handler.assert_awaited_once_with()
        close.assert_awaited_once_with(None, None, None)
        self.assertEqual(
            events,
            ["preload", "browser-ready", "alert-handler", "login"],
        )

    async def test_realm_is_read_from_composed_navigator(self) -> None:
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(hentaiverse=hentaiverse)
        hentaiverse.realm.current = AsyncMock(return_value=Realm.ISEKAI)

        self.assertTrue(await session.is_isekai)

        hentaiverse.realm.current.return_value = Realm.PERSISTENT
        self.assertFalse(await session.is_isekai)

    async def test_ring_of_blood_operations_delegate_to_shared_launcher(self) -> None:
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(hentaiverse=hentaiverse)
        option = RingOfBloodOption(112, "Triple Trio and the Tree", 1.0, 10)
        snapshot = RingOfBloodSnapshot(20, (option,))
        session._launcher.goto_ring_of_blood = AsyncMock(return_value=True)
        session._launcher.inspect_ring_of_blood = AsyncMock(return_value=snapshot)
        session._launcher.start_ring_of_blood = AsyncMock(
            return_value=RingOfBloodStartOutcome.SUBMITTED
        )

        self.assertTrue(await session.goto_ring_of_blood())
        self.assertIs(await session.inspect_ring_of_blood(), snapshot)
        self.assertIs(
            await session.start_ring_of_blood(
                option,
                expected_before=snapshot,
            ),
            RingOfBloodStartOutcome.SUBMITTED,
        )

        session._launcher.goto_ring_of_blood.assert_awaited_once_with()
        session._launcher.inspect_ring_of_blood.assert_awaited_once_with()
        session._launcher.start_ring_of_blood.assert_awaited_once_with(
            option,
            expected_before=snapshot,
        )


class BattleSessionBoundaryTests(unittest.TestCase):
    def test_session_does_not_inherit_browser_maintenance_operations(self) -> None:
        self.assertFalse(issubclass(BattleSession, HVDriver))
        for operation in (
            "player",
            "equipment",
            "market",
            "lottery",
            "monster_lab",
        ):
            with self.subTest(operation=operation):
                self.assertFalse(hasattr(BattleSession, operation))

    def test_player_resources_have_named_read_only_views(self) -> None:
        session = object.__new__(BattleSession)
        session.battle_state = SimpleNamespace(
            snap=SimpleNamespace(
                player=SimpleNamespace(
                    hp_percent=25,
                    mp_percent=50,
                    sp_percent=75,
                    overcharge_value=240,
                )
            )
        )

        self.assertEqual(session.hp_percent, 25.0)
        self.assertEqual(session.mp_percent, 50.0)
        self.assertEqual(session.sp_percent, 75.0)
        self.assertEqual(session.overcharge, 240.0)
        self.assertEqual(session.get_stat_percent("overcharge"), 240.0)


if __name__ == "__main__":
    unittest.main()

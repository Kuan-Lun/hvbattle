import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hvbrowser import HVDriver

import hvbattle.session as session_module
from hvbattle import BattleSession
from hvbattle.battle_state import BattleStateStore


class BattleSessionCompositionTests(unittest.IsolatedAsyncioTestCase):
    def test_session_uses_one_shared_battle_component_graph(self) -> None:
        client = HVDriver(headless=True)

        session = BattleSession(browser_client=client)

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
        self.assertIs(session.browser_client, client)
        self.assertIs(session._launcher.browser_client, client)
        self.assertIs(actions.hvdriver, client)
        self.assertIs(items.state_store, session.battle_state)
        self.assertIs(skills.state_store, session.battle_state)
        self.assertIs(buffs.state_store, session.battle_state)
        self.assertIs(items.element_action_manager, actions)
        self.assertIs(skills.element_action_manager, actions)
        self.assertIs(buffs.element_action_manager, actions)
        self.assertIs(buffs._item_provider, items)
        self.assertIs(buffs._skill_manager, skills)
        self.assertFalse(hasattr(client, "_hvbattle_action_lock"))

        with self.assertRaisesRegex(AttributeError, "read-only compatibility"):
            session.battle_dashboard = BattleStateStore(client)
        self.assertIs(items.state_store, session.battle_state)

    def test_injected_client_cannot_be_combined_with_browser_options(self) -> None:
        client = HVDriver(headless=True)

        with self.assertRaisesRegex(TypeError, "injected browser_client"):
            BattleSession(client, browser_client=client)
        with self.assertRaisesRegex(TypeError, "injected browser_client"):
            BattleSession(headless=False, browser_client=client)

    async def test_async_context_manages_composed_browser_lifecycle(self) -> None:
        client = HVDriver(headless=True)
        session = BattleSession(
            browser_client=client,
            auto_accept_dialogs=True,
        )
        session._setup_alert_handler = AsyncMock()

        with (
            patch.object(
                session_module,
                "preload_ponychart_classifier",
            ) as preload,
            patch.object(client, "_init_browser", new=AsyncMock()) as initialize,
            patch.object(client, "login", new=AsyncMock()) as login,
            patch.object(client, "gohomepage", new=AsyncMock()) as gohomepage,
            patch.object(client, "__aexit__", new=AsyncMock()) as close,
        ):
            async with session as entered:
                self.assertIs(entered, session)

        preload.assert_called_once_with()
        initialize.assert_awaited_once_with()
        session._setup_alert_handler.assert_awaited_once_with()
        login.assert_awaited_once_with()
        gohomepage.assert_awaited_once_with()
        close.assert_awaited_once_with(None, None, None)


class BattleSessionBoundaryTests(unittest.TestCase):
    def test_session_does_not_inherit_browser_maintenance_operations(self) -> None:
        self.assertFalse(issubclass(BattleSession, HVDriver))
        for operation in (
            "inspect_lottery",
            "inspect_market",
            "purchase_lottery_tickets",
            "recoverstamina",
            "repairequipment",
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

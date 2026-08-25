import asyncio
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hvbrowser import HentaiVerseSession, HVDriver, Realm

import hvbattle.session as session_module
from hvbattle import (
    RingOfBloodChallenge,
    RingOfBloodOption,
    RingOfBloodSnapshot,
    RingOfBloodStartOutcome,
)
from hvbattle.testing import (
    TestingAuditEventBus,
)
from hvbattle.testing import (
    TestingBattleSession as BattleSession,
)


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
        self.assertIs(session._launcher.audit_event_bus, session.audit_event_bus)
        self.assertIs(session._ponychart.hvdriver, browser)
        self.assertIs(session._ponychart.audit_event_bus, session.audit_event_bus)
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
        self.assertIs(actions.audit_event_bus, session.audit_event_bus)
        self.assertIs(
            actions._begin_dialog_observation.__self__,
            session.action_dialog_tracker,
        )
        self.assertIs(
            actions._get_dialog_category.__self__,
            session.action_dialog_tracker,
        )
        self.assertFalse(hasattr(browser, "_hvbattle_action_lock"))

    def test_injected_audit_bus_is_shared_without_session_owned_lifecycle(
        self,
    ) -> None:
        bus = TestingAuditEventBus()
        session = BattleSession(
            hentaiverse=HentaiVerseSession(browser=HVDriver(headless=True)),
            audit_event_bus=bus,
        )

        self.assertIs(session.audit_event_bus, bus)
        assert session.element_action_manager is not None
        self.assertIs(session.element_action_manager.audit_event_bus, bus)
        self.assertIs(session._launcher.audit_event_bus, bus)
        self.assertIs(session._ponychart.audit_event_bus, bus)
        session.raise_for_audit_failure()

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
        session._ponychart.arm_network_capture = AsyncMock(
            side_effect=lambda: events.append("network-arm")
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
                new=AsyncMock(side_effect=lambda: events.append("preload")),
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

        preload.assert_awaited_once_with()
        start_session.assert_awaited_once_with(
            on_browser_ready=session._on_browser_ready,
        )
        session._setup_alert_handler.assert_awaited_once_with()
        session._ponychart.arm_network_capture.assert_awaited_once_with()
        close.assert_awaited_once_with(None, None, None)
        self.assertEqual(
            tuple(event for event in events if event != "preload"),
            ("browser-ready", "network-arm", "alert-handler", "login"),
        )
        self.assertIn("preload", events)

    async def test_async_context_preloads_while_browser_starts(self) -> None:
        browser_started = asyncio.Event()
        classifier_started = asyncio.Event()
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(hentaiverse=hentaiverse)
        session._ponychart.arm_network_capture = AsyncMock()

        async def prepare_classifier() -> None:
            classifier_started.set()
            await browser_started.wait()

        async def start(
            *,
            on_browser_ready: Callable[[], Awaitable[None]],
        ) -> None:
            browser_started.set()
            await classifier_started.wait()
            await on_browser_ready()

        session._ensure_classifier = AsyncMock(side_effect=prepare_classifier)
        hentaiverse.start = AsyncMock(side_effect=start)
        hentaiverse.__aexit__ = AsyncMock()

        async with asyncio.timeout(1):
            async with session:
                pass

        session._ensure_classifier.assert_awaited_once_with()
        hentaiverse.start.assert_awaited_once_with(
            on_browser_ready=session._on_browser_ready,
        )

    async def test_classifier_failure_after_browser_start_closes_browser(
        self,
    ) -> None:
        classifier_error = RuntimeError("classifier failed")
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(hentaiverse=hentaiverse)
        session._ensure_classifier = AsyncMock(side_effect=classifier_error)
        hentaiverse.start = AsyncMock()
        hentaiverse.__aexit__ = AsyncMock()

        with self.assertRaises(RuntimeError) as raised:
            await session.__aenter__()

        self.assertIs(raised.exception, classifier_error)
        hentaiverse.__aexit__.assert_awaited_once()
        cleanup_type, cleanup_error, cleanup_traceback = (
            hentaiverse.__aexit__.await_args.args
        )
        self.assertIs(cleanup_type, RuntimeError)
        self.assertIs(cleanup_error, classifier_error)
        self.assertIsNotNone(cleanup_traceback)

    async def test_browser_failure_waits_for_classifier_and_collects_its_error(
        self,
    ) -> None:
        release_classifier = asyncio.Event()
        classifier_finished = False
        browser_error = RuntimeError("browser failed")
        classifier_error = LookupError("classifier failed")
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(hentaiverse=hentaiverse)

        async def prepare_classifier() -> None:
            nonlocal classifier_finished
            await release_classifier.wait()
            classifier_finished = True
            raise classifier_error

        session._ensure_classifier = AsyncMock(side_effect=prepare_classifier)
        hentaiverse.start = AsyncMock(side_effect=browser_error)
        hentaiverse.__aexit__ = AsyncMock()

        entering = asyncio.create_task(session.__aenter__())
        await asyncio.sleep(0)
        self.assertFalse(entering.done())

        release_classifier.set()
        with self.assertRaises(RuntimeError) as raised:
            await entering

        self.assertIs(raised.exception, browser_error)
        self.assertTrue(classifier_finished)
        self.assertEqual(
            browser_error.__notes__,
            ["parallel battle session startup also failed: LookupError"],
        )
        hentaiverse.__aexit__.assert_awaited_once()
        cleanup_type, cleanup_error, cleanup_traceback = (
            hentaiverse.__aexit__.await_args.args
        )
        self.assertIs(cleanup_type, RuntimeError)
        self.assertIs(cleanup_error, browser_error)
        self.assertIsNotNone(cleanup_traceback)

    async def test_repeated_startup_cancellation_waits_for_branches_and_cleanup(
        self,
    ) -> None:
        browser_started = asyncio.Event()
        classifier_started = asyncio.Event()
        finish_startup = asyncio.Event()
        cleanup_started = asyncio.Event()
        finish_cleanup = asyncio.Event()
        completed: list[str] = []
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(hentaiverse=hentaiverse)

        async def start(
            *,
            on_browser_ready: Callable[[], Awaitable[None]],
        ) -> None:
            del on_browser_ready
            browser_started.set()
            await finish_startup.wait()
            completed.append("browser")

        async def prepare_classifier() -> None:
            classifier_started.set()
            await finish_startup.wait()
            completed.append("classifier")

        async def close(*_args: object) -> None:
            cleanup_started.set()
            await finish_cleanup.wait()
            completed.append("cleanup")

        session._ensure_classifier = AsyncMock(side_effect=prepare_classifier)
        hentaiverse.start = AsyncMock(side_effect=start)
        hentaiverse.__aexit__ = AsyncMock(side_effect=close)

        entering = asyncio.create_task(session.__aenter__())
        await browser_started.wait()
        await classifier_started.wait()
        entering.cancel("first cancellation")
        await asyncio.sleep(0)
        self.assertFalse(entering.done())

        finish_startup.set()
        await cleanup_started.wait()
        entering.cancel("second cancellation")
        await asyncio.sleep(0)
        self.assertFalse(entering.done())

        finish_cleanup.set()
        with self.assertRaises(asyncio.CancelledError) as raised:
            await entering

        self.assertEqual(str(raised.exception), "first cancellation")
        self.assertCountEqual(completed, ("browser", "classifier", "cleanup"))
        hentaiverse.__aexit__.assert_awaited_once()

    async def test_realm_is_read_from_composed_navigator(self) -> None:
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(hentaiverse=hentaiverse)
        hentaiverse.realm.current = AsyncMock(return_value=Realm.ISEKAI)

        self.assertTrue(await session.is_isekai)

        hentaiverse.realm.current.return_value = Realm.PERSISTENT
        self.assertFalse(await session.is_isekai)

    async def test_attached_session_prepares_without_claiming_browser_lifecycle(
        self,
    ) -> None:
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(
            hentaiverse=hentaiverse,
            auto_accept_dialogs=True,
        )
        events: list[str] = []
        session._ponychart.arm_network_capture = AsyncMock(
            side_effect=lambda: events.append("network-arm")
        )
        session._setup_alert_handler = AsyncMock(
            side_effect=lambda: events.append("attach-hooks")
        )

        with (
            patch.object(
                session_module,
                "preload_ponychart_classifier",
                new=AsyncMock(side_effect=lambda: events.append("preload")),
            ) as preload,
            patch.object(hentaiverse, "start", new=AsyncMock()) as start,
            patch.object(hentaiverse, "__aexit__", new=AsyncMock()) as close,
        ):
            first = await session.prepare_attached()
            second = await session.prepare_attached()

        self.assertIs(first, session)
        self.assertIs(second, session)
        preload.assert_awaited_once_with()
        session._ponychart.arm_network_capture.assert_awaited_once_with()
        session._setup_alert_handler.assert_awaited_once_with()
        self.assertEqual(events, ["network-arm", "attach-hooks", "preload"])
        start.assert_not_awaited()
        close.assert_not_awaited()

    async def test_attaching_browser_hooks_does_not_wait_for_classifier(
        self,
    ) -> None:
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(
            hentaiverse=hentaiverse,
            auto_accept_dialogs=True,
        )
        session._ponychart.arm_network_capture = AsyncMock()
        session._setup_alert_handler = AsyncMock()

        with patch.object(
            session_module,
            "preload_ponychart_classifier",
        ) as preload:
            first = await session.attach_browser_hooks()
            second = await session.attach_browser_hooks()

        self.assertIs(first, session)
        self.assertIs(second, session)
        session._ponychart.arm_network_capture.assert_awaited_once_with()
        session._setup_alert_handler.assert_awaited_once_with()
        preload.assert_not_called()

    async def test_concurrent_attach_installs_browser_hooks_exactly_once(self) -> None:
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(
            hentaiverse=hentaiverse,
            auto_accept_dialogs=True,
        )
        arm_started = asyncio.Event()
        release_arm = asyncio.Event()

        async def arm() -> None:
            arm_started.set()
            await release_arm.wait()

        session._ponychart.arm_network_capture = AsyncMock(side_effect=arm)
        session._setup_alert_handler = AsyncMock()
        first = asyncio.create_task(session.attach_browser_hooks())
        await arm_started.wait()
        second = asyncio.create_task(session.attach_browser_hooks())
        await asyncio.sleep(0)

        session._ponychart.arm_network_capture.assert_awaited_once_with()
        self.assertFalse(second.done())
        release_arm.set()
        attached = await asyncio.gather(first, second)

        self.assertEqual(attached, [session, session])
        session._ponychart.arm_network_capture.assert_awaited_once_with()
        session._setup_alert_handler.assert_awaited_once_with()

    async def test_exit_dominates_overlapping_browser_hook_installation(self) -> None:
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(hentaiverse=hentaiverse)
        arm_started = asyncio.Event()
        release_arm = asyncio.Event()

        async def arm() -> None:
            arm_started.set()
            await release_arm.wait()

        session._ponychart.arm_network_capture = AsyncMock(side_effect=arm)
        session._ponychart.close = AsyncMock()
        hentaiverse.__aexit__ = AsyncMock()
        attaching = asyncio.create_task(session.attach_browser_hooks())
        await arm_started.wait()
        exiting = asyncio.create_task(session.__aexit__(None, None, None))
        await asyncio.sleep(0)

        with self.assertRaisesRegex(RuntimeError, "hooks are closing"):
            await session.attach_browser_hooks()

        release_arm.set()
        await attaching
        await exiting

        session._ponychart.close.assert_awaited_once_with()
        self.assertFalse(session._browser_hooks_initialized)

    async def test_exit_disarms_network_handlers_before_browser_handoff(self) -> None:
        events: list[str] = []
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(hentaiverse=hentaiverse)
        session._browser_hooks_initialized = True
        session._ponychart.close = AsyncMock(
            side_effect=lambda: events.append("network-disarm")
        )
        hentaiverse.__aexit__ = AsyncMock(
            side_effect=lambda *_args: events.append("browser-handoff")
        )

        await session.__aexit__(None, None, None)

        self.assertEqual(events, ["network-disarm", "browser-handoff"])
        self.assertFalse(session._browser_hooks_initialized)

    async def test_ring_of_blood_operations_delegate_to_shared_launcher(self) -> None:
        hentaiverse = HentaiVerseSession(browser=HVDriver(headless=True))
        session = BattleSession(hentaiverse=hentaiverse)
        option = RingOfBloodOption(112, "Triple Trio and the Tree", 1.0, 10)
        snapshot = RingOfBloodSnapshot(
            20,
            (option,),
            (RingOfBloodChallenge("Triple Trio and the Tree", 1.0, 10, option),),
        )
        session._launcher.goto_ring_of_blood = AsyncMock(return_value=True)
        session._launcher.inspect_ring_of_blood = AsyncMock(return_value=snapshot)
        session._launcher.start_ring_of_blood = AsyncMock(
            return_value=RingOfBloodStartOutcome.SUBMITTED
        )

        self.assertTrue(await session.goto_ring_of_blood(expected_realm=Realm.ISEKAI))
        self.assertIs(await session.inspect_ring_of_blood(), snapshot)
        self.assertIs(
            await session.start_ring_of_blood(
                option,
                expected_before=snapshot,
                expected_realm=Realm.ISEKAI,
            ),
            RingOfBloodStartOutcome.SUBMITTED,
        )

        session._launcher.goto_ring_of_blood.assert_awaited_once_with(
            expected_realm=Realm.ISEKAI
        )
        session._launcher.inspect_ring_of_blood.assert_awaited_once_with()
        session._launcher.start_ring_of_blood.assert_awaited_once_with(
            option,
            expected_before=snapshot,
            expected_realm=Realm.ISEKAI,
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


if __name__ == "__main__":
    unittest.main()

import ast
import asyncio
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from hvbrowser import HVDriver

import hvbattle.hv_battle_ponychart as ponychart_module
import hvbattle.session as session_module
from hvbattle import (
    ArenaOption,
    BattleCompleted,
    BattleInterruptedError,
    BattleRunner,
    BattleSession,
    BattleStopped,
    GrindfestOption,
    PonyChartResolutionError,
    TurnDecision,
)
from hvbattle.hv_battle_buff_manager import BuffManager
from hvbattle.hv_battle_observer_pattern import BattleDashboard, LogEntry
from hvbattle.hv_battle_ponychart import PonyChart


class BattleSessionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_startup_preloads_ponychart_before_browser(self) -> None:
        session = BattleSession(headless=True, auto_accept_dialogs=True)
        events: list[str] = []
        session._setup_alert_handler = AsyncMock()
        session._initialize_battle_components = Mock()

        with (
            patch.object(
                session_module,
                "preload_ponychart_classifier",
                side_effect=lambda: events.append("preload"),
            ),
            patch.object(
                HVDriver,
                "_init_browser",
                new=AsyncMock(side_effect=lambda: events.append("browser")),
            ),
        ):
            await session._init_browser()

        self.assertEqual(events, ["preload", "browser"])
        session._setup_alert_handler.assert_awaited_once_with()

    def test_snapshot_is_unavailable_before_first_prepared_turn(self) -> None:
        session = object.__new__(BattleSession)
        session.battle_dashboard = Mock()
        session.battle_dashboard.snap = None

        with self.assertRaisesRegex(RuntimeError, "before prepare_turn"):
            _ = session.snapshot

    async def test_cast_on_fails_closed_when_skill_is_unavailable(self) -> None:
        session = object.__new__(BattleSession)
        session.select_targeted_skill = AsyncMock(return_value=False)
        session.attack_monster = AsyncMock()

        acted = await BattleSession.attack_monster_by_skill(session, 3, "imperil")

        self.assertFalse(acted)
        session.select_targeted_skill.assert_awaited_once_with("imperil")
        session.attack_monster.assert_not_awaited()

    async def test_challenge_presence_is_checked_before_dashboard_parse(self) -> None:
        session = object.__new__(BattleSession)
        session.is_ponychart_present = AsyncMock(return_value=True)
        session._has_next_floor_control = AsyncMock()
        session._has_battle_marker = AsyncMock()
        session.battle_dashboard = Mock()
        session.battle_dashboard.inspect = AsyncMock(
            side_effect=AssertionError("ordinary parser ran before PonyChart")
        )

        active = await BattleSession.is_in_battle(session)

        self.assertTrue(active)
        session.battle_dashboard.inspect.assert_not_awaited()
        session._has_next_floor_control.assert_not_awaited()
        session._has_battle_marker.assert_not_awaited()

    async def test_non_battle_transition_does_not_increment_turn(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = 4
        session.round = 3
        session._has_battle_marker = AsyncMock(return_value=False)

        prepared = await BattleSession.prepare_turn(session)

        self.assertIsNone(prepared)
        self.assertEqual(session.turn, 4)

    async def test_final_round_without_monsters_records_completion_evidence(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.turn = 0
        session.round = 1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._has_next_floor_control = AsyncMock(return_value=False)
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_dashboard = Mock()
        session.battle_dashboard.update = AsyncMock()
        session.battle_dashboard.overview_monsters.alive_monster = []
        session.battle_dashboard.log_entries.current_round = 5
        session.battle_dashboard.log_entries.total_round = 5

        prepared = await BattleSession.prepare_turn(session)

        self.assertIsNone(prepared)
        self.assertTrue(session.battle_completion_observed)
        self.assertEqual(session.turn, 0)

    async def test_skill_buff_action_never_uses_an_implicit_mystic_gem(self) -> None:
        manager = object.__new__(BuffManager)
        manager.is_action_needed = Mock(return_value=True)
        manager._item_provider = Mock()
        manager._item_provider.use = AsyncMock()
        manager._cast_skill = AsyncMock(return_value=True)

        acted = await BuffManager.apply_buff(manager, "regen", force=False)

        self.assertTrue(acted)
        manager._item_provider.use.assert_not_awaited()
        manager._cast_skill.assert_awaited_once_with("regen")


class _FakeSession:
    def __init__(self, *, active: bool) -> None:
        self._active = active
        self.turn = -1
        self.current_round = 1
        self.total_rounds = 1
        self.battle_completion_observed = False
        self.repairequipment = AsyncMock()
        self.recoverstamina = AsyncMock()
        self.goto_arena = AsyncMock()
        self.goto_grindfest = AsyncMock()

    @property
    async def is_isekai(self) -> bool:
        return False

    async def is_in_battle(self) -> bool:
        return self._active

    def reset_battle_tracking(self) -> None:
        self.turn = -1
        self.battle_completion_observed = False

    async def prepare_turn(self) -> tuple[str, ...] | None:
        if not self._active:
            return None
        self.turn += 1
        return ()

    async def resolve_ponychart(self) -> bool:
        return False


class BattleRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_session_probe_failure_is_interrupted(self) -> None:
        session = _FakeSession(active=True)
        session.resolve_ponychart = AsyncMock(
            side_effect=PonyChartResolutionError("challenge remains")
        )
        strategy = Mock()
        strategy.take_turn = AsyncMock()

        with self.assertRaises(BattleInterruptedError) as raised:
            await BattleRunner(session, strategy).run_current()  # type: ignore[arg-type]

        self.assertIsInstance(raised.exception.__cause__, PonyChartResolutionError)
        strategy.take_turn.assert_not_awaited()

    async def test_no_active_battle_never_calls_strategy_or_campaign_actions(
        self,
    ) -> None:
        session = _FakeSession(active=False)
        strategy = Mock()
        strategy.take_turn = AsyncMock()

        result = await BattleRunner(session, strategy).run_current()  # type: ignore[arg-type]

        self.assertIsNone(result)
        strategy.take_turn.assert_not_awaited()
        session.repairequipment.assert_not_awaited()
        session.recoverstamina.assert_not_awaited()
        session.goto_arena.assert_not_awaited()
        session.goto_grindfest.assert_not_awaited()

    async def test_active_battle_returns_immutable_completion_summary(self) -> None:
        session = _FakeSession(active=True)
        strategy = Mock()
        strategy.on_battle_started = AsyncMock()

        async def finish(_session: object) -> TurnDecision:
            session._active = False
            session.battle_completion_observed = True
            return TurnDecision.ACTED

        strategy.take_turn = AsyncMock(side_effect=finish)
        result = await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertEqual(
            result,
            BattleCompleted(
                is_isekai=False,
                decision_count=1,
                final_round=1,
                total_rounds=1,
            ),
        )
        strategy.on_battle_started.assert_awaited_once_with(session)
        strategy.take_turn.assert_awaited_once_with(session)

    async def test_ponychart_runs_before_optional_strategy_lifecycle(self) -> None:
        session = _FakeSession(active=True)
        events: list[str] = []
        challenge_count = 0

        async def resolve_ponychart() -> bool:
            nonlocal challenge_count
            challenge_count += 1
            events.append("ponychart")
            if challenge_count == 1:
                return True
            return False

        session.resolve_ponychart = resolve_ponychart  # type: ignore[method-assign]
        strategy = Mock()
        strategy.on_battle_started = AsyncMock(
            side_effect=lambda _session: events.append("started")
        )

        def finish(_session: object) -> TurnDecision:
            events.append("strategy")
            session._active = False
            session.battle_completion_observed = True
            return TurnDecision.ACTED

        strategy.take_turn = AsyncMock(side_effect=finish)

        await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertLess(events.index("ponychart"), events.index("started"))
        self.assertLess(events.index("started"), events.index("strategy"))
        self.assertEqual(events.count("strategy"), 1)

    async def test_page_exit_without_completion_evidence_is_interrupted(self) -> None:
        session = _FakeSession(active=True)
        strategy = Mock()

        async def leave_page(_session: object) -> TurnDecision:
            session._active = False
            return TurnDecision.ACTED

        strategy.take_turn = AsyncMock(side_effect=leave_page)

        with self.assertRaises(BattleInterruptedError):
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                transition_checks=2,
                sleep=AsyncMock(),
            ).run_current()

        strategy.take_turn.assert_awaited_once_with(session)

    async def test_strategy_can_return_control_without_claiming_completion(
        self,
    ) -> None:
        session = _FakeSession(active=True)
        strategy = Mock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.STOP)

        result = await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertEqual(result, BattleStopped(False, 1, 1, 1))
        self.assertTrue(session._active)

    async def test_pause_polling_continues_to_service_ponychart(self) -> None:
        session = _FakeSession(active=True)
        gate = asyncio.Event()
        challenge_checks = 0

        async def resolve_ponychart() -> bool:
            nonlocal challenge_checks
            challenge_checks += 1
            return False

        async def wait_if_paused() -> None:
            await gate.wait()

        session.resolve_ponychart = resolve_ponychart  # type: ignore[method-assign]
        strategy = Mock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.STOP)
        runner = BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            wait_if_paused=wait_if_paused,
            challenge_poll_interval=0.001,
            sleep=AsyncMock(),
        )

        task = asyncio.create_task(runner.run_current())
        await asyncio.sleep(0.01)
        self.assertGreater(challenge_checks, 1)

        gate.set()
        await task

    async def test_pause_after_turn_parse_blocks_strategy_action(self) -> None:
        session = _FakeSession(active=True)
        release = asyncio.Event()
        post_parse_gate_entered = asyncio.Event()
        gate_calls = 0

        async def wait_if_paused() -> None:
            nonlocal gate_calls
            gate_calls += 1
            if gate_calls == 3:
                post_parse_gate_entered.set()
                await release.wait()

        strategy = Mock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.STOP)
        runner = BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            wait_if_paused=wait_if_paused,
            challenge_poll_interval=0.001,
            sleep=AsyncMock(),
        )

        task = asyncio.create_task(runner.run_current())
        await post_parse_gate_entered.wait()
        strategy.take_turn.assert_not_awaited()

        release.set()
        await task
        strategy.take_turn.assert_awaited_once_with(session)

    async def test_false_submission_result_is_not_reported_as_started(self) -> None:
        session = object.__new__(BattleSession)
        session.page = Mock()
        arena_url = "https://hentaiverse.org/?s=Battle&ss=ar"
        session._get_path_prefix = AsyncMock(return_value="")
        session.page.evaluate = AsyncMock(side_effect=[arena_url, False])

        started = await BattleSession.start_arena(session, ArenaOption(12))

        self.assertFalse(started)

    async def test_arena_submission_exception_propagates_as_unknown(self) -> None:
        session = object.__new__(BattleSession)
        session.page = Mock()
        arena_url = "https://hentaiverse.org/?s=Battle&ss=ar"
        session._get_path_prefix = AsyncMock(return_value="")
        session.page.evaluate = AsyncMock(
            side_effect=[arena_url, ValueError("navigation destroyed context")]
        )

        with self.assertRaisesRegex(ValueError, "destroyed context"):
            await BattleSession.start_arena(session, ArenaOption(12))

    async def test_arena_options_are_returned_without_selecting_the_last(self) -> None:
        session = object.__new__(BattleSession)
        session.page = Mock()
        session.page.evaluate = AsyncMock(
            return_value=["init_battle(12, 34)", "init_battle(56, 78, 'token')"]
        )

        options = await BattleSession.list_arena_options(session)

        self.assertEqual(options, (ArenaOption(12), ArenaOption(56, "token")))

    async def test_grindfest_options_are_returned_without_selecting_one(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = Mock()
        session.page.evaluate = AsyncMock(
            return_value=["init_battle(12)", "not-an-option", "init_battle(56)"]
        )

        options = await BattleSession.list_grindfest_options(session)

        self.assertEqual(options, (GrindfestOption(12), GrindfestOption(56)))

    async def test_grindfest_submission_exception_propagates_as_unknown(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = Mock()
        grindfest_url = "https://hentaiverse.org/?s=Battle&ss=gr"
        session._get_path_prefix = AsyncMock(return_value="")
        session.page.evaluate = AsyncMock(
            side_effect=[grindfest_url, ValueError("bad form")]
        )

        with self.assertRaisesRegex(ValueError, "bad form"):
            await BattleSession.start_grindfest(session, GrindfestOption(12))


class PonyChartPreloadTests(unittest.TestCase):
    def test_preload_loads_model_once_before_prediction(self) -> None:
        original_predictor = ponychart_module._predict
        fake_module = types.ModuleType("ponychart_classifier")
        preload = Mock()

        def predict(_path: str) -> object:
            raise AssertionError("Prediction must not run during preload")

        fake_module.preload = preload
        fake_module.predict = predict
        ponychart_module._predict = None
        try:
            with patch.dict(sys.modules, {"ponychart_classifier": fake_module}):
                ponychart_module.preload_ponychart_classifier()
                ponychart_module.preload_ponychart_classifier()
        finally:
            ponychart_module._predict = original_predictor

        preload.assert_called_once_with()


class PonyChartResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresolved_challenge_raises_instead_of_reporting_success(
        self,
    ) -> None:
        driver = Mock()
        driver.headless = True
        driver.page = Mock()
        driver.page.xpath = AsyncMock(return_value=[])
        driver.page.select = AsyncMock(side_effect=LookupError("no fallback"))
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)
        challenge._save_pony_chart_image = AsyncMock(return_value="pony.png")
        challenge._auto_answer = AsyncMock()

        with (
            patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
            self.assertRaises(PonyChartResolutionError),
        ):
            await challenge.check()


class BattleLogTests(unittest.TestCase):
    def test_empty_refresh_clears_previous_log_delta(self) -> None:
        entry = LogEntry()
        entry.update(SimpleNamespace(log=SimpleNamespace(lines=["first"])))

        entry.update(SimpleNamespace(log=SimpleNamespace(lines=[])))

        self.assertEqual(entry.current_lines, [])

    def test_dashboard_reset_discards_previous_battle_log_state(self) -> None:
        dashboard = BattleDashboard(Mock())
        previous = dashboard.log_entries
        previous.prev_lines.append("old battle")
        dashboard.overview_monsters.alive_monster = [2]

        dashboard.reset()

        self.assertIsNot(dashboard.log_entries, previous)
        self.assertEqual(list(dashboard.log_entries.prev_lines), [])
        self.assertEqual(dashboard.overview_monsters.alive_monster, [])
        self.assertNotIn(previous, dashboard.battle_subject._observers)
        self.assertIn(dashboard.log_entries, dashboard.battle_subject._observers)


class ArchitectureTests(unittest.TestCase):
    def test_hvbattle_uses_hvbrowser_boundary_not_hbrowser_directly(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbattle"
        imported_modules: set[str] = set()

        for source_file in source_root.glob("*.py"):
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.add(node.module)

        self.assertFalse(
            any(
                name == "hbrowser" or name.startswith("hbrowser.")
                for name in imported_modules
            )
        )

    def test_session_has_no_campaign_or_flee_operations(self) -> None:
        forbidden = {
            "battle",
            "repairequipment",
            "recoverstamina",
            "flee",
            "_ensure_stamina",
            "_try_auto_start_battle",
            "go_next_arena",
            "go_grindfest",
        }

        self.assertTrue(forbidden.isdisjoint(BattleSession.__dict__))
        self.assertTrue(hasattr(BattleSession, "list_arena_options"))
        self.assertTrue(hasattr(BattleSession, "start_arena"))
        self.assertTrue(hasattr(BattleSession, "list_grindfest_options"))
        self.assertTrue(hasattr(BattleSession, "start_grindfest"))


if __name__ == "__main__":
    unittest.main()

import ast
import asyncio
import json
import shutil
import subprocess
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
    BattleActionOutcomeUnknownError,
    BattleCompleted,
    BattleInterruptedError,
    BattleRunner,
    BattleSession,
    BattleStopped,
    BattleTurnPhase,
    BattleTurnState,
    GrindfestOption,
    PonyChartResolutionError,
    TurnDecision,
)
from hvbattle.battle_launcher import BattleLauncher
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

    async def test_dialog_log_records_category_without_raw_server_message(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = Mock()
        session.page.add_handler = Mock()
        session.page.send = AsyncMock()
        session._last_dialog_category = None
        secret_message = "Server communication failed: response-token-should-not-log"

        with patch.object(session_module.logger, "warning") as warning:
            await BattleSession._setup_alert_handler(session)
            handler = session.page.add_handler.call_args.args[1]
            await handler(SimpleNamespace(message=secret_message))

        self.assertEqual(
            session._last_dialog_category,
            "server-communication-failed",
        )
        self.assertNotIn(secret_message, str(warning.call_args))
        session.page.send.assert_awaited_once()

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

    async def test_armed_skill_with_missing_target_interrupts(self) -> None:
        session = object.__new__(BattleSession)
        session.select_targeted_skill = AsyncMock(return_value=True)
        session.attack_monster = AsyncMock(return_value=False)

        with self.assertRaisesRegex(BattleInterruptedError, "Target disappeared"):
            await BattleSession.attack_monster_by_skill(session, 3, "imperil")

    async def test_challenge_presence_is_checked_before_dashboard_parse(self) -> None:
        session = object.__new__(BattleSession)
        session.is_ponychart_present = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock()
        session._has_battle_marker = AsyncMock()
        session.battle_dashboard = Mock()
        session.battle_dashboard.inspect = AsyncMock(
            side_effect=AssertionError("ordinary parser ran before PonyChart")
        )

        active = await BattleSession.is_in_battle(session)

        self.assertTrue(active)
        session.battle_dashboard.inspect.assert_not_awaited()
        session._read_battle_phase.assert_not_awaited()
        session._has_battle_marker.assert_not_awaited()

    async def test_non_battle_transition_does_not_increment_turn(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = 4
        session.round = 3
        session._has_battle_marker = AsyncMock(return_value=False)

        prepared = await BattleSession.prepare_turn(session)

        self.assertIsNone(prepared)
        self.assertEqual(session.turn, 4)

    async def test_resumed_turn_logs_unknown_round_metadata_once(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = -1
        session.round = -1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(return_value="active")
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_dashboard = Mock()
        session.battle_dashboard.update = AsyncMock()
        session.battle_dashboard.snap = SimpleNamespace(warnings=[])
        session.battle_dashboard.overview_monsters.alive_monster = [0]
        session.battle_dashboard.log_entries.current_round = 0
        session.battle_dashboard.log_entries.total_round = 0
        session.battle_dashboard.log_entries.current_lines = ["You hit a monster."]

        with self.assertLogs("hvbattle.session", level="INFO") as captured:
            await BattleSession.prepare_turn(session)
            await BattleSession.prepare_turn(session)

        output = "\n".join(captured.output)
        self.assertEqual(output.count("Round metadata is unavailable"), 1)
        self.assertIn("Round   ? / ?", output)
        self.assertNotIn("Round   0 / 0", output)

    async def test_final_round_numbers_without_dom_marker_are_invalid(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.turn = 0
        session.round = 1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(return_value="active")
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_dashboard = Mock()
        session.battle_dashboard.update = AsyncMock()
        session.battle_dashboard.snap = SimpleNamespace(warnings=[])
        session.battle_dashboard.overview_monsters.alive_monster = []
        session.battle_dashboard.log_entries.current_round = 5
        session.battle_dashboard.log_entries.total_round = 5

        with self.assertRaisesRegex(TimeoutError, "no monsters"):
            await BattleSession.prepare_turn(session)

        self.assertFalse(session.battle_completion_observed)
        self.assertEqual(session.turn, 0)

    async def test_completion_pane_records_positive_completion_evidence(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.turn = 0
        session.round = 1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(return_value="complete")
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_dashboard = Mock()
        session.battle_dashboard.update = AsyncMock()
        session.battle_dashboard.snap = SimpleNamespace(warnings=[])
        session.battle_dashboard.overview_monsters.alive_monster = []
        session.battle_dashboard.log_entries.current_round = 5
        session.battle_dashboard.log_entries.total_round = 5

        prepared = await BattleSession.prepare_turn(session)

        self.assertIsNone(prepared)
        self.assertTrue(session.battle_completion_observed)
        self.assertEqual(session.turn, 0)
        session.battle_dashboard.update.assert_not_awaited()

    async def test_next_floor_marker_bypasses_parser_failure(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = 2
        session.round = 1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(return_value="next-floor")
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_dashboard = Mock()
        session.battle_dashboard.update = AsyncMock(
            side_effect=AssertionError("parser must not run during transition")
        )

        prepared = await BattleSession.prepare_turn(session)

        self.assertEqual(prepared, ())
        self.assertEqual(session.turn, 3)
        session.battle_dashboard.update.assert_not_awaited()

    async def test_parser_error_reconciles_completion_that_appeared(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = 2
        session.round = 1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(side_effect=["active", "complete"])
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_dashboard = Mock()
        session.battle_dashboard.update = AsyncMock(
            side_effect=TimeoutError("monsters disappeared during parse")
        )

        prepared = await BattleSession.prepare_turn(session)

        self.assertIsNone(prepared)
        self.assertTrue(session.battle_completion_observed)
        session.battle_dashboard.update.assert_awaited_once()

    async def test_finish_image_marker_is_explicit(self) -> None:
        session = object.__new__(BattleSession)
        session.page = Mock()
        session.page.evaluate = AsyncMock(return_value="complete")

        complete = await BattleSession._has_battle_completion_marker(session)

        self.assertTrue(complete)
        script = session.page.evaluate.await_args.args[0]
        self.assertIn("finishbattle.png", script)
        self.assertLess(script.index("finishbattle.png"), script.index("btcp"))

    def test_phase_script_executes_final_priority_when_btcp_coexists(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required to execute the production phase script")
        harness = r"""
const fs = require("node:fs");
const script = fs.readFileSync(0, "utf8");

const run = (finishPresent, nextFloorPresent) => {
    const lookups = [];
    globalThis.document = {
        getElementById(id) {
            lookups.push(id);
            if (id === "pane_completion") {
                return {
                    querySelector(selector) {
                        return finishPresent
                            && selector.includes("finishbattle.png") ? {} : null;
                    },
                };
            }
            if (id === "btcp" && nextFloorPresent) return {};
            return null;
        },
    };
    return {phase: eval(script), lookups};
};

console.log(JSON.stringify({
    both: run(true, true),
    nextFloor: run(false, true),
    active: run(false, false),
}));
"""
        completed = subprocess.run(
            [node, "-e", harness],
            input=session_module._BATTLE_PHASE_JS,
            capture_output=True,
            text=True,
            check=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(
            result["both"],
            {
                "phase": "complete",
                "lookups": ["pane_completion"],
            },
        )
        self.assertEqual(result["nextFloor"]["phase"], "next-floor")
        self.assertEqual(result["active"]["phase"], "active")

    async def test_final_control_wins_when_btcp_is_also_present(self) -> None:
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session.is_ponychart_present = AsyncMock(return_value=False)
        session._read_battle_phase = AsyncMock(return_value="complete")

        active = await BattleSession.is_in_battle(session)

        self.assertFalse(active)
        self.assertTrue(session.battle_completion_observed)

    async def test_final_completion_ack_uses_dedicated_exact_selector(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session._completion_observed = True
        session.element_action_manager = Mock()
        session.element_action_manager.click_and_wait_battle_exit_locator = AsyncMock()

        await BattleSession.acknowledge_battle_completion(
            session,
            expected_is_isekai=True,
        )

        (
            session.element_action_manager.click_and_wait_battle_exit_locator
        ).assert_awaited_once_with(
            '#pane_completion img[src*="finishbattle.png"]',
            expected_is_isekai=True,
        )

    async def test_final_completion_ack_requires_prior_observation(self) -> None:
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session.element_action_manager = Mock()
        session.element_action_manager.click_and_wait_battle_exit_locator = AsyncMock()

        with self.assertRaises(BattleActionOutcomeUnknownError):
            await BattleSession.acknowledge_battle_completion(
                session,
                expected_is_isekai=False,
            )

        (
            session.element_action_manager.click_and_wait_battle_exit_locator
        ).assert_not_awaited()

    async def test_inspect_error_reconciles_completion_phase(self) -> None:
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session.is_ponychart_present = AsyncMock(return_value=False)
        session._read_battle_phase = AsyncMock(side_effect=["active", "complete"])
        session._has_battle_marker = AsyncMock(return_value=True)
        session.battle_dashboard = Mock()
        session.battle_dashboard.inspect = AsyncMock(
            side_effect=ValueError("monsters disappeared during inspect")
        )

        active = await BattleSession.is_in_battle(session)

        self.assertFalse(active)
        self.assertTrue(session.battle_completion_observed)
        session.battle_dashboard.inspect.assert_awaited_once()

    async def test_inspect_timeout_reconciles_next_floor_phase(self) -> None:
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session.is_ponychart_present = AsyncMock(return_value=False)
        session._read_battle_phase = AsyncMock(side_effect=["active", "next-floor"])
        session._has_battle_marker = AsyncMock(return_value=True)
        session.battle_dashboard = Mock()
        session.battle_dashboard.inspect = AsyncMock(
            side_effect=TimeoutError("inspect raced with transition")
        )

        active = await BattleSession.is_in_battle(session)

        self.assertTrue(active)
        session.battle_dashboard.inspect.assert_awaited_once()

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
        self.go_next_floor = AsyncMock(return_value=True)
        self.acknowledge_battle_completion = AsyncMock()

    @property
    async def is_isekai(self) -> bool:
        return False

    async def is_in_battle(self) -> bool:
        return self._active

    def reset_battle_tracking(self) -> None:
        self.turn = -1
        self.battle_completion_observed = False

    async def prepare_turn_state(self) -> BattleTurnState:
        if not self._active:
            return BattleTurnState(BattleTurnPhase.ABSENT)
        self.turn += 1
        return BattleTurnState(BattleTurnPhase.ACTIVE)

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
        session.acknowledge_battle_completion.assert_not_awaited()

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
        session.acknowledge_battle_completion.assert_awaited_once_with(
            expected_is_isekai=False
        )

    async def test_unknown_round_completion_log_does_not_report_round_zero(
        self,
    ) -> None:
        session = _FakeSession(active=True)
        session.current_round = 0
        session.total_rounds = 0
        strategy = Mock()

        async def finish(_session: object) -> TurnDecision:
            session._active = False
            session.battle_completion_observed = True
            return TurnDecision.ACTED

        strategy.take_turn = AsyncMock(side_effect=finish)

        with self.assertLogs("hvbattle.runner", level="INFO") as captured:
            result = await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertIsInstance(result, BattleCompleted)
        output = "\n".join(captured.output)
        self.assertIn("round=<unknown>", output)
        self.assertNotIn("final_round=0", output)

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

    async def test_synchronous_lifecycle_hook_is_rejected(self) -> None:
        session = _FakeSession(active=True)
        strategy = Mock()
        strategy.on_battle_started = Mock(return_value=None)
        strategy.take_turn = AsyncMock()

        with self.assertRaises(BattleInterruptedError) as raised:
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertIsInstance(raised.exception.__cause__, TypeError)
        strategy.take_turn.assert_not_awaited()

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

    async def test_unknown_action_outcome_is_never_retried(self) -> None:
        session = _FakeSession(active=True)
        session.prepare_turn_state = AsyncMock(
            return_value=BattleTurnState(BattleTurnPhase.ACTIVE)
        )
        strategy = Mock()
        action_error = BattleActionOutcomeUnknownError("receipt missing")
        strategy.take_turn = AsyncMock(side_effect=action_error)

        with self.assertRaises(BattleInterruptedError) as raised:
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertIs(raised.exception.__cause__, action_error)
        session.prepare_turn_state.assert_awaited_once()
        strategy.take_turn.assert_awaited_once_with(session)

    async def test_read_timeout_is_attempted_three_times(self) -> None:
        session = _FakeSession(active=True)
        session.prepare_turn_state = AsyncMock(
            side_effect=TimeoutError("read timed out")
        )
        strategy = Mock()
        strategy.take_turn = AsyncMock()
        retry_sleep = AsyncMock()

        with self.assertRaises(BattleInterruptedError) as raised:
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                timeout_retries=3,
                sleep=retry_sleep,
            ).run_current()

        self.assertIsInstance(raised.exception.__cause__, TimeoutError)
        self.assertEqual(session.prepare_turn_state.await_count, 3)
        self.assertEqual(retry_sleep.await_count, 2)
        strategy.take_turn.assert_not_awaited()

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
        session.acknowledge_battle_completion.assert_not_awaited()

    async def test_explicit_completion_state_never_calls_strategy(self) -> None:
        session = _FakeSession(active=True)
        session.prepare_turn_state = AsyncMock(
            return_value=BattleTurnState(BattleTurnPhase.COMPLETE)
        )
        strategy = Mock()
        strategy.take_turn = AsyncMock()

        result = await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertEqual(result, BattleCompleted(False, 0, 1, 1))
        strategy.take_turn.assert_not_awaited()
        session.acknowledge_battle_completion.assert_awaited_once_with(
            expected_is_isekai=False
        )

    async def test_completion_summary_is_captured_before_ack_navigation(
        self,
    ) -> None:
        session = _FakeSession(active=True)
        session.current_round = 7
        session.total_rounds = 10
        session.prepare_turn_state = AsyncMock(
            return_value=BattleTurnState(BattleTurnPhase.COMPLETE)
        )

        async def navigate_after_ack(**_kwargs: object) -> None:
            session.current_round = 0
            session.total_rounds = 0

        session.acknowledge_battle_completion = AsyncMock(
            side_effect=navigate_after_ack
        )
        strategy = Mock()
        strategy.take_turn = AsyncMock()

        result = await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertEqual(result, BattleCompleted(False, 0, 7, 10))
        session.acknowledge_battle_completion.assert_awaited_once_with(
            expected_is_isekai=False
        )

    async def test_unknown_final_ack_is_terminal_and_never_retried(self) -> None:
        session = _FakeSession(active=True)
        session.prepare_turn_state = AsyncMock(
            return_value=BattleTurnState(BattleTurnPhase.COMPLETE)
        )
        ack_error = BattleActionOutcomeUnknownError("exit evidence missing")
        session.acknowledge_battle_completion = AsyncMock(side_effect=ack_error)
        strategy = Mock()
        strategy.take_turn = AsyncMock()

        with self.assertRaises(BattleInterruptedError) as raised:
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertIs(raised.exception.__cause__, ack_error)
        session.acknowledge_battle_completion.assert_awaited_once_with(
            expected_is_isekai=False
        )
        strategy.take_turn.assert_not_awaited()

    async def test_challenge_state_is_refreshed_before_strategy(self) -> None:
        session = _FakeSession(active=True)
        session.prepare_turn_state = AsyncMock(
            side_effect=[
                BattleTurnState(BattleTurnPhase.CHALLENGE),
                BattleTurnState(BattleTurnPhase.ACTIVE),
            ]
        )
        strategy = Mock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.STOP)

        result = await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertEqual(result, BattleStopped(False, 0, 1, 1))
        self.assertEqual(session.prepare_turn_state.await_count, 2)
        strategy.take_turn.assert_awaited_once_with(session)

    async def test_next_floor_is_progressed_without_strategy_policy(self) -> None:
        session = _FakeSession(active=True)
        session.prepare_turn_state = AsyncMock(
            side_effect=[
                BattleTurnState(BattleTurnPhase.NEXT_FLOOR),
                BattleTurnState(BattleTurnPhase.COMPLETE),
            ]
        )
        strategy = Mock()
        strategy.take_turn = AsyncMock()

        result = await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertIsInstance(result, BattleCompleted)
        session.go_next_floor.assert_awaited_once_with()
        session.acknowledge_battle_completion.assert_awaited_once_with(
            expected_is_isekai=False
        )
        strategy.take_turn.assert_not_awaited()

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
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client)
        arena_url = "https://hentaiverse.org/?s=Battle&ss=ar"
        launcher._path_prefix = AsyncMock(return_value="")
        client.page.evaluate = AsyncMock(side_effect=[arena_url, False])

        started = await launcher.start_arena(ArenaOption(12))

        self.assertFalse(started)

    async def test_arena_submission_exception_propagates_as_unknown(self) -> None:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client)
        arena_url = "https://hentaiverse.org/?s=Battle&ss=ar"
        launcher._path_prefix = AsyncMock(return_value="")
        client.page.evaluate = AsyncMock(
            side_effect=[arena_url, ValueError("navigation destroyed context")]
        )

        with self.assertRaisesRegex(ValueError, "destroyed context"):
            await launcher.start_arena(ArenaOption(12))

    async def test_arena_options_are_returned_without_selecting_the_last(self) -> None:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client)
        client.page.evaluate = AsyncMock(
            return_value=["init_battle(12, 34)", "init_battle(56, 78, 'token')"]
        )

        options = await launcher.list_arena_options()

        self.assertEqual(options, (ArenaOption(12), ArenaOption(56, "token")))

    async def test_malformed_arena_option_log_does_not_expose_token(self) -> None:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client)
        secret = "secret-battle-token"
        malformed = f"init_battle(bad, 34, '{secret}')"
        client.page.evaluate = AsyncMock(return_value=[malformed])

        with patch("hvbattle.battle_launcher.logger") as launcher_logger:
            options = await launcher.list_arena_options()

        self.assertEqual(options, ())
        launcher_logger.debug.assert_called_once_with(
            "Arena action did not match expected shape: length=%d",
            len(malformed),
        )
        self.assertNotIn(secret, repr(launcher_logger.method_calls))

    async def test_grindfest_options_are_returned_without_selecting_one(
        self,
    ) -> None:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client)
        client.page.evaluate = AsyncMock(
            return_value=["init_battle(12)", "not-an-option", "init_battle(56)"]
        )

        options = await launcher.list_grindfest_options()

        self.assertEqual(options, (GrindfestOption(12), GrindfestOption(56)))

    async def test_grindfest_submission_exception_propagates_as_unknown(
        self,
    ) -> None:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client)
        grindfest_url = "https://hentaiverse.org/?s=Battle&ss=gr"
        launcher._path_prefix = AsyncMock(return_value="")
        client.page.evaluate = AsyncMock(
            side_effect=[grindfest_url, ValueError("bad form")]
        )

        with self.assertRaisesRegex(ValueError, "bad form"):
            await launcher.start_grindfest(GrindfestOption(12))


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
    def test_unknown_round_metadata_is_rendered_as_unknown(self) -> None:
        session = object.__new__(BattleSession)
        session.battle_dashboard = Mock()
        session.battle_dashboard.log_entries.current_round = 0
        session.battle_dashboard.log_entries.total_round = 0

        self.assertEqual(session._round_progress_text(), "Round   ? / ?  ")

    def test_observed_round_metadata_is_rendered_numerically(self) -> None:
        session = object.__new__(BattleSession)
        session.battle_dashboard = Mock()
        session.battle_dashboard.log_entries.current_round = 2
        session.battle_dashboard.log_entries.total_round = 10

        self.assertEqual(session._round_progress_text(), "Round   2 / 10 ")

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
        self.assertIsNone(dashboard.snap)


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

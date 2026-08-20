import ast
import asyncio
import inspect
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from hvbrowser import Realm
from hvbrowser.runtime import ZendriverOperationTimeout

import hvbattle.hv_battle_ponychart as ponychart_module
import hvbattle.session as session_module
from hvbattle import (
    ArenaOption,
    BattleActionOutcomeUnknownError,
    BattleCompleted,
    BattleInterruptedError,
    BattlePresence,
    BattleRecoveryExhaustedError,
    BattleRunner,
    BattleSession,
    BattleStepIdle,
    BattleStepIdleReason,
    BattleStepProgress,
    BattleStepProgressKind,
    BattleStopped,
    BattleTurnPhase,
    BattleTurnState,
    GrindfestOption,
    PonyChartResolutionError,
    RingOfBloodChallenge,
    RingOfBloodOption,
    RingOfBloodSnapshot,
    RingOfBloodStartOutcome,
    TurnDecision,
)
from hvbattle.battle_launcher import BattleLauncher
from hvbattle.battle_state import BattleStateStore, CombatLogTracker
from hvbattle.hv_battle_buff_manager import BuffManager
from hvbattle.hv_battle_ponychart import PonyChart
from hvbattle.recovery import ActionDialogTracker


class BattleSessionSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_arena_option_keeps_old_positional_constructor(self) -> None:
        option = ArenaOption(12, "token")

        self.assertEqual(option.battle_id, 12)
        self.assertEqual(option.token, "token")
        self.assertIsNone(option.challenge_name)
        self.assertIsNone(option.exp_multiplier)
        enriched = ArenaOption(
            12,
            "token",
            challenge_name="Challenge",
            exp_multiplier=2.0,
        )
        self.assertEqual(option, enriched)
        self.assertEqual(hash(option), hash(enriched))
        parameters = inspect.signature(ArenaOption).parameters
        self.assertIs(parameters["challenge_name"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameters["exp_multiplier"].kind, inspect.Parameter.KEYWORD_ONLY)

    async def test_browser_ready_installs_dialog_handler_when_enabled(self) -> None:
        session = BattleSession(headless=True, auto_accept_dialogs=True)
        session._setup_alert_handler = AsyncMock()

        await session._on_browser_ready()

        session._setup_alert_handler.assert_awaited_once_with()

    async def test_dialog_log_records_category_without_raw_server_message(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.page = Mock()
        session.page.add_handler = Mock()
        session.page.send = AsyncMock()
        session.action_dialog_tracker = ActionDialogTracker()
        session.action_dialog_tracker.begin("action-1")
        secret_message = "Server communication failed: response-token-should-not-log"

        with patch.object(session_module.logger, "warning") as warning:
            await BattleSession._setup_alert_handler(session)
            handler = session.page.add_handler.call_args.args[1]
            await handler(SimpleNamespace(message=secret_message))

        self.assertEqual(
            session.action_dialog_tracker.category_for("action-1"),
            "server-communication-failed",
        )
        self.assertNotIn(secret_message, str(warning.call_args))
        session.page.send.assert_awaited_once()

    def test_snapshot_is_unavailable_before_first_prepared_turn(self) -> None:
        session = object.__new__(BattleSession)
        session.battle_state = Mock()
        session.battle_state.snap = None

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

    async def test_armed_skill_generation_timeout_propagates_unchanged(self) -> None:
        session = object.__new__(BattleSession)
        timeout = ZendriverOperationTimeout(timeout_seconds=15.0)
        session.attack_monster = AsyncMock(side_effect=timeout)

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await BattleSession.submit_selected_skill_target(
                session,
                3,
                "imperil",
            )

        self.assertIs(raised.exception, timeout)
        session.attack_monster.assert_awaited_once_with(3)

    async def test_challenge_presence_is_checked_before_state_parse(self) -> None:
        session = object.__new__(BattleSession)
        session.is_ponychart_present = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock()
        session._has_battle_marker = AsyncMock()
        session.battle_state = Mock()
        session.battle_state.inspect = AsyncMock(
            side_effect=AssertionError("ordinary parser ran before PonyChart")
        )

        active = await BattleSession.is_in_battle(session)

        self.assertTrue(active)
        session.battle_state.inspect.assert_not_awaited()
        session._read_battle_phase.assert_not_awaited()
        session._has_battle_marker.assert_not_awaited()

    async def test_live_zendriver_timeout_does_not_probe_after_prepare_parse(
        self,
    ) -> None:
        operation_timeout = ZendriverOperationTimeout(timeout_seconds=10.0)
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(return_value="active")
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_state = Mock()
        session.battle_state.update = AsyncMock(side_effect=operation_timeout)

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await BattleSession.prepare_turn_state(session)

        self.assertIs(raised.exception, operation_timeout)
        session.battle_state.update.assert_awaited_once_with()
        session.is_ponychart_present.assert_awaited_once_with()
        session._read_battle_phase.assert_awaited_once_with()

    async def test_non_battle_transition_does_not_increment_turn(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = 4
        session.round = 3
        session._has_battle_marker = AsyncMock(return_value=False)

        state = await BattleSession.prepare_turn_state(session)

        self.assertFalse(state.actionable)
        self.assertEqual(session.turn, 4)

    async def test_resumed_turn_logs_unknown_then_available_progress_once(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.turn = -1
        session.round = -1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(return_value="active")
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_state = Mock()
        session.battle_state.update = AsyncMock()
        session.battle_state.snap = SimpleNamespace(
            warnings=[],
            player=SimpleNamespace(
                hp_percent=87.0, mp_percent=100.0, sp_percent=42.0, overcharge_value=30
            ),
        )
        session.battle_state.overview_monsters.alive_monster = [0]
        session.battle_state.log_entries.current_round = 2
        session.battle_state.log_entries.total_round = 0
        session.battle_state.log_entries.current_lines = ["You hit a monster."]

        with self.assertLogs("hvbattle.session", level="INFO") as captured:
            await BattleSession.prepare_turn_state(session)
            await BattleSession.prepare_turn_state(session)
            session.battle_state.log_entries.total_round = 10
            await BattleSession.prepare_turn_state(session)

        output = "\n".join(captured.output)
        self.assertEqual(
            output.count("Battle detected; round data is not available yet"),
            1,
        )
        self.assertEqual(
            output.count("Round 2/10 HP 87.0% MP 100.0% SP 42.0% OC 30"), 1
        )
        self.assertNotIn("You hit a monster.", output)
        self.assertNotIn("Round   0 / 0", output)

    async def test_combat_details_are_debug_and_round_progress_is_deduplicated(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.turn = -1
        session.round = -1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(return_value="active")
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_state = Mock()
        session.battle_state.update = AsyncMock()
        session.battle_state.snap = SimpleNamespace(
            warnings=[],
            player=SimpleNamespace(
                hp_percent=87.0, mp_percent=100.0, sp_percent=42.0, overcharge_value=30
            ),
        )
        session.battle_state.overview_monsters.alive_monster = [0]
        session.battle_state.log_entries.current_round = 2
        session.battle_state.log_entries.total_round = 10
        session.battle_state.log_entries.current_lines = ["You hit a monster."]

        with (
            patch.object(session_module.logger, "info") as info,
            patch.object(session_module.logger, "debug") as debug,
        ):
            first_state = await BattleSession.prepare_turn_state(session)
            second_state = await BattleSession.prepare_turn_state(session)
            session.battle_state.log_entries.current_round = 3
            third_state = await BattleSession.prepare_turn_state(session)

        self.assertEqual(
            info.call_args_list,
            [
                call(
                    "Round %d/%d HP %.1f%% MP %.1f%% SP %.1f%% OC %d",
                    2,
                    10,
                    87.0,
                    100.0,
                    42.0,
                    30,
                    extra={"activity": "Battle"},
                ),
                call(
                    "Round %d/%d HP %.1f%% MP %.1f%% SP %.1f%% OC %d",
                    3,
                    10,
                    87.0,
                    100.0,
                    42.0,
                    30,
                    extra={"activity": "Battle"},
                ),
            ],
        )
        self.assertEqual(
            debug.call_args_list,
            [
                call("%s", "Turn     0 Round   2 / 10  You hit a monster."),
                call("%s", "Turn     1 Round   2 / 10  You hit a monster."),
                call("%s", "Turn     2 Round   3 / 10  You hit a monster."),
            ],
        )
        self.assertTrue(first_state.actionable)
        self.assertEqual(
            first_state.log_lines,
            ("Turn     0 Round   2 / 10  You hit a monster.",),
        )
        self.assertTrue(second_state.actionable)
        self.assertEqual(
            second_state.log_lines,
            ("Turn     1 Round   2 / 10  You hit a monster.",),
        )
        self.assertTrue(third_state.actionable)
        self.assertEqual(
            third_state.log_lines,
            ("Turn     2 Round   3 / 10  You hit a monster.",),
        )

    def test_round_transition_detail_is_debug(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = 4

        with (
            patch.object(session_module.logger, "info") as info,
            patch.object(session_module.logger, "debug") as debug,
        ):
            state = BattleSession._prepare_round_transition(session)

        self.assertEqual(state.phase, BattleTurnPhase.NEXT_FLOOR)
        info.assert_not_called()
        debug.assert_called_once_with(
            "Turn %5d: next-round control is ready.",
            5,
        )

    async def test_parser_warnings_log_changes_once_and_repeats_at_debug(
        self,
    ) -> None:
        first_warnings = [
            "missing player hp",
            "missing player mp",
            "missing player sp",
            "missing monster 1",
            "missing monster 2",
            "missing monster 3 hp",
        ]
        hidden_warning_changed = [*first_warnings[:5], "missing monster 3 mp"]
        visible_warning_changed = ["invalid player hp", *first_warnings[1:]]
        session = object.__new__(BattleSession)
        session.turn = -1
        session.round = -1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(return_value="active")
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_state = Mock()
        session.battle_state.update = AsyncMock()
        session.battle_state.snap = SimpleNamespace(
            warnings=first_warnings,
            player=SimpleNamespace(
                hp_percent=87.0, mp_percent=100.0, sp_percent=42.0, overcharge_value=30
            ),
        )
        session.battle_state.overview_monsters.alive_monster = [0]
        session.battle_state.log_entries.current_round = 2
        session.battle_state.log_entries.total_round = 10
        session.battle_state.log_entries.current_lines = []

        with (
            patch.object(session_module.logger, "info"),
            patch.object(session_module.logger, "warning") as warning,
            patch.object(session_module.logger, "debug") as debug,
        ):
            await BattleSession.prepare_turn_state(session)
            await BattleSession.prepare_turn_state(session)
            session.battle_state.snap.warnings = hidden_warning_changed
            await BattleSession.prepare_turn_state(session)
            session.battle_state.snap.warnings = visible_warning_changed
            await BattleSession.prepare_turn_state(session)

        displayed_warnings = ", ".join(first_warnings[:5])
        changed_displayed_warnings = ", ".join(visible_warning_changed[:5])
        self.assertEqual(
            warning.call_args_list,
            [
                call(
                    "Battle parser warnings count=%d first=%s",
                    6,
                    displayed_warnings,
                ),
                call(
                    "Battle parser warnings count=%d first=%s",
                    6,
                    changed_displayed_warnings,
                ),
            ],
        )
        self.assertEqual(
            debug.call_args_list,
            [
                call(
                    "Battle parser warnings count=%d first=%s",
                    6,
                    displayed_warnings,
                ),
                call(
                    "Battle parser warnings count=%d first=%s",
                    6,
                    displayed_warnings,
                ),
            ],
        )

    def test_reset_clears_battle_scoped_log_deduplication(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = 12
        session.round = 3
        session._completion_observed = True
        session._last_parser_warning_signature = (1, ("old warning",))
        session._last_reported_round_progress = (3, 10)
        session.battle_state = Mock()

        BattleSession.reset_battle_tracking(session)

        self.assertEqual(session.turn, -1)
        self.assertEqual(session.round, -1)
        self.assertFalse(session._completion_observed)
        self.assertIsNone(session._last_parser_warning_signature)
        self.assertIsNone(session._last_reported_round_progress)
        session.battle_state.reset.assert_called_once_with()

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
        session.battle_state = Mock()
        session.battle_state.update = AsyncMock()
        session.battle_state.snap = SimpleNamespace(warnings=[])
        session.battle_state.overview_monsters.alive_monster = []
        session.battle_state.log_entries.current_round = 5
        session.battle_state.log_entries.total_round = 5

        with self.assertRaisesRegex(TimeoutError, "no monsters"):
            await BattleSession.prepare_turn_state(session)

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
        session.battle_state = Mock()
        session.battle_state.update = AsyncMock()
        session.battle_state.snap = SimpleNamespace(warnings=[])
        session.battle_state.overview_monsters.alive_monster = []
        session.battle_state.log_entries.current_round = 5
        session.battle_state.log_entries.total_round = 5

        with (
            patch.object(session_module.logger, "info") as info,
            patch.object(session_module.logger, "debug") as debug,
        ):
            state = await BattleSession.prepare_turn_state(session)

        self.assertFalse(state.actionable)
        self.assertTrue(session.battle_completion_observed)
        self.assertEqual(session.turn, 0)
        session.battle_state.update.assert_not_awaited()
        info.assert_not_called()
        debug.assert_called_once_with("Final battle completion control is ready.")

    async def test_next_floor_marker_bypasses_parser_failure(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = 2
        session.round = 1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(return_value="next-floor")
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_state = Mock()
        session.battle_state.update = AsyncMock(
            side_effect=AssertionError("parser must not run during transition")
        )

        state = await BattleSession.prepare_turn_state(session)

        self.assertTrue(state.actionable)
        self.assertEqual(state.log_lines, ())
        self.assertEqual(session.turn, 3)
        session.battle_state.update.assert_not_awaited()

    async def test_parser_error_reconciles_completion_that_appeared(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = 2
        session.round = 1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(side_effect=["active", "complete"])
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_state = Mock()
        session.battle_state.update = AsyncMock(
            side_effect=TimeoutError("monsters disappeared during parse")
        )

        with (
            patch.object(session_module.logger, "info") as info,
            patch.object(session_module.logger, "debug") as debug,
        ):
            state = await BattleSession.prepare_turn_state(session)

        self.assertFalse(state.actionable)
        self.assertTrue(session.battle_completion_observed)
        session.battle_state.update.assert_awaited_once()
        info.assert_not_called()
        debug.assert_called_once_with(
            "Final battle completion control appeared while parsing."
        )

    async def test_completion_after_parse_is_debug_detail(self) -> None:
        session = object.__new__(BattleSession)
        session.turn = 2
        session.round = 1
        session._completion_observed = False
        session._has_battle_marker = AsyncMock(return_value=True)
        session._read_battle_phase = AsyncMock(side_effect=["active", "complete"])
        session.is_ponychart_present = AsyncMock(return_value=False)
        session.battle_state = Mock()
        session.battle_state.update = AsyncMock()

        with (
            patch.object(session_module.logger, "info") as info,
            patch.object(session_module.logger, "debug") as debug,
        ):
            state = await BattleSession.prepare_turn_state(session)

        self.assertFalse(state.actionable)
        self.assertTrue(session.battle_completion_observed)
        session.battle_state.update.assert_awaited_once_with()
        info.assert_not_called()
        debug.assert_called_once_with(
            "Final battle completion control appeared after parsing."
        )

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

    async def test_completion_presence_is_distinct_from_absence(self) -> None:
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session.is_ponychart_present = AsyncMock(return_value=False)
        session._read_battle_phase = AsyncMock(return_value="complete")

        presence = await BattleSession.inspect_battle_presence(session)

        self.assertIs(presence, BattlePresence.COMPLETION)
        self.assertTrue(session.battle_completion_observed)

    async def test_no_active_battle_probe_is_debug_detail(self) -> None:
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session.is_ponychart_present = AsyncMock(return_value=False)
        session._read_battle_phase = AsyncMock(return_value="active")
        session._has_battle_marker = AsyncMock(return_value=False)

        with (
            patch.object(session_module.logger, "info") as info,
            patch.object(session_module.logger, "debug") as debug,
        ):
            active = await BattleSession.is_in_battle(session)

        self.assertFalse(active)
        info.assert_not_called()
        debug.assert_called_once_with("No active battle detected.")

    async def test_battle_parse_retry_and_terminal_error_have_context(self) -> None:
        first_error = ValueError("first parse failure")
        last_error = ValueError("second parse failure")
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session.is_ponychart_present = AsyncMock(return_value=False)
        session._read_battle_phase = AsyncMock(return_value="active")
        session._has_battle_marker = AsyncMock(return_value=True)
        session.page = Mock()
        session.page.wait = AsyncMock()
        session.battle_state = Mock()
        session.battle_state.inspect = AsyncMock(side_effect=[first_error, last_error])

        with (
            patch.object(session_module.logger, "warning") as warning,
            patch.object(session_module.logger, "debug") as debug,
            patch.object(session_module.logger, "error") as error,
            self.assertRaisesRegex(ValueError, "second parse failure"),
        ):
            await BattleSession.is_in_battle(session)

        warning.assert_called_once_with(
            "Battle state parse failed; retrying next_attempt=%d/%d "
            "delay=%.1fs error_type=%s",
            2,
            2,
            1.0,
            "ValueError",
        )
        debug.assert_called_once_with(
            "Battle state parse retry error detail",
            exc_info=True,
        )
        session.page.wait.assert_awaited_once_with(1.0)
        error.assert_called_once_with(
            "Battle state parse failed after %d attempts on an active battle "
            "page; refusing to report no battle: error_type=%s",
            2,
            "ValueError",
        )

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
        session.battle_state = Mock()
        session.battle_state.inspect = AsyncMock(
            side_effect=ValueError("monsters disappeared during inspect")
        )

        active = await BattleSession.is_in_battle(session)

        self.assertFalse(active)
        self.assertTrue(session.battle_completion_observed)
        session.battle_state.inspect.assert_awaited_once()

    async def test_inspect_timeout_reconciles_next_floor_phase(self) -> None:
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session.is_ponychart_present = AsyncMock(return_value=False)
        session._read_battle_phase = AsyncMock(side_effect=["active", "next-floor"])
        session._has_battle_marker = AsyncMock(return_value=True)
        session.battle_state = Mock()
        session.battle_state.inspect = AsyncMock(
            side_effect=TimeoutError("inspect raced with transition")
        )

        active = await BattleSession.is_in_battle(session)

        self.assertTrue(active)
        session.battle_state.inspect.assert_awaited_once()

    async def test_live_zendriver_timeout_does_not_probe_after_battle_inspect(
        self,
    ) -> None:
        operation_timeout = ZendriverOperationTimeout(timeout_seconds=10.0)
        session = object.__new__(BattleSession)
        session._completion_observed = False
        session.is_ponychart_present = AsyncMock(return_value=False)
        session._read_battle_phase = AsyncMock(return_value="active")
        session._has_battle_marker = AsyncMock(return_value=True)
        session.battle_state = Mock()
        session.battle_state.inspect = AsyncMock(side_effect=operation_timeout)

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await BattleSession.is_in_battle(session)

        self.assertIs(raised.exception, operation_timeout)
        session.battle_state.inspect.assert_awaited_once_with()
        session.is_ponychart_present.assert_awaited_once_with()
        session._read_battle_phase.assert_awaited_once_with()

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
        self.goto_ring_of_blood = AsyncMock()
        self.goto_grindfest = AsyncMock()
        self.go_next_floor = AsyncMock(return_value=True)
        self.acknowledge_battle_completion = AsyncMock()

    @property
    async def is_isekai(self) -> bool:
        return False

    async def is_in_battle(self) -> bool:
        return self._active

    async def inspect_battle_presence(self) -> BattlePresence:
        return BattlePresence.ACTIVE if self._active else BattlePresence.ABSENT

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
        session.goto_ring_of_blood.assert_not_awaited()
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
        self.assertIn("round data was unavailable", output)
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

        with (
            patch("hvbattle.runner.logger") as runner_logger,
            self.assertRaises(BattleInterruptedError) as raised,
        ):
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertIs(raised.exception.__cause__, action_error)
        self.assertNotIsInstance(raised.exception, BattleRecoveryExhaustedError)
        runner_logger.error.assert_called_once_with(
            "Battle action completion could not be confirmed: error_type=%s",
            "BattleActionOutcomeUnknownError",
        )
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

        with (
            patch("hvbattle.runner.logger") as runner_logger,
            self.assertRaises(BattleInterruptedError) as raised,
        ):
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                timeout_retries=3,
                sleep=retry_sleep,
            ).run_current()

        self.assertIsInstance(raised.exception.__cause__, TimeoutError)
        self.assertEqual(
            runner_logger.warning.call_args_list,
            [
                call(
                    "Battle turn timed out; retrying (%d/%d) error_type=%s",
                    1,
                    3,
                    "TimeoutError",
                ),
                call(
                    "Battle turn timed out; retrying (%d/%d) error_type=%s",
                    2,
                    3,
                    "TimeoutError",
                ),
            ],
        )
        self.assertEqual(
            runner_logger.debug.call_args_list,
            [
                call("Battle turn timeout error detail", exc_info=True),
                call("Battle turn timeout error detail", exc_info=True),
            ],
        )
        runner_logger.error.assert_called_once_with(
            "Battle turn timed out; retry limit reached (%d/%d) error_type=%s",
            3,
            3,
            "TimeoutError",
        )
        self.assertEqual(session.prepare_turn_state.await_count, 3)
        self.assertEqual(retry_sleep.await_count, 2)
        strategy.take_turn.assert_not_awaited()

    async def test_live_zendriver_timeout_is_never_retried(self) -> None:
        session = _FakeSession(active=True)
        operation_timeout = ZendriverOperationTimeout(timeout_seconds=10.0)
        session.prepare_turn_state = AsyncMock(side_effect=operation_timeout)
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

        self.assertIs(raised.exception.__cause__, operation_timeout)
        session.prepare_turn_state.assert_awaited_once_with()
        retry_sleep.assert_not_awaited()
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

        with patch("hvbattle.runner.logger") as runner_logger:
            result = await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertEqual(result, BattleCompleted(False, 0, 1, 1))
        runner_logger.info.assert_called_once_with(
            "Completed after %d decisions (round %d/%d)",
            0,
            1,
            1,
            extra={
                "activity": "Battle",
                "realm": "Persistent",
                "tab_role": "persistent",
            },
        )
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

    async def test_paused_steps_continue_to_service_ponychart(self) -> None:
        session = _FakeSession(active=True)
        challenge_checks = 0

        async def resolve_ponychart() -> bool:
            nonlocal challenge_checks
            challenge_checks += 1
            return challenge_checks == 3

        session.resolve_ponychart = resolve_ponychart  # type: ignore[method-assign]
        strategy = Mock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.STOP)
        runner = BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            pause_requested=lambda: True,
            challenge_poll_interval=0.5,
            sleep=AsyncMock(),
        )

        first = await asyncio.wait_for(runner.step(), timeout=0.1)
        second = await asyncio.wait_for(runner.step(), timeout=0.1)
        third = await asyncio.wait_for(runner.step(), timeout=0.1)

        expected = BattleStepIdle(
            retry_after=0.5,
            reason=BattleStepIdleReason.PAUSED,
        )
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertEqual(
            third,
            BattleStepProgress(
                kind=BattleStepProgressKind.PONYCHART_RESOLVED,
                is_isekai=None,
                decision_count=0,
                current_round=1,
                total_rounds=1,
            ),
        )
        self.assertEqual(challenge_checks, 3)
        strategy.take_turn.assert_not_awaited()

    async def test_pause_after_turn_parse_defers_strategy_action(self) -> None:
        session = _FakeSession(active=True)
        gate_calls = 0
        resumed = False

        def pause_requested() -> bool:
            nonlocal gate_calls
            gate_calls += 1
            return not resumed and gate_calls == 3

        strategy = Mock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.STOP)
        runner = BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            pause_requested=pause_requested,
            challenge_poll_interval=0.5,
            sleep=AsyncMock(),
        )

        deferred = await asyncio.wait_for(runner.step(), timeout=0.1)

        self.assertEqual(
            deferred,
            BattleStepIdle(
                retry_after=0.5,
                reason=BattleStepIdleReason.PAUSED,
            ),
        )
        strategy.take_turn.assert_not_awaited()

        resumed = True
        result = await runner.step()

        self.assertIsInstance(result, BattleStopped)
        strategy.take_turn.assert_awaited_once_with(session)

    async def test_battle_launcher_uses_composed_realm_navigator(self) -> None:
        browser = Mock()
        browser.page = Mock()
        realm = Mock()
        realm.current = AsyncMock(return_value=Realm.ISEKAI)
        launcher = BattleLauncher(browser, realm)

        self.assertEqual(await launcher._path_prefix(), "/isekai")

        realm.current.return_value = Realm.PERSISTENT
        self.assertEqual(await launcher._path_prefix(), "")

    async def test_false_submission_result_is_not_reported_as_started(self) -> None:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client, Mock())
        arena_url = "https://hentaiverse.org/?s=Battle&ss=ar"
        launcher._path_prefix = AsyncMock(return_value="")
        client.page.evaluate = AsyncMock(side_effect=[arena_url, False])

        started = await launcher.start_arena(ArenaOption(12))

        self.assertFalse(started)

    async def test_arena_submission_exception_propagates_as_unknown(self) -> None:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client, Mock())
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
        launcher = BattleLauncher(client, Mock())
        client.page.evaluate = AsyncMock(
            return_value=["init_battle(12, 34)", "init_battle(56, 78, 'token')"]
        )

        options = await launcher.list_arena_options()

        self.assertEqual(options, (ArenaOption(12), ArenaOption(56, "token")))

    async def test_arena_options_include_row_metadata_in_server_order(self) -> None:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client, Mock())
        client.page.evaluate = AsyncMock(
            return_value=[
                {
                    "onclick": "init_battle(12, 34)",
                    "challengeName": " First Challenge ",
                    "expText": "X1.25",
                },
                {
                    "onclick": "init_battle(56, 78, 'token');",
                    "challengeName": "Last Challenge",
                    "expText": "x3.0",
                },
            ]
        )

        options = await launcher.list_arena_options()

        self.assertEqual(
            options,
            (
                ArenaOption(
                    12,
                    challenge_name="First Challenge",
                    exp_multiplier=1.25,
                ),
                ArenaOption(
                    56,
                    "token",
                    challenge_name="Last Challenge",
                    exp_multiplier=3.0,
                ),
            ),
        )

    async def test_malformed_arena_option_log_does_not_expose_token(self) -> None:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client, Mock())
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
        launcher = BattleLauncher(client, Mock())
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
        launcher = BattleLauncher(client, Mock())
        grindfest_url = "https://hentaiverse.org/?s=Battle&ss=gr"
        launcher._path_prefix = AsyncMock(return_value="")
        client.page.evaluate = AsyncMock(
            side_effect=[grindfest_url, ValueError("bad form")]
        )

        with self.assertRaisesRegex(ValueError, "bad form"):
            await launcher.start_grindfest(GrindfestOption(12))


class RingOfBloodLauncherTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _launcher() -> tuple[BattleLauncher, Mock]:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client, Mock())
        launcher._path_prefix = AsyncMock(return_value="")
        return launcher, client.page

    @staticmethod
    def _payload(
        *,
        tokens: str = "You have 1,234 tokens of blood.",
        rows: list[object] | None = None,
    ) -> dict[str, object]:
        if rows is None:
            rows = [
                {
                    "onclick": "init_battle(112,10)",
                    "challengeName": "Triple Trio and the Tree",
                    "expText": "X1.0",
                    "entryCostText": "10 Tokens",
                }
            ]
        return {"tokenText": tokens, "rows": rows}

    async def test_snapshot_keeps_rows_without_start_actions(self) -> None:
        launcher, page = self._launcher()
        option = RingOfBloodOption(105, "Konata", 1.0, 1)
        page.evaluate = AsyncMock(
            return_value=self._payload(
                rows=[
                    {
                        "onclick": "init_battle(105,1)",
                        "challengeName": "Konata",
                        "expText": "X1.0",
                        "entryCostText": "1 Token",
                    },
                    {
                        "onclick": None,
                        "challengeName": "Triple Trio and the Tree",
                        "expText": "",
                        "entryCostText": "Completed",
                    },
                ]
            )
        )

        snapshot = await launcher.inspect_ring_of_blood()

        self.assertEqual(
            snapshot,
            RingOfBloodSnapshot(
                1_234,
                (option,),
                (
                    RingOfBloodChallenge(
                        "Konata",
                        1.0,
                        1,
                        option,
                    ),
                    RingOfBloodChallenge(
                        "Triple Trio and the Tree",
                        None,
                        None,
                    ),
                ),
            ),
        )
        self.assertTrue(snapshot.challenges[0].startable)
        self.assertFalse(snapshot.challenges[1].startable)
        self.assertIsNone(snapshot.challenges[1].exp_multiplier)
        self.assertIsNone(snapshot.challenges[1].entry_cost)
        self.assertIsNone(snapshot.challenges[1].start_action)
        inspection_script = page.evaluate.await_args.args[0]
        self.assertIn("rows.slice(1).flatMap", inspection_script)
        self.assertIn("!cells[challengeIndex]", inspection_script)
        self.assertNotIn("if (!action) return []", inspection_script)
        self.assertNotIn("postoken", inspection_script.casefold())

    async def test_snapshot_keeps_unaffordable_option_for_client_policy(self) -> None:
        launcher, page = self._launcher()
        page.evaluate = AsyncMock(
            return_value=self._payload(tokens="You have 3 tokens of blood.")
        )

        snapshot = await launcher.inspect_ring_of_blood()

        self.assertEqual(snapshot.tokens_of_blood, 3)
        self.assertEqual(snapshot.options[0].entry_cost, 10)

    async def test_snapshot_rejects_missing_or_malformed_balance(self) -> None:
        cases = (
            None,
            {"tokenText": "balance unavailable", "rows": []},
            {"tokenText": "You have 3 tokens of blood.", "rows": None},
        )
        for payload in cases:
            with self.subTest(payload_type=type(payload).__name__):
                launcher, page = self._launcher()
                page.evaluate = AsyncMock(return_value=payload)

                with self.assertRaises(RuntimeError):
                    await launcher.inspect_ring_of_blood()

    async def test_snapshot_rejects_malformed_present_action(self) -> None:
        malformed_actions: tuple[object, ...] = (
            "",
            "init_battle(not-valid)",
            112,
        )
        for onclick in malformed_actions:
            with self.subTest(onclick=onclick):
                launcher, page = self._launcher()
                page.evaluate = AsyncMock(
                    return_value=self._payload(
                        rows=[
                            {
                                "onclick": onclick,
                                "challengeName": "Triple Trio and the Tree",
                                "expText": "X1.0",
                                "entryCostText": "10 Tokens",
                            }
                        ]
                    )
                )

                with self.assertRaisesRegex(RuntimeError, "action is malformed"):
                    await launcher.inspect_ring_of_blood()

    async def test_snapshot_rejects_missing_metadata_for_present_action(self) -> None:
        cases = (
            ("", "10 Tokens", "EXP multiplier"),
            ("X1.0", "Completed", "entry cost"),
        )
        for exp_text, entry_cost_text, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                launcher, page = self._launcher()
                page.evaluate = AsyncMock(
                    return_value=self._payload(
                        rows=[
                            {
                                "onclick": "init_battle(112,10)",
                                "challengeName": "Triple Trio and the Tree",
                                "expText": exp_text,
                                "entryCostText": entry_cost_text,
                            }
                        ]
                    )
                )

                with self.assertRaisesRegex(RuntimeError, expected_error):
                    await launcher.inspect_ring_of_blood()

    async def test_snapshot_rejects_inconsistent_entry_cost(self) -> None:
        launcher, page = self._launcher()
        page.evaluate = AsyncMock(
            return_value=self._payload(
                rows=[
                    {
                        "onclick": "init_battle(112,9)",
                        "challengeName": "Triple Trio and the Tree",
                        "expText": "X1.0",
                        "entryCostText": "10 Tokens",
                    }
                ]
            )
        )

        with self.assertRaisesRegex(RuntimeError, "inconsistent"):
            await launcher.inspect_ring_of_blood()

    async def test_start_returns_insufficient_tokens_without_submission(self) -> None:
        launcher, page = self._launcher()
        option = RingOfBloodOption(112, "Triple Trio and the Tree", 1.0, 10)
        snapshot = RingOfBloodSnapshot(3, (option,))
        page.evaluate = AsyncMock(
            side_effect=[
                "https://hentaiverse.org/?s=Battle&ss=rb",
                self._payload(tokens="You have 3 tokens of blood."),
            ]
        )

        outcome = await launcher.start_ring_of_blood(
            option,
            expected_before=snapshot,
        )

        self.assertIs(outcome, RingOfBloodStartOutcome.INSUFFICIENT_TOKENS)
        self.assertEqual(page.evaluate.await_count, 2)

    async def test_start_returns_unavailable_when_option_disappears(self) -> None:
        launcher, page = self._launcher()
        option = RingOfBloodOption(112, "Triple Trio and the Tree", 1.0, 10)
        snapshot = RingOfBloodSnapshot(20, (option,))
        page.evaluate = AsyncMock(
            side_effect=[
                "https://hentaiverse.org/?s=Battle&ss=rb",
                self._payload(tokens="You have 20 tokens of blood.", rows=[]),
            ]
        )

        outcome = await launcher.start_ring_of_blood(
            option,
            expected_before=snapshot,
        )

        self.assertIs(outcome, RingOfBloodStartOutcome.OPTION_UNAVAILABLE)
        self.assertEqual(page.evaluate.await_count, 2)

    async def test_start_returns_state_changed_before_submission(self) -> None:
        launcher, page = self._launcher()
        option = RingOfBloodOption(112, "Triple Trio and the Tree", 1.0, 10)
        snapshot = RingOfBloodSnapshot(20, (option,))
        page.evaluate = AsyncMock(
            side_effect=[
                "https://hentaiverse.org/?s=Battle&ss=rb",
                self._payload(tokens="You have 19 tokens of blood."),
            ]
        )

        outcome = await launcher.start_ring_of_blood(
            option,
            expected_before=snapshot,
        )

        self.assertIs(outcome, RingOfBloodStartOutcome.STATE_CHANGED)
        self.assertEqual(page.evaluate.await_count, 2)

    async def test_start_submits_existing_form_without_copying_hidden_state(
        self,
    ) -> None:
        launcher, page = self._launcher()
        option = RingOfBloodOption(112, "Triple Trio and the Tree", 1.0, 10)
        snapshot = RingOfBloodSnapshot(20, (option,))
        page.evaluate = AsyncMock(
            side_effect=[
                "https://hentaiverse.org/?s=Battle&ss=rb",
                self._payload(tokens="You have 20 tokens of blood."),
                "submitted",
            ]
        )

        outcome = await launcher.start_ring_of_blood(
            option,
            expected_before=snapshot,
        )

        self.assertIs(outcome, RingOfBloodStartOutcome.SUBMITTED)
        submission_script = page.evaluate.await_args_list[-1].args[0]
        self.assertIn("initform.submit()", submission_script)
        self.assertIn("window.location.href !== expectedUrl", submission_script)
        self.assertIn("Number(match[1]) === expectedId", submission_script)
        self.assertIn("Number(match[2]) === expectedCost", submission_script)
        self.assertIn("const expectedId = 112", submission_script)
        self.assertIn("const expectedCost = 10", submission_script)
        for result in (
            "unexpected-page",
            "missing-table",
            "missing-initid",
            "missing-initform",
            "missing-exact-action",
            "submitted",
        ):
            with self.subTest(atomic_result=result):
                self.assertIn(f"return '{result}'", submission_script)
        self.assertNotIn("postoken", submission_script.casefold())

    async def test_start_submits_from_exact_isekai_ring_url(self) -> None:
        launcher, page = self._launcher()
        launcher._path_prefix = AsyncMock(return_value="/isekai")
        option = RingOfBloodOption(112, "Triple Trio and the Tree", 1.0, 10)
        snapshot = RingOfBloodSnapshot(20, (option,))
        page.evaluate = AsyncMock(
            side_effect=[
                "https://hentaiverse.org/isekai/?s=Battle&ss=rb",
                self._payload(tokens="You have 20 tokens of blood."),
                "submitted",
            ]
        )

        outcome = await launcher.start_ring_of_blood(
            option,
            expected_before=snapshot,
        )

        self.assertIs(outcome, RingOfBloodStartOutcome.SUBMITTED)
        submission_script = page.evaluate.await_args_list[-1].args[0]
        self.assertIn(
            'const expectedUrl = "https://hentaiverse.org/isekai/' '?s=Battle&ss=rb";',
            submission_script,
        )

    async def test_final_atomic_revalidation_failures_do_not_submit(self) -> None:
        failure_results: tuple[object, ...] = (
            "unexpected-page",
            "missing-table",
            "missing-initid",
            "missing-initform",
            "missing-exact-action",
            True,
            {"unexpected": "payload"},
        )
        for atomic_result in failure_results:
            with self.subTest(atomic_result=atomic_result):
                launcher, page = self._launcher()
                option = RingOfBloodOption(
                    112,
                    "Triple Trio and the Tree",
                    1.0,
                    10,
                )
                snapshot = RingOfBloodSnapshot(20, (option,))
                page.evaluate = AsyncMock(
                    side_effect=[
                        "https://hentaiverse.org/?s=Battle&ss=rb",
                        self._payload(tokens="You have 20 tokens of blood."),
                        atomic_result,
                    ]
                )

                outcome = await launcher.start_ring_of_blood(
                    option,
                    expected_before=snapshot,
                )

                self.assertIs(
                    outcome,
                    RingOfBloodStartOutcome.OPTION_UNAVAILABLE,
                )
                self.assertEqual(page.evaluate.await_count, 3)

    async def test_start_submission_exception_propagates(self) -> None:
        launcher, page = self._launcher()
        option = RingOfBloodOption(112, "Triple Trio and the Tree", 1.0, 10)
        snapshot = RingOfBloodSnapshot(20, (option,))
        submission_error = ValueError("navigation destroyed context")
        page.evaluate = AsyncMock(
            side_effect=[
                "https://hentaiverse.org/?s=Battle&ss=rb",
                self._payload(tokens="You have 20 tokens of blood."),
                submission_error,
            ]
        )

        with self.assertRaises(ValueError) as raised:
            await launcher.start_ring_of_blood(
                option,
                expected_before=snapshot,
            )

        self.assertIs(raised.exception, submission_error)


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
        session.battle_state = Mock()
        session.battle_state.log_entries.current_round = 0
        session.battle_state.log_entries.total_round = 0

        self.assertEqual(session._round_progress_text(), "Round   ? / ?  ")

    def test_observed_round_metadata_is_rendered_numerically(self) -> None:
        session = object.__new__(BattleSession)
        session.battle_state = Mock()
        session.battle_state.log_entries.current_round = 2
        session.battle_state.log_entries.total_round = 10

        self.assertEqual(session._round_progress_text(), "Round   2 / 10 ")

    def test_empty_refresh_clears_previous_log_delta(self) -> None:
        entry = CombatLogTracker()
        entry.update(SimpleNamespace(log=SimpleNamespace(lines=["first"])))

        entry.update(SimpleNamespace(log=SimpleNamespace(lines=[])))

        self.assertEqual(entry.current_lines, [])

    def test_state_store_reset_discards_previous_battle_log_state(self) -> None:
        state_store = BattleStateStore(Mock())
        previous = state_store.log_entries
        previous.prev_lines.append("old battle")
        state_store.overview_monsters.alive_monster = [2]

        state_store.reset()

        self.assertIsNot(state_store.log_entries, previous)
        self.assertEqual(list(state_store.log_entries.prev_lines), [])
        self.assertEqual(state_store.overview_monsters.alive_monster, [])
        self.assertIsNone(state_store.snap)


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

    def test_every_production_zendriver_wait_has_an_explicit_owner(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbattle"
        violations: list[str] = []

        for source_file in source_root.glob("*.py"):
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "wait_for_zendriver"
                ):
                    continue
                owner = next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg == "owner"
                    ),
                    None,
                )
                if owner is None or (
                    isinstance(owner, ast.Constant) and owner.value is None
                ):
                    violations.append(f"{source_file.name}:{node.lineno}")

        self.assertEqual(violations, [])

    def test_production_never_awaits_zendriver_protocol_methods_directly(
        self,
    ) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbattle"
        protocol_methods = {
            "activate",
            "apply",
            "click",
            "evaluate",
            "get",
            "get_content",
            "get_position",
            "mouse_click",
            "mouse_move",
            "query_selector",
            "query_selector_all",
            "reload",
            "save_screenshot",
            "select",
            "select_all",
            "send",
            "send_keys",
            "set_value",
            "update_targets",
            "wait",
            "xpath",
        }
        formal_high_level_calls = {
            ("self._action", "click"),
            ("self.browser", "get"),
            ("self.browser", "wait"),
        }
        violations: list[str] = []

        for source_file in source_root.glob("*.py"):
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Await):
                    continue
                call = node.value
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in protocol_methods
                ):
                    continue
                if (
                    ast.unparse(call.func.value),
                    call.func.attr,
                ) in formal_high_level_calls:
                    continue
                violations.append(f"{source_file.name}:{node.lineno}:{call.func.attr}")

        self.assertEqual(violations, [])

    def test_removed_connection_classifier_is_not_reintroduced(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbattle"
        violations: list[str] = []

        for source_file in source_root.glob("*.py"):
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            if any(
                isinstance(node, ast.Name) and node.id == "is_connection_error"
                for node in ast.walk(tree)
            ):
                violations.append(source_file.name)

        self.assertEqual(violations, [])

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
        self.assertTrue(hasattr(BattleSession, "goto_ring_of_blood"))
        self.assertTrue(hasattr(BattleSession, "inspect_ring_of_blood"))
        self.assertTrue(hasattr(BattleSession, "start_ring_of_blood"))
        self.assertTrue(hasattr(BattleSession, "list_grindfest_options"))
        self.assertTrue(hasattr(BattleSession, "start_grindfest"))


if __name__ == "__main__":
    unittest.main()

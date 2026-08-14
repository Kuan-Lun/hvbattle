import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from hvbattle import (
    BattleActionKind,
    BattleActionOutcomeUnknownError,
    BattleActionRecoveryEvidence,
    BattleInterruptedError,
    BattleRecoveryExhaustedError,
    BattleRunner,
    BattleSession,
    BattleStopped,
    BattleTurnPhase,
    BattleTurnState,
    TurnDecision,
)
from hvbattle.hv_battle_action_manager import (
    ElementActionManager,
    _ActionMonitorState,
    _BattleActionState,
    _BattleExitState,
    _reconcile_recovery_state,
)
from hvbattle.recovery import (
    ActionDialogTracker,
    BattleRecoveryCoordinator,
    BattleRecoveryState,
)


def _monitor(
    *,
    sent: bool = True,
    completed: bool = True,
    status: int | None = 0,
    outcome: str | None = "error",
    request_age_ms: float | None = None,
) -> _ActionMonitorState:
    return _ActionMonitorState(
        sent=sent,
        sent_count=1 if sent else 0,
        request_age_ms=(5_000.0 if sent and request_age_ms is None else request_age_ms),
        completed=completed,
        status=status,
        outcome=outcome,
        log_mutations=0,
        response_parse_ok=False,
        response_has_textlog=False,
        response_has_pane_completion=False,
        response_has_error=False,
        response_has_reload=False,
        response_has_login=False,
    )


def _action_state(
    *,
    document_id: str = "document-before",
    monitor: _ActionMonitorState | None = None,
) -> _BattleActionState:
    return _BattleActionState(
        document_id=document_id,
        battle_node_id="battle-node",
        ready_state="complete",
        battle_present=True,
        log_revision="log",
        log_rows=1,
        latest_log="Round",
        round_text="Initializing arena (Round 1 / 10)",
        completion_present=False,
        battle_complete_present=False,
        finish_image_present=False,
        completion_revision="empty",
        next_floor_present=False,
        ponychart_present=False,
        action_controls=1,
        monitor=monitor,
    )


def _recovery_state(
    *,
    document_id: str = "document-after",
    realm: str = "persistent",
    phase: BattleTurnPhase | None = BattleTurnPhase.ACTIVE,
    log_revision: str | None = "log-after",
    action_controls: int = 1,
) -> BattleRecoveryState:
    return BattleRecoveryState(
        document_id=document_id,
        realm=realm,
        ready_state="complete",
        phase=phase,
        log_revision=log_revision,
        completion_revision="completion-after",
        action_controls=action_controls,
    )


def _recoverable_error() -> BattleActionOutcomeUnknownError:
    return BattleActionOutcomeUnknownError(
        "receipt missing",
        recovery_evidence=_recovery_evidence(),
    )


def _stalled_recoverable_error() -> BattleActionOutcomeUnknownError:
    return BattleActionOutcomeUnknownError(
        "single XHR remained pending",
        recovery_evidence=_stalled_xhr_evidence(),
    )


def _recovery_evidence(**changes: object) -> BattleActionRecoveryEvidence:
    evidence = BattleActionRecoveryEvidence(
        action_id="action-1",
        action_kind=BattleActionKind.TURN,
        selector="#mkey_3",
        click_started=True,
        xhr_pending_at_least_five_seconds=False,
        pre_click_document_id="document-before",
        post_click_document_id="document-after",
        dialog_action_id="action-1",
        dialog_category="server-communication-failed",
        xhr_sent=False,
        xhr_sent_count=0,
        xhr_completed=False,
        xhr_status=None,
        xhr_outcome=None,
    )
    return replace(evidence, **changes)


def _stalled_xhr_evidence(**changes: object) -> BattleActionRecoveryEvidence:
    evidence = _recovery_evidence(
        xhr_pending_at_least_five_seconds=True,
        post_click_document_id="document-before",
        dialog_action_id=None,
        dialog_category=None,
        xhr_sent=True,
        xhr_sent_count=1,
        xhr_completed=False,
        xhr_status=None,
        xhr_outcome=None,
    )
    return replace(evidence, **changes)


class RecoveryStateReconciliationTests(unittest.TestCase):
    def test_consistent_action_and_realm_probes_classify_active_page(self) -> None:
        state = _reconcile_recovery_state(
            _action_state(document_id="document-after"),
            _BattleExitState(
                document_id="document-after",
                realm="persistent",
                ready_state="complete",
                battle_present=True,
                finish_image_present=False,
                next_floor_present=False,
                ponychart_present=False,
            ),
        )

        self.assertIsNotNone(state)
        assert state is not None
        self.assertIs(state.phase, BattleTurnPhase.ACTIVE)

    def test_ponychart_without_battle_container_is_classified(self) -> None:
        action = _BattleActionState(
            document_id="document-after",
            battle_node_id=None,
            ready_state="complete",
            battle_present=False,
            log_revision=None,
            log_rows=0,
            latest_log=None,
            round_text=None,
            completion_present=False,
            battle_complete_present=False,
            finish_image_present=False,
            completion_revision=None,
            next_floor_present=False,
            ponychart_present=True,
            action_controls=0,
            monitor=None,
        )
        state = _reconcile_recovery_state(
            action,
            _BattleExitState(
                document_id="document-after",
                realm="persistent",
                ready_state="complete",
                battle_present=False,
                finish_image_present=False,
                next_floor_present=False,
                ponychart_present=True,
            ),
        )

        self.assertIsNotNone(state)
        assert state is not None
        self.assertIs(state.phase, BattleTurnPhase.CHALLENGE)

    def test_cross_document_probe_race_is_rejected(self) -> None:
        state = _reconcile_recovery_state(
            _action_state(document_id="document-one"),
            _BattleExitState(
                document_id="document-two",
                realm="persistent",
                ready_state="complete",
                battle_present=True,
                finish_image_present=False,
                next_floor_present=False,
                ponychart_present=False,
            ),
        )

        self.assertIsNone(state)


class ActionRecoveryContextTests(unittest.IsolatedAsyncioTestCase):
    def test_recovery_evidence_requires_bound_dialog_and_receipt_loss_shape(
        self,
    ) -> None:
        self.assertTrue(_recovery_evidence().matches_server_communication_failure)
        status_zero = _recovery_evidence(
            post_click_document_id="document-before",
            xhr_sent=True,
            xhr_sent_count=1,
            xhr_completed=True,
            xhr_status=0,
            xhr_outcome="error",
        )
        self.assertTrue(status_zero.matches_server_communication_failure)

        rejected_core = (
            {"action_kind": "turn"},
            {"click_started": 1},
            {"dialog_action_id": "another-action"},
            {"dialog_category": "other"},
            {"pre_click_document_id": "unknown"},
            {"post_click_document_id": "unknown"},
        )
        for changes in rejected_core:
            with self.subTest(changes=changes):
                self.assertFalse(
                    _recovery_evidence(**changes).matches_server_communication_failure
                )

        rejected_status_zero = (
            {"xhr_completed": 1},
            {"xhr_sent_count": True},
            {"xhr_sent": False},
            {"xhr_sent_count": 2},
            {"xhr_status": 500},
            {"xhr_outcome": "load"},
        )
        for changes in rejected_status_zero:
            with self.subTest(changes=changes):
                self.assertFalse(
                    replace(
                        status_zero,
                        **changes,
                    ).matches_server_communication_failure
                )
        self.assertTrue(
            replace(
                status_zero,
                xhr_completed=False,
                xhr_status=None,
                xhr_outcome=None,
            ).matches_server_communication_failure
        )
        self.assertFalse(
            replace(
                status_zero,
                xhr_completed=False,
                xhr_sent=True,
                xhr_sent_count=2,
                xhr_status=None,
                xhr_outcome=None,
            ).matches_server_communication_failure
        )
        self.assertFalse(
            _recovery_evidence(
                xhr_status=0,
                xhr_outcome="error",
            ).matches_server_communication_failure
        )

    def test_stalled_single_xhr_requires_exact_expired_same_document_shape(
        self,
    ) -> None:
        stalled = _stalled_xhr_evidence()

        self.assertTrue(stalled.matches_stalled_single_xhr)
        self.assertFalse(stalled.matches_server_communication_failure)
        self.assertTrue(stalled.allows_same_browser_recovery)

        rejected = (
            {"action_kind": BattleActionKind.NEXT_FLOOR},
            {"action_kind": "turn"},
            {"action_id": ""},
            {"selector": ""},
            {"click_started": 1},
            {"xhr_pending_at_least_five_seconds": False},
            {"xhr_pending_at_least_five_seconds": 1},
            {"pre_click_document_id": "unknown"},
            {"post_click_document_id": "unknown"},
            {"post_click_document_id": "document-after"},
            {"dialog_action_id": "action-1"},
            {"dialog_category": "server-communication-failed"},
            {"xhr_sent": False, "xhr_sent_count": 0},
            {"xhr_sent": False, "xhr_sent_count": 1},
            {"xhr_sent_count": 0},
            {"xhr_sent": 1},
            {"xhr_sent_count": True},
            {"xhr_sent_count": 2},
            {"xhr_completed": True},
            {"xhr_completed": 0},
            {"xhr_status": 0},
            {"xhr_outcome": "error"},
        )
        for changes in rejected:
            with self.subTest(changes=changes):
                evidence = replace(stalled, **changes)
                self.assertFalse(evidence.matches_stalled_single_xhr)
                self.assertFalse(evidence.allows_same_browser_recovery)

    def test_dialog_tracker_binds_category_to_active_action_token(self) -> None:
        tracker = ActionDialogTracker()
        tracker.begin("action-1")
        tracker.record("server-communication-failed")

        self.assertEqual(
            tracker.category_for("action-1"),
            "server-communication-failed",
        )
        self.assertIsNone(tracker.category_for("another-action"))

    async def test_unknown_action_exposes_structured_recovery_context(self) -> None:
        manager = object.__new__(ElementActionManager)
        manager._action_lock = asyncio.Lock()
        manager._select_for_single_click = AsyncMock(return_value=object())
        manager._click = AsyncMock()
        manager._cleanup_action_monitor = AsyncMock()
        manager._begin_dialog_observation = Mock()
        manager._get_dialog_category = Mock(return_value="server-communication-failed")
        before = _action_state(
            monitor=_monitor(
                sent=False,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        navigated = _action_state(document_id="document-after", monitor=None)
        manager._read_action_state = AsyncMock(side_effect=[before, navigated])

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await manager.click_and_wait_log_locator("#mkey_3", timeout=1e-9)

        evidence = raised.exception.recovery_evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertTrue(evidence.matches_server_communication_failure)
        self.assertEqual(evidence.action_kind, BattleActionKind.TURN)
        self.assertEqual(evidence.selector, "#mkey_3")
        self.assertEqual(evidence.pre_click_document_id, "document-before")
        self.assertEqual(evidence.post_click_document_id, "document-after")
        self.assertFalse(evidence.xhr_completed)
        action_id = manager._begin_dialog_observation.call_args.args[0]
        self.assertEqual(evidence.action_id, action_id)
        self.assertEqual(evidence.dialog_action_id, action_id)
        manager._get_dialog_category.assert_called_once_with(action_id)
        manager._click.assert_awaited_once_with(
            manager._select_for_single_click.return_value
        )

    async def test_partial_xhr_before_navigation_remains_receipt_unavailable(
        self,
    ) -> None:
        manager = object.__new__(ElementActionManager)
        manager._action_lock = asyncio.Lock()
        manager._select_for_single_click = AsyncMock(return_value=object())
        manager._click = AsyncMock()
        manager._cleanup_action_monitor = AsyncMock()
        manager._begin_dialog_observation = Mock()
        manager._get_dialog_category = Mock(return_value="server-communication-failed")
        before = _action_state(
            monitor=_monitor(
                sent=False,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        partial = _action_state(
            monitor=_monitor(
                sent=True,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        navigated = _action_state(document_id="document-after", monitor=None)
        reads = 0

        async def read_state(*_args: object, **_kwargs: object) -> _BattleActionState:
            nonlocal reads
            reads += 1
            if reads == 1:
                return before
            if reads == 2:
                return partial
            return navigated

        manager._read_action_state = AsyncMock(side_effect=read_state)
        manager._final_action_probe = AsyncMock(return_value=(navigated, None))

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await manager.click_and_wait_log_locator(
                "#mkey_3",
                timeout=0.001,
                check_interval=1e-9,
            )

        evidence = raised.exception.recovery_evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertTrue(evidence.matches_server_communication_failure)
        self.assertFalse(evidence.xhr_completed)
        self.assertTrue(evidence.xhr_sent)
        self.assertEqual(evidence.xhr_sent_count, 1)
        self.assertEqual(evidence.post_click_document_id, "document-after")

    async def test_five_second_same_document_single_xhr_is_recoverable_without_dialog(
        self,
    ) -> None:
        manager = object.__new__(ElementActionManager)
        manager._action_lock = asyncio.Lock()
        manager._select_for_single_click = AsyncMock(return_value=object())
        manager._click = AsyncMock()
        manager._cleanup_action_monitor = AsyncMock()
        manager._begin_dialog_observation = Mock()
        manager._get_dialog_category = Mock(return_value=None)
        before = _action_state(
            monitor=_monitor(
                sent=False,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        stalled = _action_state(
            monitor=_monitor(
                sent=True,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        manager._read_action_state = AsyncMock(side_effect=[before, stalled])

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await manager.click_and_wait_log_locator(
                "#ikey_2",
                timeout=1e-9,
                check_interval=1e-9,
            )

        evidence = raised.exception.recovery_evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertTrue(evidence.xhr_pending_at_least_five_seconds)
        self.assertTrue(evidence.matches_stalled_single_xhr)
        self.assertTrue(evidence.allows_same_browser_recovery)
        self.assertIsNone(evidence.dialog_category)
        manager._click.assert_awaited_once_with(
            manager._select_for_single_click.return_value
        )
        manager._cleanup_action_monitor.assert_not_awaited()

    async def test_recently_sent_xhr_is_not_recoverable_from_click_deadline_alone(
        self,
    ) -> None:
        manager = object.__new__(ElementActionManager)
        manager._action_lock = asyncio.Lock()
        manager._select_for_single_click = AsyncMock(return_value=object())
        manager._click = AsyncMock()
        manager._cleanup_action_monitor = AsyncMock()
        manager._begin_dialog_observation = Mock()
        manager._get_dialog_category = Mock(return_value=None)
        before = _action_state(
            monitor=_monitor(
                sent=False,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        recently_sent = _action_state(
            monitor=_monitor(
                sent=True,
                completed=False,
                status=None,
                outcome=None,
                request_age_ms=100.0,
            )
        )
        manager._read_action_state = AsyncMock(side_effect=[before, recently_sent])

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await manager.click_and_wait_log_locator(
                "#ikey_2",
                timeout=1e-9,
                check_interval=1e-9,
            )

        evidence = raised.exception.recovery_evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertFalse(evidence.xhr_pending_at_least_five_seconds)
        self.assertFalse(evidence.matches_stalled_single_xhr)
        self.assertFalse(evidence.allows_same_browser_recovery)
        manager._click.assert_awaited_once()

    async def test_turn_without_successful_post_click_probe_has_unknown_document(
        self,
    ) -> None:
        manager = object.__new__(ElementActionManager)
        manager._action_lock = asyncio.Lock()
        manager._select_for_single_click = AsyncMock(return_value=object())
        manager._click = AsyncMock()
        manager._cleanup_action_monitor = AsyncMock()
        manager._begin_dialog_observation = Mock()
        manager._get_dialog_category = Mock(return_value="server-communication-failed")
        before = _action_state(
            monitor=_monitor(
                sent=False,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        manager._read_action_state = AsyncMock(
            side_effect=[before, RuntimeError("post-click probe failed")]
        )

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await manager.click_and_wait_log_locator(
                "#mkey_3",
                timeout=1e-9,
                check_interval=1e-9,
            )

        evidence = raised.exception.recovery_evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence.post_click_document_id, "unknown")
        self.assertFalse(evidence.matches_server_communication_failure)
        manager._cleanup_action_monitor.assert_not_awaited()

    async def test_next_floor_preserves_partial_receipt_across_navigation(
        self,
    ) -> None:
        manager = object.__new__(ElementActionManager)
        manager._action_lock = asyncio.Lock()
        manager._select_for_single_click = AsyncMock(return_value=object())
        manager._click = AsyncMock()
        manager._cleanup_action_monitor = AsyncMock()
        manager._begin_dialog_observation = Mock()
        manager._get_dialog_category = Mock(return_value="server-communication-failed")
        before = _action_state(
            monitor=_monitor(
                sent=False,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        partial = _action_state(
            monitor=_monitor(
                sent=True,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        navigated = _action_state(document_id="document-after", monitor=None)
        reads = 0

        async def read_state(
            _action_id: str,
            *,
            arm_monitor: bool = False,
            probe_timeout: float = 3,
        ) -> _BattleActionState:
            nonlocal reads
            del probe_timeout
            reads += 1
            if reads == 1:
                self.assertTrue(arm_monitor)
                return before
            self.assertFalse(arm_monitor)
            if reads == 2:
                return partial
            return navigated

        manager._read_action_state = AsyncMock(side_effect=read_state)

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await manager.click_and_wait_transition_locator(
                "#btcp",
                timeout=0.001,
                check_interval=1e-9,
            )

        evidence = raised.exception.recovery_evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertTrue(evidence.matches_server_communication_failure)
        self.assertEqual(evidence.action_kind, BattleActionKind.NEXT_FLOOR)
        self.assertTrue(evidence.xhr_sent)
        self.assertEqual(evidence.xhr_sent_count, 1)
        self.assertFalse(evidence.xhr_completed)
        self.assertFalse(evidence.xhr_pending_at_least_five_seconds)
        self.assertEqual(evidence.post_click_document_id, "document-after")
        manager._cleanup_action_monitor.assert_not_awaited()

    async def test_next_floor_duplicate_receipt_is_preserved_and_rejected(
        self,
    ) -> None:
        manager = object.__new__(ElementActionManager)
        manager._action_lock = asyncio.Lock()
        manager._select_for_single_click = AsyncMock(return_value=object())
        manager._click = AsyncMock()
        manager._cleanup_action_monitor = AsyncMock()
        manager._begin_dialog_observation = Mock()
        manager._get_dialog_category = Mock(return_value="server-communication-failed")
        before = _action_state(
            monitor=_monitor(
                sent=False,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        duplicate = _action_state(
            monitor=replace(
                _monitor(
                    sent=True,
                    completed=False,
                    status=None,
                    outcome=None,
                ),
                sent_count=2,
            )
        )
        navigated = _action_state(document_id="document-after", monitor=None)
        reads = 0

        async def read_state(
            _action_id: str,
            *,
            arm_monitor: bool = False,
            probe_timeout: float = 3,
        ) -> _BattleActionState:
            nonlocal reads
            del arm_monitor, probe_timeout
            reads += 1
            if reads == 1:
                return before
            if reads == 2:
                return duplicate
            return navigated

        manager._read_action_state = AsyncMock(side_effect=read_state)

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await manager.click_and_wait_transition_locator(
                "#btcp",
                timeout=0.001,
                check_interval=1e-9,
            )

        evidence = raised.exception.recovery_evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence.xhr_sent_count, 2)
        self.assertFalse(evidence.matches_server_communication_failure)

    async def test_next_floor_without_post_click_probe_has_unknown_document(
        self,
    ) -> None:
        manager = object.__new__(ElementActionManager)
        manager._action_lock = asyncio.Lock()
        manager._select_for_single_click = AsyncMock(return_value=object())
        manager._click = AsyncMock()
        manager._cleanup_action_monitor = AsyncMock()
        manager._begin_dialog_observation = Mock()
        manager._get_dialog_category = Mock(return_value="server-communication-failed")
        before = _action_state(
            monitor=_monitor(
                sent=False,
                completed=False,
                status=None,
                outcome=None,
            )
        )
        manager._read_action_state = AsyncMock(
            side_effect=[before, RuntimeError("post-click probe failed")]
        )

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await manager.click_and_wait_transition_locator(
                "#btcp",
                timeout=1e-9,
                check_interval=1e-9,
            )

        evidence = raised.exception.recovery_evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence.post_click_document_id, "unknown")
        self.assertFalse(evidence.matches_server_communication_failure)
        manager._cleanup_action_monitor.assert_not_awaited()

    async def test_targeted_skill_preserves_unknown_action_for_runner(self) -> None:
        session = object.__new__(BattleSession)
        error = _recoverable_error()
        session.select_targeted_skill = AsyncMock(return_value=True)
        session.attack_monster = AsyncMock(side_effect=error)

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await BattleSession.attack_monster_by_skill(session, 3, "imperil")

        self.assertIs(raised.exception, error)

    async def test_selected_skill_target_uses_one_session_safety_boundary(self) -> None:
        session = object.__new__(BattleSession)
        session.attack_monster = AsyncMock(return_value=True)

        acted = await BattleSession.submit_selected_skill_target(
            session,
            3,
            "imperil",
        )

        self.assertTrue(acted)
        session.attack_monster.assert_awaited_once_with(3)

    async def test_selected_skill_target_wraps_non_recovery_error(self) -> None:
        session = object.__new__(BattleSession)
        timeout = TimeoutError("target click did not dispatch")
        session.attack_monster = AsyncMock(side_effect=timeout)

        with self.assertRaises(BattleInterruptedError) as raised:
            await BattleSession.submit_selected_skill_target(
                session,
                3,
                "imperil",
            )

        self.assertIs(raised.exception.__cause__, timeout)

    async def test_selected_skill_target_preserves_recovery_error(self) -> None:
        session = object.__new__(BattleSession)
        error = _recoverable_error()
        session.attack_monster = AsyncMock(side_effect=error)

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await BattleSession.submit_selected_skill_target(
                session,
                3,
                "imperil",
            )

        self.assertIs(raised.exception, error)


class BattleRecoveryCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def _coordinator(self) -> tuple[BattleRecoveryCoordinator, Mock, Mock]:
        actions = Mock()
        actions.read_recovery_state = AsyncMock()
        actions.reload_current_page = AsyncMock()
        actions.clear_page_action_state = AsyncMock(return_value="document-after")
        state_store = Mock()
        state_store.inspect = AsyncMock(
            return_value=SimpleNamespace(
                monsters={1: SimpleNamespace(alive=True)},
            )
        )
        state_store.reset = Mock()
        return BattleRecoveryCoordinator(actions, state_store), actions, state_store

    async def test_auto_reload_rebases_only_after_stable_parseable_active_state(
        self,
    ) -> None:
        coordinator, actions, state_store = self._coordinator()
        old = _recovery_state(document_id="document-before")
        fresh = _recovery_state()
        actions.read_recovery_state.side_effect = [
            old,
            fresh,
            fresh,
            fresh,
        ]

        recovered = await coordinator.recover(
            _recovery_evidence(),
            expected_realm="persistent",
            auto_reload_checks=2,
            recovery_checks=2,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertTrue(recovered)
        actions.reload_current_page.assert_not_awaited()
        actions.clear_page_action_state.assert_awaited_once_with(probe_timeout=1)
        state_store.inspect.assert_awaited_once_with()
        state_store.reset.assert_called_once_with()

    async def test_missing_auto_reload_reloads_current_page_once_then_rebases(
        self,
    ) -> None:
        coordinator, actions, state_store = self._coordinator()
        old = _recovery_state(document_id="document-before")
        transition = _recovery_state(
            phase=BattleTurnPhase.NEXT_FLOOR,
            log_revision=None,
            action_controls=0,
        )
        actions.read_recovery_state.side_effect = [
            old,
            old,
            transition,
            transition,
            transition,
        ]

        recovered = await coordinator.recover(
            _recovery_evidence(),
            expected_realm="persistent",
            auto_reload_checks=2,
            recovery_checks=2,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertTrue(recovered)
        actions.reload_current_page.assert_awaited_once_with(probe_timeout=1)
        state_store.inspect.assert_not_awaited()

    async def test_stalled_single_xhr_reloads_after_one_race_probe_and_rebases(
        self,
    ) -> None:
        coordinator, actions, state_store = self._coordinator()
        old = _recovery_state(document_id="document-before")
        fresh = _recovery_state()
        actions.read_recovery_state.side_effect = [old, fresh, fresh, fresh]

        recovered = await coordinator.recover(
            _stalled_xhr_evidence(),
            expected_realm="persistent",
            auto_reload_checks=4,
            recovery_checks=2,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertTrue(recovered)
        self.assertEqual(actions.read_recovery_state.await_count, 4)
        actions.reload_current_page.assert_awaited_once_with(probe_timeout=1)
        actions.clear_page_action_state.assert_awaited_once_with(probe_timeout=1)
        state_store.inspect.assert_awaited_once_with()
        state_store.reset.assert_called_once_with()

    async def test_stalled_xhr_skips_reload_when_race_probe_sees_new_document(
        self,
    ) -> None:
        coordinator, actions, state_store = self._coordinator()
        fresh = _recovery_state()
        actions.read_recovery_state.side_effect = [fresh, fresh, fresh]

        recovered = await coordinator.recover(
            _stalled_xhr_evidence(),
            expected_realm="persistent",
            recovery_checks=2,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertTrue(recovered)
        actions.reload_current_page.assert_not_awaited()
        actions.clear_page_action_state.assert_awaited_once_with(probe_timeout=1)
        state_store.inspect.assert_awaited_once_with()
        state_store.reset.assert_called_once_with()

    async def test_cross_probe_race_after_manual_reload_can_stabilize(self) -> None:
        coordinator, actions, _state_store = self._coordinator()
        old = _recovery_state(document_id="document-before")
        fresh = _recovery_state(
            phase=BattleTurnPhase.NEXT_FLOOR,
            log_revision=None,
            action_controls=0,
        )
        actions.read_recovery_state.side_effect = [
            old,
            old,
            None,
            fresh,
            fresh,
            fresh,
        ]

        recovered = await coordinator.recover(
            _recovery_evidence(),
            expected_realm="persistent",
            auto_reload_checks=2,
            recovery_checks=3,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertTrue(recovered)
        actions.reload_current_page.assert_awaited_once_with(probe_timeout=1)

    async def test_indeterminate_auto_reload_probe_never_triggers_manual_reload(
        self,
    ) -> None:
        coordinator, actions, state_store = self._coordinator()
        actions.read_recovery_state.side_effect = TimeoutError("navigation in flight")

        recovered = await coordinator.recover(
            _recovery_evidence(),
            expected_realm="persistent",
            auto_reload_checks=2,
            recovery_checks=2,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertFalse(recovered)
        self.assertEqual(actions.read_recovery_state.await_count, 1)
        actions.reload_current_page.assert_not_awaited()
        actions.clear_page_action_state.assert_not_awaited()
        state_store.reset.assert_not_called()

    async def test_stable_ponychart_document_rebases_for_runner_resolution(
        self,
    ) -> None:
        coordinator, actions, state_store = self._coordinator()
        challenge = _recovery_state(
            phase=BattleTurnPhase.CHALLENGE,
            log_revision=None,
            action_controls=0,
        )
        actions.read_recovery_state.return_value = challenge

        recovered = await coordinator.recover(
            _recovery_evidence(),
            expected_realm="persistent",
            auto_reload_checks=1,
            recovery_checks=2,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertTrue(recovered)
        state_store.inspect.assert_not_awaited()
        actions.reload_current_page.assert_not_awaited()

    async def test_complete_state_preserves_last_known_round_metadata(self) -> None:
        coordinator, actions, state_store = self._coordinator()
        complete = _recovery_state(
            phase=BattleTurnPhase.COMPLETE,
            log_revision="final-log",
            action_controls=0,
        )
        actions.read_recovery_state.return_value = complete

        recovered = await coordinator.recover(
            _recovery_evidence(),
            expected_realm="persistent",
            auto_reload_checks=1,
            recovery_checks=2,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertTrue(recovered)
        state_store.reset.assert_not_called()

    async def test_stability_contract_requires_at_least_two_reads(self) -> None:
        coordinator, _actions, _state_store = self._coordinator()

        with self.assertRaisesRegex(ValueError, "stable_checks"):
            await coordinator.recover(
                _recovery_evidence(),
                expected_realm="persistent",
                stable_checks=1,
            )

    async def test_changed_but_untrusted_document_fails_without_second_reload(
        self,
    ) -> None:
        coordinator, actions, state_store = self._coordinator()
        outside = _recovery_state(realm="outside")
        actions.read_recovery_state.return_value = outside

        recovered = await coordinator.recover(
            _recovery_evidence(),
            expected_realm="persistent",
            auto_reload_checks=1,
            recovery_checks=2,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertFalse(recovered)
        actions.reload_current_page.assert_not_awaited()
        actions.clear_page_action_state.assert_not_awaited()
        state_store.reset.assert_not_called()

    async def test_evidence_outside_exact_incident_shape_is_not_recoverable(
        self,
    ) -> None:
        coordinator, actions, state_store = self._coordinator()

        recovered = await coordinator.recover(
            _recovery_evidence(dialog_action_id="another-action"),
            expected_realm="persistent",
        )

        self.assertFalse(recovered)
        actions.read_recovery_state.assert_not_awaited()
        actions.reload_current_page.assert_not_awaited()
        state_store.reset.assert_not_called()

    async def test_younger_pending_xhr_never_reaches_recovery_probes(self) -> None:
        coordinator, actions, state_store = self._coordinator()

        recovered = await coordinator.recover(
            _stalled_xhr_evidence(xhr_pending_at_least_five_seconds=False),
            expected_realm="persistent",
        )

        self.assertFalse(recovered)
        actions.read_recovery_state.assert_not_awaited()
        actions.reload_current_page.assert_not_awaited()
        actions.clear_page_action_state.assert_not_awaited()
        state_store.reset.assert_not_called()

    async def test_cleanup_document_change_rejects_previously_stable_state(
        self,
    ) -> None:
        coordinator, actions, state_store = self._coordinator()
        fresh = _recovery_state()
        actions.read_recovery_state.return_value = fresh
        actions.clear_page_action_state.return_value = "document-after-cleanup"

        recovered = await coordinator.recover(
            _recovery_evidence(),
            expected_realm="persistent",
            auto_reload_checks=1,
            recovery_checks=2,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertFalse(recovered)
        state_store.reset.assert_not_called()

    async def test_final_state_change_after_active_parse_fails_closed(self) -> None:
        coordinator, actions, state_store = self._coordinator()
        fresh = _recovery_state()
        changed_again = _recovery_state(document_id="document-later")
        actions.read_recovery_state.side_effect = [fresh, fresh, changed_again]

        recovered = await coordinator.recover(
            _recovery_evidence(),
            expected_realm="persistent",
            auto_reload_checks=1,
            recovery_checks=2,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertFalse(recovered)
        state_store.inspect.assert_awaited_once_with()
        state_store.reset.assert_not_called()

    async def test_active_parse_timeout_is_not_stacked(self) -> None:
        coordinator, actions, state_store = self._coordinator()
        fresh = _recovery_state()
        actions.read_recovery_state.return_value = fresh
        state_store.inspect.side_effect = TimeoutError("parser command still live")

        recovered = await coordinator.recover(
            _recovery_evidence(),
            expected_realm="persistent",
            auto_reload_checks=1,
            recovery_checks=4,
            stable_checks=2,
            check_interval=1e-9,
            probe_timeout=1,
        )

        self.assertFalse(recovered)
        self.assertEqual(state_store.inspect.await_count, 1)
        state_store.reset.assert_not_called()


class BattleSessionRecoveryCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_delegates_recovery_and_resets_only_session_caches(
        self,
    ) -> None:
        session = object.__new__(BattleSession)
        session.battle_recovery = Mock()
        session.battle_recovery.recover = AsyncMock(return_value=True)
        session.action_dialog_tracker = Mock()
        session._completion_observed = True
        session._last_parser_warning_signature = (1, ("old",))
        session._last_reported_round_progress = (3, 10)
        session.turn = 8
        session.round = 3

        recovered = await BattleSession.recover_unknown_action(
            session,
            _recoverable_error(),
            expected_is_isekai=False,
        )

        self.assertTrue(recovered)
        session.battle_recovery.recover.assert_awaited_once()
        call = session.battle_recovery.recover.call_args
        self.assertEqual(call.args, (_recovery_evidence(),))
        self.assertEqual(call.kwargs["expected_realm"], "persistent")
        session.action_dialog_tracker.clear.assert_called_once_with()
        self.assertEqual(session.turn, 8)
        self.assertEqual(session.round, -1)
        self.assertFalse(session._completion_observed)


class _RunnerSession:
    def __init__(self) -> None:
        self.turn = -1
        self.current_round = 1
        self.total_rounds = 10
        self.battle_completion_observed = False
        self.recover_unknown_action = AsyncMock(return_value=True)
        self.go_next_floor = AsyncMock(return_value=True)
        self.acknowledge_battle_completion = AsyncMock()

    @property
    async def is_isekai(self) -> bool:
        return False

    async def is_in_battle(self) -> bool:
        return True

    def reset_battle_tracking(self) -> None:
        self.turn = -1

    async def resolve_ponychart(self) -> bool:
        return False

    async def prepare_turn_state(self) -> BattleTurnState:
        self.turn += 1
        return BattleTurnState(BattleTurnPhase.ACTIVE)


class BattleRunnerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_returns_to_prepare_and_makes_a_fresh_decision(self) -> None:
        session = _RunnerSession()
        strategy = Mock()
        error = _recoverable_error()
        strategy.take_turn = AsyncMock(side_effect=[error, TurnDecision.STOP])

        result = await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertIsInstance(result, BattleStopped)
        self.assertEqual(strategy.take_turn.await_count, 2)
        session.recover_unknown_action.assert_awaited_once_with(
            error,
            expected_is_isekai=False,
        )

    async def test_stalled_single_xhr_recovery_makes_a_fresh_decision(self) -> None:
        session = _RunnerSession()
        strategy = Mock()
        error = _stalled_recoverable_error()
        strategy.take_turn = AsyncMock(side_effect=[error, TurnDecision.STOP])

        result = await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertIsInstance(result, BattleStopped)
        self.assertEqual(strategy.take_turn.await_count, 2)
        session.recover_unknown_action.assert_awaited_once_with(
            error,
            expected_is_isekai=False,
        )

    async def test_second_stalled_xhr_before_receipt_exhausts_recovery(self) -> None:
        session = _RunnerSession()
        strategy = Mock()
        first = _stalled_recoverable_error()
        second = _stalled_recoverable_error()
        strategy.take_turn = AsyncMock(side_effect=[first, second])

        with self.assertRaises(BattleRecoveryExhaustedError) as raised:
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertIs(raised.exception.__cause__, second)
        self.assertEqual(strategy.take_turn.await_count, 2)
        session.recover_unknown_action.assert_awaited_once_with(
            first,
            expected_is_isekai=False,
        )

    async def test_second_unknown_before_receipt_fails_closed(self) -> None:
        session = _RunnerSession()
        strategy = Mock()
        first = _recoverable_error()
        second = _recoverable_error()
        strategy.take_turn = AsyncMock(side_effect=[first, second])

        with self.assertRaises(BattleRecoveryExhaustedError) as raised:
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertIs(raised.exception.__cause__, second)
        self.assertEqual(strategy.take_turn.await_count, 2)
        session.recover_unknown_action.assert_awaited_once_with(
            first,
            expected_is_isekai=False,
        )

    async def test_second_unbound_unknown_before_receipt_exhausts_browser(self) -> None:
        session = _RunnerSession()
        strategy = Mock()
        first = _recoverable_error()
        second = BattleActionOutcomeUnknownError("unbound receipt loss")
        strategy.take_turn = AsyncMock(side_effect=[first, second])

        with self.assertRaises(BattleRecoveryExhaustedError) as raised:
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertIs(raised.exception.__cause__, second)
        session.recover_unknown_action.assert_awaited_once_with(
            first,
            expected_is_isekai=False,
        )

    async def test_failed_eligible_recovery_raises_typed_exhaustion(self) -> None:
        session = _RunnerSession()
        session.recover_unknown_action.return_value = False
        strategy = Mock()
        error = _recoverable_error()
        strategy.take_turn = AsyncMock(side_effect=error)

        with self.assertRaises(BattleRecoveryExhaustedError) as raised:
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertIs(raised.exception.__cause__, error)

    async def test_confirmed_acted_decision_resets_consecutive_recovery_budget(
        self,
    ) -> None:
        session = _RunnerSession()
        strategy = Mock()
        first = _recoverable_error()
        second = _recoverable_error()
        strategy.take_turn = AsyncMock(
            side_effect=[
                first,
                TurnDecision.ACTED,
                second,
                TurnDecision.STOP,
            ]
        )

        result = await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertIsInstance(result, BattleStopped)
        self.assertEqual(session.recover_unknown_action.await_count, 2)

    async def test_confirmed_next_floor_resets_consecutive_recovery_budget(
        self,
    ) -> None:
        session = _RunnerSession()
        session.prepare_turn_state = AsyncMock(
            side_effect=[
                BattleTurnState(BattleTurnPhase.ACTIVE),
                BattleTurnState(BattleTurnPhase.NEXT_FLOOR),
                BattleTurnState(BattleTurnPhase.ACTIVE),
                BattleTurnState(BattleTurnPhase.ACTIVE),
            ]
        )
        strategy = Mock()
        first = _recoverable_error()
        second = _recoverable_error()
        strategy.take_turn = AsyncMock(side_effect=[first, second, TurnDecision.STOP])

        result = await BattleRunner(
            session,  # type: ignore[arg-type]
            strategy,
            sleep=AsyncMock(),
        ).run_current()

        self.assertIsInstance(result, BattleStopped)
        session.go_next_floor.assert_awaited_once_with()
        self.assertEqual(session.recover_unknown_action.await_count, 2)

    async def test_unknown_next_floor_before_receipt_exhausts_browser(self) -> None:
        session = _RunnerSession()
        session.prepare_turn_state = AsyncMock(
            side_effect=[
                BattleTurnState(BattleTurnPhase.ACTIVE),
                BattleTurnState(BattleTurnPhase.NEXT_FLOOR),
            ]
        )
        first = _recoverable_error()
        second = BattleActionOutcomeUnknownError("next-floor receipt missing")
        session.go_next_floor.side_effect = second
        strategy = Mock()
        strategy.take_turn = AsyncMock(side_effect=first)

        with self.assertRaises(BattleRecoveryExhaustedError) as raised:
            await BattleRunner(
                session,  # type: ignore[arg-type]
                strategy,
                sleep=AsyncMock(),
            ).run_current()

        self.assertIs(raised.exception.__cause__, second)
        session.recover_unknown_action.assert_awaited_once_with(
            first,
            expected_is_isekai=False,
        )


if __name__ == "__main__":
    unittest.main()

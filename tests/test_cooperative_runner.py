import asyncio
import unittest
from collections import deque
from unittest.mock import AsyncMock, Mock

from hvbattle import (
    BattleAbsent,
    BattleActionKind,
    BattleActionOutcomeUnknownError,
    BattleActionRecoveryEvidence,
    BattleCompleted,
    BattleInterruptedError,
    BattlePresence,
    BattleRecoveryExhaustedError,
    BattleRunner,
    BattleStepIdle,
    BattleStepIdleReason,
    BattleStepProgress,
    BattleStepProgressKind,
    BattleStopped,
    BattleTurnPhase,
    BattleTurnState,
    TurnDecision,
)


class _StepSession:
    def __init__(
        self,
        *phases: BattleTurnPhase,
        active: bool = True,
        presence: BattlePresence | None = None,
        ponychart_results: tuple[bool, ...] = (),
    ) -> None:
        self._phases = deque(phases)
        self._active = active
        self._presence = (
            (BattlePresence.ACTIVE if active else BattlePresence.ABSENT)
            if presence is None
            else presence
        )
        self._ponychart_results = deque(ponychart_results)
        self.turn = -1
        self.current_round = 1
        self.total_rounds = 10
        self.battle_completion_observed = False
        self.realm_probes = 0
        self.battle_probes = 0
        self.prepare_calls = 0
        self.go_next_floor = AsyncMock(return_value=True)
        self.acknowledge_battle_completion = AsyncMock()
        self.recover_unknown_action = AsyncMock(return_value=True)

    @property
    async def is_isekai(self) -> bool:
        self.realm_probes += 1
        return False

    async def is_in_battle(self) -> bool:
        self.battle_probes += 1
        return self._active

    async def inspect_battle_presence(self) -> BattlePresence:
        self.battle_probes += 1
        if self._presence is BattlePresence.COMPLETION:
            self.battle_completion_observed = True
        return self._presence

    def reset_battle_tracking(self) -> None:
        self.turn = -1
        self.battle_completion_observed = False

    async def resolve_ponychart(self) -> bool:
        if self._ponychart_results:
            return self._ponychart_results.popleft()
        return False

    async def prepare_turn_state(self) -> BattleTurnState:
        self.prepare_calls += 1
        phase = self._phases.popleft()
        if phase in {BattleTurnPhase.ACTIVE, BattleTurnPhase.NEXT_FLOOR}:
            self.turn += 1
        if phase is BattleTurnPhase.COMPLETE:
            self.battle_completion_observed = True
        return BattleTurnState(phase)


def _recoverable_error() -> BattleActionOutcomeUnknownError:
    return BattleActionOutcomeUnknownError(
        "receipt missing",
        recovery_evidence=BattleActionRecoveryEvidence(
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
        ),
    )


class CooperativeBattleRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_action_returns_at_first_confirmed_receipt(self) -> None:
        session = _StepSession(BattleTurnPhase.ACTIVE, BattleTurnPhase.ACTIVE)
        strategy = Mock()
        strategy.on_battle_started = AsyncMock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.ACTED)
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            sleep=AsyncMock(),
        )

        first = await runner.step()

        self.assertEqual(
            first,
            BattleStepProgress(
                kind=BattleStepProgressKind.TURN_ACTION_CONFIRMED,
                is_isekai=False,
                decision_count=1,
                current_round=1,
                total_rounds=10,
            ),
        )
        self.assertEqual(session.prepare_calls, 1)
        strategy.take_turn.assert_awaited_once_with(session)
        strategy.on_battle_started.assert_awaited_once_with(session)

        second = await runner.step()

        self.assertIsInstance(second, BattleStepProgress)
        self.assertEqual(session.prepare_calls, 2)
        self.assertEqual(strategy.take_turn.await_count, 2)
        strategy.on_battle_started.assert_awaited_once_with(session)

    async def test_ponychart_resolution_yields_before_realm_or_policy_probe(
        self,
    ) -> None:
        session = _StepSession(
            BattleTurnPhase.ACTIVE,
            ponychart_results=(True,),
        )
        strategy = Mock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.ACTED)

        result = await BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            sleep=AsyncMock(),
        ).step()

        self.assertEqual(
            result,
            BattleStepProgress(
                kind=BattleStepProgressKind.PONYCHART_RESOLVED,
                is_isekai=None,
                decision_count=0,
                current_round=1,
                total_rounds=10,
            ),
        )
        self.assertEqual(session.battle_probes, 0)
        self.assertEqual(session.realm_probes, 0)
        self.assertEqual(session.prepare_calls, 0)
        strategy.take_turn.assert_not_awaited()

    async def test_ponychart_during_pause_yields_after_first_resolution(self) -> None:
        session = _StepSession(
            BattleTurnPhase.ACTIVE,
            ponychart_results=(False, True, True),
        )
        strategy = Mock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.ACTED)
        pause = asyncio.Event()
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            wait_if_paused=pause.wait,
            challenge_poll_interval=0.001,
            sleep=AsyncMock(),
        )

        result = await runner.step()

        self.assertIsInstance(result, BattleStepProgress)
        assert isinstance(result, BattleStepProgress)
        self.assertIs(result.kind, BattleStepProgressKind.PONYCHART_RESOLVED)
        self.assertEqual(len(session._ponychart_results), 1)
        self.assertEqual(session.battle_probes, 0)
        strategy.take_turn.assert_not_awaited()

    async def test_next_floor_yields_before_completion_ack(self) -> None:
        session = _StepSession(
            BattleTurnPhase.NEXT_FLOOR,
            BattleTurnPhase.COMPLETE,
        )
        strategy = Mock()
        strategy.take_turn = AsyncMock()
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            sleep=AsyncMock(),
        )

        progressed = await runner.step()

        self.assertIsInstance(progressed, BattleStepProgress)
        assert isinstance(progressed, BattleStepProgress)
        self.assertIs(
            progressed.kind,
            BattleStepProgressKind.NEXT_FLOOR_CONFIRMED,
        )
        self.assertEqual(session.prepare_calls, 1)
        session.go_next_floor.assert_awaited_once_with()
        session.acknowledge_battle_completion.assert_not_awaited()

        completed = await runner.step()
        cached = await runner.step()

        self.assertEqual(completed, BattleCompleted(False, 1, 1, 10))
        self.assertIs(cached, completed)
        session.acknowledge_battle_completion.assert_awaited_once_with(
            expected_is_isekai=False
        )
        strategy.take_turn.assert_not_awaited()

    async def test_confirmed_completion_requires_explicit_reset_for_next_battle(
        self,
    ) -> None:
        session = _StepSession(BattleTurnPhase.COMPLETE, BattleTurnPhase.COMPLETE)
        strategy = Mock()
        strategy.on_battle_started = AsyncMock()
        strategy.take_turn = AsyncMock()
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            sleep=AsyncMock(),
        )

        first = await runner.step()
        cached = await runner.step()

        self.assertIs(cached, first)
        session.acknowledge_battle_completion.assert_awaited_once()

        runner.reset_for_next_battle()
        session._active = True
        completed_again = await runner.step()

        self.assertIsInstance(completed_again, BattleCompleted)
        self.assertEqual(session.acknowledge_battle_completion.await_count, 2)
        self.assertEqual(strategy.on_battle_started.await_count, 2)

    async def test_active_runner_cannot_discard_state_with_reset(self) -> None:
        session = _StepSession(BattleTurnPhase.ACTIVE)
        strategy = Mock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.ACTED)
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            sleep=AsyncMock(),
        )

        await runner.step()

        with self.assertRaisesRegex(RuntimeError, "only after confirmed"):
            runner.reset_for_next_battle()

    async def test_recovery_budget_survives_cooperative_yield(self) -> None:
        session = _StepSession(BattleTurnPhase.ACTIVE, BattleTurnPhase.ACTIVE)
        strategy = Mock()
        first_error = _recoverable_error()
        second_error = _recoverable_error()
        strategy.take_turn = AsyncMock(side_effect=[first_error, second_error])
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            sleep=AsyncMock(),
        )

        recovered = await runner.step()

        self.assertIsInstance(recovered, BattleStepProgress)
        assert isinstance(recovered, BattleStepProgress)
        self.assertIs(
            recovered.kind,
            BattleStepProgressKind.RECOVERY_RECONCILED,
        )

        with self.assertRaises(BattleRecoveryExhaustedError) as raised:
            await runner.step()

        self.assertIs(raised.exception.__cause__, second_error)
        session.recover_unknown_action.assert_awaited_once_with(
            first_error,
            expected_is_isekai=False,
        )

    async def test_idle_step_does_not_sleep_but_run_current_honors_delay(
        self,
    ) -> None:
        session = _StepSession(BattleTurnPhase.ACTIVE)
        strategy = Mock()
        strategy.take_turn = AsyncMock(return_value=TurnDecision.IDLE)
        sleep = AsyncMock()
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            idle_delay=3,
            sleep=sleep,
        )

        result = await runner.step()

        self.assertEqual(result, BattleStepIdle(retry_after=3))
        sleep.assert_not_awaited()

        session._phases.extend([BattleTurnPhase.ACTIVE, BattleTurnPhase.COMPLETE])
        strategy.take_turn.return_value = TurnDecision.IDLE
        completed = await runner.run_current()

        self.assertIsInstance(completed, BattleCompleted)
        sleep.assert_awaited_once_with(3)

    async def test_retryable_timeout_is_a_cooperative_deferred_result(self) -> None:
        session = _StepSession(BattleTurnPhase.ACTIVE)
        session.prepare_turn_state = AsyncMock(side_effect=TimeoutError("read"))
        strategy = Mock()
        strategy.take_turn = AsyncMock()
        sleep = AsyncMock()
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            timeout_retries=2,
            retry_delay=7,
            sleep=sleep,
        )

        deferred = await runner.step()

        self.assertEqual(
            deferred,
            BattleStepIdle(
                retry_after=7,
                reason=BattleStepIdleReason.RETRYABLE_TIMEOUT,
            ),
        )
        sleep.assert_not_awaited()
        strategy.take_turn.assert_not_awaited()

        with self.assertRaisesRegex(
            BattleInterruptedError,
            "Battle outcome is unknown after a turn timeout",
        ):
            await runner.step()

    async def test_unknown_exit_confirmation_yields_between_each_probe(self) -> None:
        session = _StepSession(
            BattleTurnPhase.ABSENT,
            BattleTurnPhase.ABSENT,
            BattleTurnPhase.ABSENT,
        )
        strategy = Mock()
        strategy.take_turn = AsyncMock()
        sleep = AsyncMock()
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            idle_delay=4,
            transition_checks=2,
            sleep=sleep,
        )

        first = await runner.step()
        session._active = False
        second = await runner.step()

        expected = BattleStepIdle(
            retry_after=4,
            reason=BattleStepIdleReason.TRANSITION_CONFIRMATION,
        )
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertEqual(session.battle_probes, 2)
        sleep.assert_not_awaited()
        strategy.take_turn.assert_not_awaited()

        with self.assertRaisesRegex(
            BattleInterruptedError,
            "disappeared without positive completion evidence",
        ):
            await runner.step()

    async def test_stop_returns_control_without_freezing_active_runner(self) -> None:
        session = _StepSession(BattleTurnPhase.ACTIVE, BattleTurnPhase.ACTIVE)
        strategy = Mock()
        strategy.take_turn = AsyncMock(
            side_effect=[TurnDecision.STOP, TurnDecision.ACTED]
        )
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            sleep=AsyncMock(),
        )

        stopped = await runner.step()
        resumed = await runner.step()

        self.assertIsInstance(stopped, BattleStopped)
        self.assertIsInstance(resumed, BattleStepProgress)
        assert isinstance(resumed, BattleStepProgress)
        self.assertIs(
            resumed.kind,
            BattleStepProgressKind.TURN_ACTION_CONFIRMED,
        )

    async def test_absent_battle_has_typed_non_mutating_result(self) -> None:
        session = _StepSession(active=False)
        strategy = Mock()
        strategy.take_turn = AsyncMock()

        result = await BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            sleep=AsyncMock(),
        ).step()

        self.assertEqual(result, BattleAbsent())
        session.go_next_floor.assert_not_awaited()
        session.acknowledge_battle_completion.assert_not_awaited()
        strategy.take_turn.assert_not_awaited()

    async def test_restart_completion_is_acknowledged_once_and_cached(self) -> None:
        session = _StepSession(presence=BattlePresence.COMPLETION)
        strategy = Mock()
        strategy.on_battle_started = AsyncMock()
        strategy.take_turn = AsyncMock()
        runner = BattleRunner(  # type: ignore[arg-type]
            session,
            strategy,
            sleep=AsyncMock(),
        )

        completed = await runner.step()
        cached = await runner.step()

        self.assertEqual(completed, BattleCompleted(False, 0, 1, 10))
        self.assertIs(cached, completed)
        session.acknowledge_battle_completion.assert_awaited_once_with(
            expected_is_isekai=False
        )
        strategy.on_battle_started.assert_not_awaited()
        strategy.take_turn.assert_not_awaited()


class CooperativeStepContractTests(unittest.TestCase):
    def test_idle_delay_rejects_negative_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be negative"):
            BattleStepIdle(-0.01)


if __name__ == "__main__":
    unittest.main()

import asyncio
import unittest
from unittest.mock import ANY, AsyncMock, Mock

from hvbattle import (
    ActionIntentRecordedAuditEvent,
    ActionOutcomeUnknownAuditEvent,
    ActionReceiptConfirmedAuditEvent,
    ActionReceiptEvidence,
    ActionSubmittedAuditEvent,
    AuditEvent,
    AuditEventBus,
    AuditPublicationError,
    BattleActionKind,
    BattleInterruptedError,
    PonyChartResolutionOutcome,
)
from hvbattle._timing import SemanticDeadline
from hvbattle.audit import _ActionAuditTrail
from hvbattle.hv_battle_ponychart import PonyChart, _PonyChartReceiptContext
from hvbattle.testing import TestingAuditEventBus


def _receipt_context() -> _PonyChartReceiptContext:
    deadline = SemanticDeadline.after(15.0)
    return _PonyChartReceiptContext(
        "0123456789abcdef0123456789abcdef",
        "https://hentaiverse.org/battle",
        "https://hentaiverse.org",
        deadline,
        deadline,
    )


def _challenge(
    event_bus: AuditEventBus,
) -> PonyChart:
    driver = Mock(headless=True)
    challenge = PonyChart(driver, audit_event_bus=event_bus)
    challenge._check = AsyncMock(return_value=True)  # type: ignore[method-assign]
    challenge._arm_challenge_receipt_monitor = AsyncMock(  # type: ignore[method-assign]
        return_value=_receipt_context()
    )
    challenge._capture_pony_chart_image = AsyncMock(  # type: ignore[method-assign]
        return_value=b"challenge"
    )
    challenge._predict_labels = AsyncMock(  # type: ignore[method-assign]
        return_value=("Applejack",)
    )
    challenge._select_and_submit_answer = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    challenge._wait_for_challenge_receipt = AsyncMock()  # type: ignore[method-assign]
    challenge._retain_pony_chart_image = AsyncMock()  # type: ignore[method-assign]
    return challenge


class PonyChartAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_submission_and_transition_publish_one_event_each(
        self,
    ) -> None:
        events: list[AuditEvent] = []
        challenge = _challenge(TestingAuditEventBus(events.append))

        async def exact_submit(
            _labels: tuple[str, ...],
            *,
            monitor_id: str,
            deadline: SemanticDeadline,
            audit_trail: _ActionAuditTrail,
        ) -> bool:
            del deadline, monitor_id
            audit_trail.record_intent()
            audit_trail.mark_submitted()
            return True

        challenge._select_and_submit_answer = AsyncMock(  # type: ignore[method-assign]
            side_effect=exact_submit
        )

        outcome = await challenge.check()

        self.assertIs(outcome, PonyChartResolutionOutcome.SUBMISSION_CONFIRMED)
        self.assertEqual(
            [type(event) for event in events],
            [
                ActionIntentRecordedAuditEvent,
                ActionSubmittedAuditEvent,
                ActionReceiptConfirmedAuditEvent,
            ],
        )
        intent, submitted, receipt = events
        assert isinstance(intent, ActionIntentRecordedAuditEvent)
        assert isinstance(submitted, ActionSubmittedAuditEvent)
        assert isinstance(receipt, ActionReceiptConfirmedAuditEvent)
        self.assertEqual(intent.action_id, submitted.action_id)
        self.assertEqual(submitted.action_id, receipt.action_id)
        self.assertIs(submitted.action_kind, BattleActionKind.PONYCHART)
        self.assertIs(receipt.action_kind, BattleActionKind.PONYCHART)
        self.assertIs(
            receipt.evidence,
            ActionReceiptEvidence.PONYCHART_SUBMISSION_TRANSITION,
        )
        challenge._select_and_submit_answer.assert_awaited_once_with(  # type: ignore[attr-defined]
            ("Applejack",),
            monitor_id=ANY,
            deadline=ANY,
            audit_trail=ANY,
        )

    async def test_exact_submit_followed_by_failure_publishes_unknown(self) -> None:
        events: list[AuditEvent] = []
        challenge = _challenge(TestingAuditEventBus(events.append))

        async def submit_then_fail(
            _labels: tuple[str, ...],
            *,
            monitor_id: str,
            deadline: SemanticDeadline,
            audit_trail: _ActionAuditTrail,
        ) -> bool:
            del deadline, monitor_id
            audit_trail.record_intent()
            audit_trail.mark_submitted()
            raise BattleInterruptedError(
                "receipt unavailable",
                diagnostic_code="battle.ponychart.submit-outcome-unknown",
            )

        challenge._select_and_submit_answer = AsyncMock(  # type: ignore[method-assign]
            side_effect=submit_then_fail
        )
        challenge._wait_for_challenge_receipt = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("receipt unavailable")
        )

        with self.assertRaises(BattleInterruptedError):
            await challenge.check()

        self.assertEqual(
            [type(event) for event in events],
            [
                ActionIntentRecordedAuditEvent,
                ActionSubmittedAuditEvent,
                ActionOutcomeUnknownAuditEvent,
            ],
        )

    async def test_absent_or_expired_before_monitor_has_no_action_event(self) -> None:
        for state in ("absent", "expired"):
            with self.subTest(state=state):
                events: list[AuditEvent] = []
                challenge = _challenge(TestingAuditEventBus(events.append))
                if state == "absent":
                    challenge._check = AsyncMock(  # type: ignore[method-assign]
                        return_value=False
                    )
                else:
                    challenge._arm_challenge_receipt_monitor = AsyncMock(  # type: ignore[method-assign]
                        return_value=None
                    )

                await challenge.check()

                self.assertEqual(events, [])

    async def test_writer_failure_prevents_ponychart_submission(self) -> None:
        writer_error = OSError("/private/journal credential=secret")
        calls: list[AuditEvent] = []

        def fail(event: AuditEvent) -> None:
            calls.append(event)
            raise writer_error

        bus = TestingAuditEventBus(fail)
        challenge = _challenge(bus)

        async def attempt_submit(
            _labels: tuple[str, ...],
            *,
            monitor_id: str,
            deadline: SemanticDeadline,
            audit_trail: _ActionAuditTrail,
        ) -> bool:
            del deadline, monitor_id
            audit_trail.record_intent()
            return True

        challenge._select_and_submit_answer = AsyncMock(  # type: ignore[method-assign]
            side_effect=attempt_submit
        )

        with self.assertRaises(AuditPublicationError) as raised:
            await challenge.check()

        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0], ActionIntentRecordedAuditEvent)
        self.assertIs(raised.exception.__cause__, writer_error)

    async def test_cancellation_after_ponychart_intent_records_unknown(self) -> None:
        events: list[AuditEvent] = []
        challenge = _challenge(TestingAuditEventBus(events.append))

        async def cancel_after_intent(
            _labels: tuple[str, ...],
            *,
            monitor_id: str,
            deadline: SemanticDeadline,
            audit_trail: _ActionAuditTrail,
        ) -> bool:
            del deadline, monitor_id
            audit_trail.record_intent()
            raise asyncio.CancelledError

        challenge._select_and_submit_answer = AsyncMock(  # type: ignore[method-assign]
            side_effect=cancel_after_intent
        )

        with self.assertRaises(asyncio.CancelledError):
            await challenge.check()

        self.assertEqual(
            [type(event) for event in events],
            [ActionIntentRecordedAuditEvent, ActionOutcomeUnknownAuditEvent],
        )


if __name__ == "__main__":
    unittest.main()

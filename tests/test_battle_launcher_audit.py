import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from hvbrowser import MaintenanceNavigationBlocker, Realm

from hvbattle import (
    ActionIntentRecordedAuditEvent,
    ActionNotSubmittedAuditEvent,
    ActionOutcomeUnknownAuditEvent,
    ActionReceiptConfirmedAuditEvent,
    ActionReceiptEvidence,
    ActionReconciliationConfirmedAuditEvent,
    ActionSubmittedAuditEvent,
    AuditEvent,
    AuditEventBus,
    AuditPublicationError,
    BattleActionKind,
)
from hvbattle.battle_launcher import BattleLauncher
from hvbattle.testing import TestingAuditEventBus


def _launcher(
    events: list[AuditEvent] | None = None,
    *,
    event_bus: AuditEventBus | None = None,
) -> tuple[BattleLauncher, SimpleNamespace, Mock]:
    page = SimpleNamespace(evaluate=AsyncMock())
    browser = SimpleNamespace(page=page)
    bus = (
        event_bus
        if event_bus is not None
        else TestingAuditEventBus(events.append if events is not None else None)
    )
    launcher = BattleLauncher(
        browser,  # type: ignore[arg-type]
        Mock(),
        audit_event_bus=bus,
    )
    lifecycle = Mock()
    lifecycle.enable = AsyncMock()
    lifecycle.trigger = Mock()
    lifecycle.wait = AsyncMock()
    lifecycle.close = Mock()
    launcher._main_document_lifecycle = Mock(return_value=lifecycle)  # type: ignore[method-assign]
    return launcher, page, lifecycle


class BattleLauncherAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_form_kind_publishes_exact_submission_and_receipt(
        self,
    ) -> None:
        for action_kind in (
            BattleActionKind.ARENA,
            BattleActionKind.RING_OF_BLOOD,
            BattleActionKind.GRINDFEST,
        ):
            with self.subTest(action_kind=action_kind):
                events: list[AuditEvent] = []
                launcher, page, _lifecycle = _launcher(events)
                page.evaluate.return_value = "submitted"
                launcher._confirm_battle_form_receipt = AsyncMock(  # type: ignore[method-assign]
                    return_value=MaintenanceNavigationBlocker.ACTIVE
                )

                result = await launcher._submit_battle_form(
                    "atomic-form-script",
                    kind=action_kind,
                    battle_id=42,
                    route="ar",
                    expected_realm=Realm.PERSISTENT,
                )

                self.assertEqual(result, "submitted")
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
                self.assertIs(submitted.action_kind, action_kind)
                self.assertIs(receipt.action_kind, action_kind)
                self.assertIs(
                    receipt.evidence,
                    ActionReceiptEvidence.BATTLE_FORM_ACTIVE,
                )

    async def test_acknowledgement_loss_with_trusted_receipt_does_not_infer_submit(
        self,
    ) -> None:
        events: list[AuditEvent] = []
        launcher, page, _lifecycle = _launcher(events)
        page.evaluate.side_effect = RuntimeError("acknowledgement lost")
        launcher._confirm_battle_form_receipt = AsyncMock(  # type: ignore[method-assign]
            return_value=MaintenanceNavigationBlocker.CHALLENGE
        )

        result = await launcher._submit_battle_form(
            "atomic-form-script",
            kind=BattleActionKind.ARENA,
            battle_id=42,
            route="ar",
            expected_realm=Realm.PERSISTENT,
        )

        self.assertEqual(result, "submitted")
        self.assertEqual(len(events), 2)
        intent, receipt = events
        self.assertIsInstance(intent, ActionIntentRecordedAuditEvent)
        self.assertIsInstance(receipt, ActionReconciliationConfirmedAuditEvent)
        assert isinstance(receipt, ActionReconciliationConfirmedAuditEvent)
        self.assertIs(
            receipt.evidence,
            ActionReceiptEvidence.BATTLE_FORM_CHALLENGE,
        )

    async def test_proven_submission_with_missing_receipt_is_unknown(self) -> None:
        events: list[AuditEvent] = []
        launcher, page, lifecycle = _launcher(events)
        page.evaluate.return_value = "submitted"
        lifecycle.wait.side_effect = TimeoutError("receipt missing")

        with self.assertRaises(TimeoutError):
            await launcher._submit_battle_form(
                "atomic-form-script",
                kind=BattleActionKind.GRINDFEST,
                battle_id=42,
                route="gr",
                expected_realm=Realm.PERSISTENT,
            )

        self.assertEqual(
            [type(event) for event in events],
            [
                ActionIntentRecordedAuditEvent,
                ActionSubmittedAuditEvent,
                ActionOutcomeUnknownAuditEvent,
            ],
        )

    async def test_cancellation_after_intent_records_unknown(self) -> None:
        events: list[AuditEvent] = []
        launcher, page, lifecycle = _launcher(events)
        page.evaluate.side_effect = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await launcher._submit_battle_form(
                "atomic-form-script",
                kind=BattleActionKind.ARENA,
                battle_id=42,
                route="ar",
                expected_realm=Realm.PERSISTENT,
            )

        self.assertEqual(
            [type(event) for event in events],
            [ActionIntentRecordedAuditEvent, ActionOutcomeUnknownAuditEvent],
        )
        lifecycle.close.assert_called_once_with()

    async def test_pre_submit_rejection_closes_intent_as_not_submitted(self) -> None:
        events: list[AuditEvent] = []
        launcher, page, lifecycle = _launcher(events)
        page.evaluate.return_value = "option-unavailable"

        result = await launcher._submit_battle_form(
            "atomic-form-script",
            kind=BattleActionKind.RING_OF_BLOOD,
            battle_id=42,
            route="rb",
            expected_realm=Realm.PERSISTENT,
        )

        self.assertEqual(result, "option-unavailable")
        self.assertEqual(
            [type(event) for event in events],
            [ActionIntentRecordedAuditEvent, ActionNotSubmittedAuditEvent],
        )
        lifecycle.wait.assert_not_awaited()

    async def test_writer_failure_prevents_form_submission(
        self,
    ) -> None:
        writer_error = OSError("/private/journal credential=secret")
        calls: list[AuditEvent] = []

        def fail(event: AuditEvent) -> None:
            calls.append(event)
            raise writer_error

        bus = TestingAuditEventBus(fail)
        launcher, page, _lifecycle = _launcher(event_bus=bus)
        page.evaluate.return_value = "submitted"
        launcher._confirm_battle_form_receipt = AsyncMock(  # type: ignore[method-assign]
            return_value=MaintenanceNavigationBlocker.ACTIVE
        )

        with self.assertRaises(AuditPublicationError) as raised:
            await launcher._submit_battle_form(
                "atomic-form-script",
                kind=BattleActionKind.ARENA,
                battle_id=42,
                route="ar",
                expected_realm=Realm.PERSISTENT,
            )

        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0], ActionIntentRecordedAuditEvent)
        page.evaluate.assert_not_called()
        self.assertIs(raised.exception.__cause__, writer_error)


if __name__ == "__main__":
    unittest.main()

import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, dataclass
from types import MappingProxyType
from unittest.mock import Mock

from hvbattle import (
    ActionIntentRecordedAuditEvent,
    ActionNotSubmittedAuditEvent,
    ActionNotSubmittedReason,
    ActionOutcomeUnknownAuditEvent,
    ActionOutcomeUnknownReason,
    ActionReceiptConfirmedAuditEvent,
    ActionReceiptEvidence,
    ActionReconciliationConfirmedAuditEvent,
    ActionSubmittedAuditEvent,
    AuditEvent,
    AuditEventBus,
    AuditEventType,
    AuditPublicationError,
    AuditValue,
    BattleActionKind,
    DurableAuditEventWriter,
)
from hvbattle.battle_launcher import BattleLauncher
from hvbattle.hv_battle_action_manager import ElementActionManager
from hvbattle.hv_battle_ponychart import PonyChart
from hvbattle.session import BattleSession
from hvbattle.testing import TestingAuditEventBus

_ACTION_ID = "0123456789abcdef0123456789abcdef"


class AuditEventContractTests(unittest.TestCase):
    def test_events_are_frozen_and_expose_only_immutable_machine_payloads(
        self,
    ) -> None:
        events = (
            ActionSubmittedAuditEvent(_ACTION_ID, BattleActionKind.TURN),
            ActionIntentRecordedAuditEvent(_ACTION_ID, BattleActionKind.TURN),
            ActionReceiptConfirmedAuditEvent(
                _ACTION_ID,
                BattleActionKind.TURN,
                ActionReceiptEvidence.XHR_ACK_COMBAT_LOG_MUTATION,
            ),
            ActionOutcomeUnknownAuditEvent(
                _ACTION_ID,
                BattleActionKind.NEXT_FLOOR,
                ActionOutcomeUnknownReason.POSITIVE_NEXT_PHASE_EVIDENCE_MISSING,
            ),
            ActionNotSubmittedAuditEvent(
                _ACTION_ID,
                BattleActionKind.FINAL_BATTLE_EXIT,
                ActionNotSubmittedReason.PRE_MUTATION_ABORTED,
            ),
            ActionReconciliationConfirmedAuditEvent(
                _ACTION_ID,
                BattleActionKind.TURN,
                ActionReceiptEvidence.SAME_BROWSER_RECOVERY_STABLE_STATE,
            ),
        )

        for event in events:
            with self.subTest(event_type=event.event_type):
                self.assertIsInstance(event, AuditEvent)
                payload = event.audit_payload()
                self.assertEqual(payload["action_id"], _ACTION_ID)
                self.assertIn(
                    payload["action_kind"],
                    {
                        BattleActionKind.TURN.value,
                        BattleActionKind.NEXT_FLOOR.value,
                        BattleActionKind.FINAL_BATTLE_EXIT.value,
                    },
                )
                self.assertNotIn("selector", payload)
                self.assertNotIn("document_id", payload)
                with self.assertRaises(TypeError):
                    payload["page"] = "secret"  # type: ignore[index]
                with self.assertRaises(FrozenInstanceError):
                    event.action_id = "f" * 32  # type: ignore[misc]

    def test_event_envelope_and_typed_codes_reject_unbounded_input(self) -> None:
        invalid_action_ids = (
            "",
            "A" * 32,
            "a" * 31,
            "a" * 33,
            "../../credential.txt",
            "<html>page content</html>",
        )
        for action_id in invalid_action_ids:
            with (
                self.subTest(action_id=action_id),
                self.assertRaisesRegex(ValueError, "action_id"),
            ):
                ActionSubmittedAuditEvent(action_id, BattleActionKind.TURN)

        with self.assertRaisesRegex(TypeError, "action_kind"):
            ActionSubmittedAuditEvent(
                _ACTION_ID,
                "turn",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(TypeError, "evidence"):
            ActionReceiptConfirmedAuditEvent(
                _ACTION_ID,
                BattleActionKind.TURN,
                "page-changed",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(TypeError, "reason"):
            ActionNotSubmittedAuditEvent(
                _ACTION_ID,
                BattleActionKind.FINAL_BATTLE_EXIT,
                "click",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(TypeError, "reason"):
            ActionOutcomeUnknownAuditEvent(
                _ACTION_ID,
                BattleActionKind.TURN,
                "unknown",  # type: ignore[arg-type]
            )


class AuditEventBusTests(unittest.TestCase):
    def test_production_bus_requires_an_explicit_durable_writer(self) -> None:
        with self.assertRaisesRegex(TypeError, "DurableAuditEventWriter"):
            AuditEventBus(None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "DurableAuditEventWriter"):
            AuditEventBus(lambda _event: None)  # type: ignore[arg-type]

        received: list[AuditEvent] = []
        bus = AuditEventBus(DurableAuditEventWriter(received.append))
        event = ActionSubmittedAuditEvent(_ACTION_ID, BattleActionKind.TURN)

        bus.publish(event)
        bus.raise_for_failure()

        self.assertEqual(received, [event])
        self.assertTrue(bus.healthy)
        self.assertIsNone(bus.failure)

    def test_writer_failure_is_sticky_safe_and_raised_only_when_polled(self) -> None:
        secret = "credential=/private/account.json"
        writer_error = RuntimeError(secret)
        calls: list[AuditEvent] = []

        def fail(event: AuditEvent) -> None:
            calls.append(event)
            raise writer_error

        bus = TestingAuditEventBus(fail)
        first = ActionSubmittedAuditEvent(_ACTION_ID, BattleActionKind.TURN)
        second = ActionOutcomeUnknownAuditEvent(
            _ACTION_ID,
            BattleActionKind.TURN,
            ActionOutcomeUnknownReason.AUTHORITATIVE_RECEIPT_MISSING,
        )

        bus.publish(first)
        bus.publish(second)

        self.assertEqual(calls, [first])
        self.assertFalse(bus.healthy)
        failure = bus.failure
        self.assertIsInstance(failure, AuditPublicationError)
        assert failure is not None
        self.assertEqual(failure.event_type, AuditEventType.ACTION_SUBMITTED.value)
        self.assertEqual(failure.error_type, "RuntimeError")
        self.assertIs(failure.cause, writer_error)
        self.assertNotIn(secret, str(failure))
        with self.assertRaises(AuditPublicationError) as raised:
            bus.raise_for_failure()
        self.assertIs(raised.exception, failure)
        self.assertIs(raised.exception.__cause__, writer_error)

    def test_structural_event_from_composition_root_uses_the_same_bus(self) -> None:
        @dataclass(frozen=True, slots=True)
        class CursorAdvanced:
            event_type: str = "post-battle-cursor-advanced"

            def audit_payload(self) -> Mapping[str, AuditValue]:
                return MappingProxyType({"cursor": 42, "committed": True})

        received: list[AuditEvent] = []
        bus = TestingAuditEventBus(received.append)
        event = CursorAdvanced()

        bus.publish(event)

        self.assertEqual(received, [event])
        self.assertTrue(bus.healthy)

    def test_structural_event_type_is_bounded_before_writer_call(self) -> None:
        @dataclass(frozen=True, slots=True)
        class UnsafeEvent:
            event_type: str = "../../private/account.json"

            def audit_payload(self) -> Mapping[str, AuditValue]:
                return MappingProxyType({})

        received: list[AuditEvent] = []
        bus = TestingAuditEventBus(received.append)

        with self.assertRaisesRegex(ValueError, "event_type"):
            bus.publish(UnsafeEvent())

        self.assertEqual(received, [])
        self.assertTrue(bus.healthy)

    def test_failure_metadata_sanitizes_an_untrusted_exception_type(self) -> None:
        class WriterFailure(Exception):
            pass

        WriterFailure.__name__ = "../../CredentialPath"

        def fail(_event: AuditEvent) -> None:
            raise WriterFailure("secret")

        bus = TestingAuditEventBus(fail)
        bus.publish(ActionSubmittedAuditEvent(_ACTION_ID, BattleActionKind.TURN))

        failure = bus.failure
        assert failure is not None
        self.assertEqual(failure.error_type, "Exception")
        self.assertNotIn("CredentialPath", str(failure))
        with self.assertRaisesRegex(ValueError, "event_type"):
            AuditPublicationError("../../private", RuntimeError("secret"))


class DurableAuditCompositionTests(unittest.TestCase):
    def test_mutation_compositions_require_explicit_bus_injection(self) -> None:
        constructors = (
            (BattleSession, ()),
            (BattleLauncher, (Mock(), Mock())),
            (ElementActionManager, (Mock(),)),
            (PonyChart, (Mock(),)),
        )

        for constructor, args in constructors:
            with (
                self.subTest(constructor=constructor.__name__),
                self.assertRaisesRegex(TypeError, "audit_event_bus"),
            ):
                constructor(*args)

    def test_mutation_compositions_reject_none_before_driver_use(self) -> None:
        constructors = (
            (BattleSession, ()),
            (BattleLauncher, (Mock(), Mock())),
            (ElementActionManager, (Mock(),)),
            (PonyChart, (Mock(),)),
        )

        for constructor, args in constructors:
            with (
                self.subTest(constructor=constructor.__name__),
                self.assertRaisesRegex(TypeError, "must be AuditEventBus"),
            ):
                constructor(*args, audit_event_bus=None)


if __name__ == "__main__":
    unittest.main()

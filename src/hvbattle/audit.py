"""Bounded write-ahead audit events for irreversible battle actions.

Every action records a durable intent before the first operation that can reach
the server.  A hard crash can therefore leave an unmatched intent, which a
composition root can detect and fail closed instead of replaying the action.
Page content, selectors, document identifiers, paths, and credentials
deliberately have no place in these contracts.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from .contracts import BattleActionKind

_ACTION_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_ERROR_TYPE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_EVENT_TYPE_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}\Z")

type AuditValue = str | int | bool | None


@runtime_checkable
class AuditEvent(Protocol):
    """Structural event contract shared by hvbattle and its composition root."""

    @property
    def event_type(self) -> str:
        """Return a bounded lowercase ASCII machine discriminator."""

        ...

    def audit_payload(self) -> Mapping[str, AuditValue]:
        """Return an immutable mapping containing only bounded machine data."""

        ...


class AuditEventType(StrEnum):
    """Stable discriminator for one battle-domain audit event."""

    ACTION_INTENT_RECORDED = "action-intent-recorded"
    ACTION_SUBMITTED = "action-submitted"
    ACTION_NOT_SUBMITTED = "action-not-submitted"
    AUTHORITATIVE_ACTION_RECEIPT_CONFIRMED = "authoritative-action-receipt-confirmed"
    ACTION_OUTCOME_UNKNOWN = "action-outcome-unknown"
    ACTION_RECONCILIATION_CONFIRMED = "action-reconciliation-confirmed"


class ActionReceiptEvidence(StrEnum):
    """Bounded evidence codes accepted by the action receipt predicates."""

    XHR_ACK_COMBAT_LOG_MUTATION = "xhr-ack+combat-log-mutation"
    XHR_ACK_COMBAT_LOG_REVISION = "xhr-ack+combat-log-revision"
    XHR_ACK_BATTLE_COMPLETION = "xhr-ack+battle-completion"
    XHR_ACK_ROUND_COMPLETION = "xhr-ack+round-completion"
    XHR_ACK_COMPLETION_PANE = "xhr-ack+completion-pane"
    PONYCHART_PRESENT = "ponychart-present"
    BATTLE_COMPLETION_PRESENT = "battle-completion-present"
    BATTLE_GENERATION_ROUND_ADVANCED = "battle-generation+round-advanced"
    BATTLE_ROUND_ADVANCED = "battle-round-advanced"
    BATTLE_GENERATION_ROUND_INITIALIZED = "battle-generation+round-initialized"
    BATTLE_ROUND_INITIALIZED = "battle-round-initialized"
    PONYCHART_SUBMISSION_TRANSITION = "ponychart-submission-transition"
    BATTLE_FORM_ACTIVE = "battle-form-active"
    BATTLE_FORM_CHALLENGE = "battle-form-challenge"
    FINAL_EXIT_NEW_DOCUMENT = "final-exit-new-document"
    SAME_BROWSER_RECOVERY_STABLE_STATE = "same-browser-recovery-stable-state"


class ActionOutcomeUnknownReason(StrEnum):
    """Bounded reason codes for actions lacking authoritative completion."""

    AUTHORITATIVE_RECEIPT_MISSING = "authoritative-receipt-missing"
    POSITIVE_NEXT_PHASE_EVIDENCE_MISSING = "positive-next-phase-evidence-missing"
    PONYCHART_RECEIPT_MISSING = "ponychart-receipt-missing"
    BATTLE_FORM_RECEIPT_MISSING = "battle-form-receipt-missing"
    FINAL_EXIT_RECEIPT_MISSING = "final-exit-receipt-missing"


class ActionNotSubmittedReason(StrEnum):
    """Bounded evidence that an intent ended before server submission."""

    COMMAND_REJECTED = "command-rejected"
    CHALLENGE_EXPIRED = "challenge-expired"
    PRE_MUTATION_ABORTED = "pre-mutation-aborted"


@dataclass(frozen=True, slots=True)
class ActionIntentRecordedAuditEvent:
    """A durable write-ahead boundary recorded before an irreversible action."""

    action_id: str
    action_kind: BattleActionKind
    event_type: AuditEventType = field(
        default=AuditEventType.ACTION_INTENT_RECORDED,
        init=False,
    )

    def __post_init__(self) -> None:
        _validate_action_envelope(self.action_id, self.action_kind)

    def audit_payload(self) -> Mapping[str, AuditValue]:
        return MappingProxyType(
            {
                "action_id": self.action_id,
                "action_kind": self.action_kind.value,
            }
        )


def _validate_action_envelope(
    action_id: object,
    action_kind: object,
) -> None:
    if (
        not isinstance(action_id, str)
        or _ACTION_ID_PATTERN.fullmatch(action_id) is None
    ):
        raise ValueError("action_id must be a lowercase 32-character hex token")
    if not isinstance(action_kind, BattleActionKind):
        raise TypeError("action_kind must be BattleActionKind")


@dataclass(frozen=True, slots=True)
class ActionSubmittedAuditEvent:
    """An action-specific monitor proved exactly one submission."""

    action_id: str
    action_kind: BattleActionKind
    event_type: AuditEventType = field(
        default=AuditEventType.ACTION_SUBMITTED,
        init=False,
    )

    def __post_init__(self) -> None:
        _validate_action_envelope(self.action_id, self.action_kind)

    def audit_payload(self) -> Mapping[str, AuditValue]:
        return MappingProxyType(
            {
                "action_id": self.action_id,
                "action_kind": self.action_kind.value,
            }
        )


@dataclass(frozen=True, slots=True)
class ActionNotSubmittedAuditEvent:
    """The action ended with proof that no server submission occurred."""

    action_id: str
    action_kind: BattleActionKind
    reason: ActionNotSubmittedReason
    event_type: AuditEventType = field(
        default=AuditEventType.ACTION_NOT_SUBMITTED,
        init=False,
    )

    def __post_init__(self) -> None:
        _validate_action_envelope(self.action_id, self.action_kind)
        if not isinstance(self.reason, ActionNotSubmittedReason):
            raise TypeError("reason must be ActionNotSubmittedReason")

    def audit_payload(self) -> Mapping[str, AuditValue]:
        return MappingProxyType(
            {
                "action_id": self.action_id,
                "action_kind": self.action_kind.value,
                "reason": self.reason.value,
            }
        )


@dataclass(frozen=True, slots=True)
class ActionReceiptConfirmedAuditEvent:
    """Authoritative action completion evidence was accepted."""

    action_id: str
    action_kind: BattleActionKind
    evidence: ActionReceiptEvidence
    event_type: AuditEventType = field(
        default=AuditEventType.AUTHORITATIVE_ACTION_RECEIPT_CONFIRMED,
        init=False,
    )

    def __post_init__(self) -> None:
        _validate_action_envelope(self.action_id, self.action_kind)
        if not isinstance(self.evidence, ActionReceiptEvidence):
            raise TypeError("evidence must be ActionReceiptEvidence")

    def audit_payload(self) -> Mapping[str, AuditValue]:
        return MappingProxyType(
            {
                "action_id": self.action_id,
                "action_kind": self.action_kind.value,
                "evidence": self.evidence.value,
            }
        )


@dataclass(frozen=True, slots=True)
class ActionOutcomeUnknownAuditEvent:
    """A recorded intent crossed an uncertain server-mutation boundary."""

    action_id: str
    action_kind: BattleActionKind
    reason: ActionOutcomeUnknownReason
    event_type: AuditEventType = field(
        default=AuditEventType.ACTION_OUTCOME_UNKNOWN,
        init=False,
    )

    def __post_init__(self) -> None:
        _validate_action_envelope(self.action_id, self.action_kind)
        if not isinstance(self.reason, ActionOutcomeUnknownReason):
            raise TypeError("reason must be ActionOutcomeUnknownReason")

    def audit_payload(self) -> Mapping[str, AuditValue]:
        return MappingProxyType(
            {
                "action_id": self.action_id,
                "action_kind": self.action_kind.value,
                "reason": self.reason.value,
            }
        )


@dataclass(frozen=True, slots=True)
class ActionReconciliationConfirmedAuditEvent:
    """Authoritative recovery resolved a previously ambiguous action."""

    action_id: str
    action_kind: BattleActionKind
    evidence: ActionReceiptEvidence
    event_type: AuditEventType = field(
        default=AuditEventType.ACTION_RECONCILIATION_CONFIRMED,
        init=False,
    )

    def __post_init__(self) -> None:
        _validate_action_envelope(self.action_id, self.action_kind)
        if not isinstance(self.evidence, ActionReceiptEvidence):
            raise TypeError("evidence must be ActionReceiptEvidence")

    def audit_payload(self) -> Mapping[str, AuditValue]:
        return MappingProxyType(
            {
                "action_id": self.action_id,
                "action_kind": self.action_kind.value,
                "evidence": self.evidence.value,
            }
        )


type BattleAuditEvent = (
    ActionIntentRecordedAuditEvent
    | ActionSubmittedAuditEvent
    | ActionNotSubmittedAuditEvent
    | ActionReceiptConfirmedAuditEvent
    | ActionOutcomeUnknownAuditEvent
    | ActionReconciliationConfirmedAuditEvent
)
type AuditEventWriter = Callable[[AuditEvent], None]


@dataclass(frozen=True, slots=True)
class DurableAuditEventWriter:
    """Capability for a writer that durably commits before returning.

    Constructing this capability is an explicit promise by the composition
    root: a successful call means the event has reached stable storage.  A
    plain callback is deliberately not accepted by :class:`AuditEventBus` so
    live code cannot accidentally substitute a best-effort or no-op sink.
    """

    write: AuditEventWriter

    def __post_init__(self) -> None:
        if not callable(self.write):
            raise TypeError("write must be callable")

    def __call__(self, event: AuditEvent) -> None:
        self.write(event)


@dataclass(slots=True)
class _ActionAuditTrail:
    """Publish a correlated write-ahead action lifecycle exactly once."""

    event_bus: AuditEventBus
    action_id: str
    action_kind: BattleActionKind
    intent_recorded: bool = False
    submitted: bool = False
    not_submitted: bool = False
    receipt_confirmed: bool = False
    outcome_unknown: bool = False
    reconciliation_confirmed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.event_bus, AuditEventBus):
            raise TypeError("event_bus must be AuditEventBus")
        _validate_action_envelope(self.action_id, self.action_kind)

    @property
    def resolved(self) -> bool:
        """Whether the intent has a replay-safe or explicitly ambiguous result."""

        return bool(
            self.not_submitted
            or self.receipt_confirmed
            or self.outcome_unknown
            or self.reconciliation_confirmed
        )

    def _publish(self, event: AuditEvent) -> None:
        self.event_bus.publish(event)
        self.event_bus.raise_for_failure()

    def record_intent(self) -> None:
        """Synchronously publish intent and fail before any external mutation."""

        if self.intent_recorded:
            return
        self._publish(ActionIntentRecordedAuditEvent(self.action_id, self.action_kind))
        self.intent_recorded = True

    def _require_intent(self) -> None:
        if not self.intent_recorded:
            raise RuntimeError("action intent must be recorded before its outcome")

    def mark_submitted(self) -> None:
        self._require_intent()
        if self.submitted or self.resolved:
            return
        self._publish(ActionSubmittedAuditEvent(self.action_id, self.action_kind))
        self.submitted = True

    def mark_not_submitted(self, reason: ActionNotSubmittedReason) -> None:
        self._require_intent()
        if self.submitted or self.resolved:
            return
        self._publish(
            ActionNotSubmittedAuditEvent(
                self.action_id,
                self.action_kind,
                reason,
            )
        )
        self.not_submitted = True

    def confirm_receipt(self, evidence: ActionReceiptEvidence) -> None:
        self._require_intent()
        if (
            self.receipt_confirmed
            or self.not_submitted
            or self.outcome_unknown
            or self.reconciliation_confirmed
        ):
            return
        self._publish(
            ActionReceiptConfirmedAuditEvent(
                self.action_id,
                self.action_kind,
                evidence,
            )
        )
        self.receipt_confirmed = True

    def mark_outcome_unknown(self, reason: ActionOutcomeUnknownReason) -> None:
        self._require_intent()
        if (
            self.outcome_unknown
            or self.receipt_confirmed
            or self.not_submitted
            or self.reconciliation_confirmed
        ):
            return
        self._publish(
            ActionOutcomeUnknownAuditEvent(
                self.action_id,
                self.action_kind,
                reason,
            )
        )
        self.outcome_unknown = True

    def confirm_reconciliation(self, evidence: ActionReceiptEvidence) -> None:
        """Resolve an ambiguous acknowledgement with authoritative evidence."""

        self._require_intent()
        if (
            self.reconciliation_confirmed
            or self.receipt_confirmed
            or self.not_submitted
        ):
            return
        self._publish(
            ActionReconciliationConfirmedAuditEvent(
                self.action_id,
                self.action_kind,
                evidence,
            )
        )
        self.reconciliation_confirmed = True


def _safe_error_type(error: Exception) -> str:
    candidate = type(error).__name__
    if _ERROR_TYPE_PATTERN.fullmatch(candidate) is None:
        return "Exception"
    return candidate


class AuditPublicationError(RuntimeError):
    """A writer failed; its potentially sensitive message is never rendered."""

    diagnostic_code = "battle.audit-publication-failed"

    def __init__(self, event_type: str, cause: Exception) -> None:
        if (
            not isinstance(event_type, str)
            or _EVENT_TYPE_PATTERN.fullmatch(event_type) is None
        ):
            raise ValueError(
                "event_type must be a bounded lowercase ASCII machine code"
            )
        if not isinstance(cause, Exception):
            raise TypeError("cause must be Exception")
        self.event_type = str(event_type)
        self.error_type = _safe_error_type(cause)
        self.cause = cause
        super().__init__(
            "Battle audit event publication failed; "
            f"event_type={self.event_type}; error_type={self.error_type}"
        )


class AuditEventBus:
    """Synchronous publisher with a sticky, caller-polled failure latch.

    Writer exceptions never escape :meth:`publish`.  The first failure is
    retained for the owning scheduler to surface at its next safe boundary;
    later events do not call a writer whose integrity is already unknown.
    The constructor requires an explicit durable-writer capability.  There is
    no writer-less production state.
    """

    def __init__(self, writer: DurableAuditEventWriter) -> None:
        if not isinstance(writer, DurableAuditEventWriter):
            raise TypeError("writer must be DurableAuditEventWriter")
        self._writer = writer
        self._failure: AuditPublicationError | None = None
        self._lock = RLock()

    @property
    def failure(self) -> AuditPublicationError | None:
        """Return the first publication failure without raising it."""

        with self._lock:
            return self._failure

    @property
    def healthy(self) -> bool:
        """Whether no writer failure has been observed."""

        return self.failure is None

    def publish(self, event: AuditEvent) -> None:
        """Publish one typed event, latching rather than raising writer errors."""

        if not isinstance(event, AuditEvent):
            raise TypeError("event must implement AuditEvent")
        event_type_value = event.event_type
        if (
            not isinstance(event_type_value, str)
            or _EVENT_TYPE_PATTERN.fullmatch(event_type_value) is None
        ):
            raise ValueError(
                "event_type must be a bounded lowercase ASCII machine code"
            )
        event_type = str(event_type_value)
        with self._lock:
            if self._failure is not None:
                return
            try:
                self._writer(event)
            except Exception as error:
                self._failure = AuditPublicationError(event_type, error)

    def raise_for_failure(self) -> None:
        """Raise the sticky failure at an explicitly chosen safe boundary."""

        failure = self.failure
        if failure is not None:
            raise failure from failure.cause

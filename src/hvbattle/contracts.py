"""Stable contracts between battle sessions, runners, and client policies."""

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

_ACTION_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True, slots=True)
class ArenaOption:
    """One explicit Arena start option in the order exposed by the server."""

    battle_id: int
    token: str | None = None
    challenge_name: str | None = field(kw_only=True)
    exp_multiplier: float | None = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class RingOfBloodOption:
    """One currently submit-capable Ring of Blood action."""

    battle_id: int
    challenge_name: str
    exp_multiplier: float
    entry_cost: int


@dataclass(frozen=True, slots=True)
class RingOfBloodChallenge:
    """One Ring row, including rows that cannot currently be started."""

    challenge_name: str
    exp_multiplier: float | None
    entry_cost: int | None
    start_action: RingOfBloodOption | None = None

    def __post_init__(self) -> None:
        action = self.start_action
        if action is not None and (
            action.challenge_name != self.challenge_name
            or action.exp_multiplier != self.exp_multiplier
            or action.entry_cost != self.entry_cost
        ):
            raise ValueError("Ring start action metadata must match its challenge")

    @property
    def startable(self) -> bool:
        """Whether the server exposed a submit action for this challenge."""

        return self.start_action is not None


@dataclass(frozen=True, slots=True)
class RingOfBloodSnapshot:
    """Read-only Ring rows, submit actions, and current token balance."""

    tokens_of_blood: int
    options: tuple[RingOfBloodOption, ...]
    challenges: tuple[RingOfBloodChallenge, ...]

    def __post_init__(self) -> None:
        derived_options = tuple(
            challenge.start_action
            for challenge in self.challenges
            if challenge.start_action is not None
        )
        if derived_options != self.options:
            raise ValueError("Ring snapshot options must match challenge start actions")


class RingOfBloodStartOutcome(StrEnum):
    """The authoritative pre-submit result of one explicit Ring challenge."""

    SUBMITTED = "submitted"
    INSUFFICIENT_TOKENS = "insufficient-tokens"
    OPTION_UNAVAILABLE = "option-unavailable"
    STATE_CHANGED = "state-changed"


@dataclass(frozen=True, slots=True)
class GrindfestOption:
    """One explicit GrindFest start option exposed by the server."""

    battle_id: int


@dataclass(frozen=True, slots=True)
class BattleCompleted:
    """Immutable summary; zero round values mean metadata was not observed."""

    is_isekai: bool
    decision_count: int
    final_round: int
    total_rounds: int


@dataclass(frozen=True, slots=True)
class BattleStopped:
    """Strategy yield summary; zero round values mean metadata was not observed."""

    is_isekai: bool
    decision_count: int
    current_round: int
    total_rounds: int


@dataclass(frozen=True, slots=True)
class BattleAbsent:
    """No active battle was present when a cooperative runner was probed."""


class BattlePresence(StrEnum):
    """Battle state represented by one observation or startup reconciliation."""

    ABSENT = "absent"
    ACTIVE = "active"
    COMPLETION = "completion"


class PonyChartResolutionOutcome(StrEnum):
    """The authoritative outcome of one PonyChart resolution attempt."""

    NOT_PRESENT = "not-present"
    SUBMISSION_CONFIRMED = "submission-confirmed"
    EXPIRED_WITHOUT_SUBMISSION = "expired-without-submission"


class BattleStepProgressKind(StrEnum):
    """A safe cooperative yield boundary reached while a battle remains active."""

    PONYCHART_RESOLVED = "ponychart-resolved"
    PONYCHART_EXPIRED_WITHOUT_SUBMISSION = "ponychart-expired-without-submission"
    TURN_ACTION_CONFIRMED = "turn-action-confirmed"
    NEXT_FLOOR_CONFIRMED = "next-floor-confirmed"
    RECOVERY_RECONCILED = "recovery-reconciled"


@dataclass(frozen=True, slots=True)
class BattleStepProgress:
    """One confirmed progress boundary after which another actor may run safely.

    ``is_isekai`` is unknown only when a PonyChart challenge returned to battle
    before the runner could inspect the battle realm.
    """

    kind: BattleStepProgressKind
    is_isekai: bool | None
    decision_count: int
    current_round: int
    total_rounds: int


class BattleStepIdleReason(StrEnum):
    """Why a cooperative runner yielded without changing server state."""

    PAUSED = "paused"
    POLICY = "policy"
    STATE_NOT_READY = "state-not-ready"
    TRANSITION_CONFIRMATION = "transition-confirmation"


@dataclass(frozen=True, slots=True)
class BattleStepIdle:
    """No mutation was made; retry this runner after the recommended delay."""

    retry_after: float
    reason: BattleStepIdleReason = BattleStepIdleReason.POLICY

    def __post_init__(self) -> None:
        if (
            not isinstance(self.retry_after, (int, float))
            or isinstance(self.retry_after, bool)
            or not math.isfinite(self.retry_after)
            or self.retry_after < 0
        ):
            raise ValueError("retry_after must be a finite non-negative number")


type BattleStepResult = (
    BattleAbsent | BattleStepProgress | BattleStepIdle | BattleCompleted | BattleStopped
)


_DIAGNOSTIC_CODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.:-]*\Z")
_DIAGNOSTIC_CODE_MAX_LENGTH = 128


def _validate_diagnostic_code(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _DIAGNOSTIC_CODE_MAX_LENGTH
        or _DIAGNOSTIC_CODE_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(
            "diagnostic_code must be a bounded lowercase ASCII machine code"
        )
    return value


class BattleInterruptedError(RuntimeError):
    """The battle cannot continue without violating a safety boundary."""

    def __init__(self, message: str, *, diagnostic_code: str) -> None:
        self.diagnostic_code = _validate_diagnostic_code(diagnostic_code)
        super().__init__(message)


class BattleStateReadinessError(BattleInterruptedError):
    """An active battle document never became safe for turn policy."""

    def __init__(
        self,
        *,
        observation_count: int,
        diagnostic_path: str | None,
        diagnostic_error_type: str | None,
    ) -> None:
        if (
            not isinstance(observation_count, int)
            or isinstance(observation_count, bool)
            or observation_count < 1
        ):
            raise ValueError("observation_count must be a positive integer")
        if diagnostic_path is not None and not isinstance(diagnostic_path, str):
            raise TypeError("diagnostic_path must be str or None")
        if diagnostic_error_type is not None and not isinstance(
            diagnostic_error_type, str
        ):
            raise TypeError("diagnostic_error_type must be str or None")
        self.observation_count = observation_count
        self.diagnostic_path = diagnostic_path
        self.diagnostic_error_type = diagnostic_error_type
        super().__init__(
            "Battle state did not become ready before the bounded readiness "
            "deadline",
            diagnostic_code="battle.state-readiness-exhausted",
        )


class BattleRecoveryExhaustedError(BattleInterruptedError):
    """A verified receipt-loss incident exhausted same-browser recovery."""


class BattleActionKind(StrEnum):
    """One bounded kind of battle-domain mutation."""

    TURN = "turn"
    NEXT_FLOOR = "next-floor"
    PONYCHART = "ponychart"
    ARENA = "arena"
    RING_OF_BLOOD = "ring-of-blood"
    GRINDFEST = "grindfest"
    FINAL_BATTLE_EXIT = "final-battle-exit"


@dataclass(frozen=True, slots=True)
class BattleActionRecoveryEvidence:
    """Immutable observations bound to one submitted action token."""

    action_id: str
    action_kind: BattleActionKind
    selector: str
    click_started: bool
    xhr_pending_at_least_five_seconds: bool
    pre_click_document_id: str
    post_click_document_id: str
    dialog_action_id: str | None
    dialog_category: str | None
    xhr_sent: bool
    xhr_sent_count: int
    xhr_completed: bool
    xhr_status: int | None
    xhr_outcome: str | None

    @property
    def _has_valid_action_envelope(self) -> bool:
        return bool(
            isinstance(self.action_kind, BattleActionKind)
            and self.action_kind in {BattleActionKind.TURN, BattleActionKind.NEXT_FLOOR}
            and isinstance(self.action_id, str)
            and _ACTION_ID_PATTERN.fullmatch(self.action_id) is not None
            and isinstance(self.selector, str)
            and self.selector
            and self.click_started is True
            and isinstance(self.pre_click_document_id, str)
            and self.pre_click_document_id
            and self.pre_click_document_id != "unknown"
            and isinstance(self.post_click_document_id, str)
            and self.post_click_document_id
            and self.post_click_document_id != "unknown"
        )

    @property
    def matches_server_communication_failure(self) -> bool:
        """Whether a bound dialog explains the missing terminal action receipt."""
        bound_action_failure = bool(
            self._has_valid_action_envelope
            and self.dialog_action_id == self.action_id
            and self.dialog_category == "server-communication-failed"
        )
        status_zero_receipt = bool(
            self.xhr_completed is True
            and self.xhr_sent is True
            and type(self.xhr_sent_count) is int
            and self.xhr_sent_count == 1
            and type(self.xhr_status) is int
            and self.xhr_status == 0
            and self.xhr_outcome == "error"
        )
        terminal_receipt_unavailable = bool(
            self.xhr_completed is False
            and type(self.xhr_sent) is bool
            and type(self.xhr_sent_count) is int
            and self.xhr_status is None
            and self.xhr_outcome is None
            and (
                (not self.xhr_sent and self.xhr_sent_count == 0)
                or (self.xhr_sent and self.xhr_sent_count == 1)
            )
        )
        return bound_action_failure and (
            status_zero_receipt or terminal_receipt_unavailable
        )

    @property
    def matches_stalled_single_xhr(self) -> bool:
        """Whether one battle-action XHR remained pending for five seconds."""
        return bool(
            self._has_valid_action_envelope
            and self.action_kind in {BattleActionKind.TURN, BattleActionKind.NEXT_FLOOR}
            and self.xhr_pending_at_least_five_seconds is True
            and self.pre_click_document_id == self.post_click_document_id
            and self.dialog_action_id is None
            and self.dialog_category is None
            and self.xhr_sent is True
            and type(self.xhr_sent_count) is int
            and self.xhr_sent_count == 1
            and self.xhr_completed is False
            and self.xhr_status is None
            and self.xhr_outcome is None
        )

    @property
    def allows_same_browser_recovery(self) -> bool:
        """Whether immutable evidence permits one guarded same-browser reload."""
        return bool(
            self.matches_server_communication_failure or self.matches_stalled_single_xhr
        )


class BattleActionOutcomeUnknownError(RuntimeError):
    """A submitted action did not produce authoritative completion evidence."""

    def __init__(
        self,
        message: str,
        *,
        recovery_evidence: BattleActionRecoveryEvidence | None = None,
    ) -> None:
        super().__init__(message)
        self.recovery_evidence = recovery_evidence


class BattleTurnPhase(StrEnum):
    """The explicit page state observed while preparing a strategy decision."""

    ABSENT = "absent"
    CHALLENGE = "challenge"
    NOT_READY = "not-ready"
    ACTIVE = "active"
    NEXT_FLOOR = "next-floor"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class BattleTurnState:
    """One typed turn-preparation result without sentinel return values."""

    phase: BattleTurnPhase
    log_lines: tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        """Whether the state needs an action to advance the battle."""
        return self.phase in {
            BattleTurnPhase.ACTIVE,
            BattleTurnPhase.NEXT_FLOOR,
        }

    @property
    def strategy_actionable(self) -> bool:
        """Whether client strategy code should make a policy decision."""
        return self.phase is BattleTurnPhase.ACTIVE


class TurnDecision(StrEnum):
    """What the single-battle runner should do after a strategy decision."""

    ACTED = "acted"
    IDLE = "idle"
    STOP = "stop"

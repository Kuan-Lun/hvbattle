"""Stable contracts between battle sessions, runners, and client policies."""

from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class ArenaOption:
    """One explicit Arena start option in the order exposed by the server."""

    battle_id: int
    token: str | None = None
    challenge_name: str | None = field(default=None, kw_only=True, compare=False)
    exp_multiplier: float | None = field(default=None, kw_only=True, compare=False)


@dataclass(frozen=True, slots=True)
class RingOfBloodOption:
    """One currently startable Ring of Blood challenge."""

    battle_id: int
    challenge_name: str
    exp_multiplier: float
    entry_cost: int


@dataclass(frozen=True, slots=True)
class RingOfBloodSnapshot:
    """Read-only Ring of Blood choices and the current token balance."""

    tokens_of_blood: int
    options: tuple[RingOfBloodOption, ...]


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


class BattleInterruptedError(RuntimeError):
    """The page left battle without positive completion evidence."""


class BattleRecoveryExhaustedError(BattleInterruptedError):
    """A verified receipt-loss incident exhausted same-browser recovery."""


class BattleActionKind(StrEnum):
    """The submitted control whose receipt became ambiguous."""

    TURN = "turn"
    NEXT_FLOOR = "next-floor"


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
            and isinstance(self.action_id, str)
            and self.action_id
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
        """Whether one turn XHR remained pending for at least five seconds."""
        return bool(
            self._has_valid_action_envelope
            and self.action_kind is BattleActionKind.TURN
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

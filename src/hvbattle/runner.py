"""Policy-neutral cooperative runner for exactly one already-active battle."""

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable

from hvbrowser.runtime import (
    ZendriverOperationTimeout,
    is_browser_generation_error,
    setup_logger,
)

from ._failure_safety import contains_log_persistence_error
from .contracts import (
    BattleAbsent,
    BattleActionOutcomeUnknownError,
    BattleCompleted,
    BattleInterruptedError,
    BattlePresence,
    BattleRecoveryExhaustedError,
    BattleStateReadinessError,
    BattleStepIdle,
    BattleStepIdleReason,
    BattleStepProgress,
    BattleStepProgressKind,
    BattleStepResult,
    BattleStopped,
    BattleTurnPhase,
    BattleTurnState,
    TurnDecision,
)
from .session import BattleSession
from .strategy import BattleStrategy

logger = setup_logger(__name__)

_MAX_STATE_READINESS_TIMEOUT_SECONDS = 5.0


def _not_paused() -> bool:
    return False


class BattleRunner:
    """Drive one battle through cooperative, receipt-bounded steps.

    :meth:`step` performs at most one confirmed state-changing operation before
    returning control. It never maintains or starts a battle. Runner state,
    including the one-shot same-browser recovery budget, survives across step
    calls.
    """

    def __init__(
        self,
        session: BattleSession,
        strategy: BattleStrategy,
        *,
        pause_requested: Callable[[], bool] = _not_paused,
        idle_delay: float = 2,
        state_readiness_timeout: float = _MAX_STATE_READINESS_TIMEOUT_SECONDS,
        state_readiness_retry_delay: float = 0.25,
        challenge_poll_interval: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(challenge_poll_interval, (int, float))
            or isinstance(challenge_poll_interval, bool)
            or not math.isfinite(challenge_poll_interval)
            or challenge_poll_interval <= 0
        ):
            raise ValueError("challenge_poll_interval must be finite and positive")
        if (
            not isinstance(idle_delay, int | float)
            or isinstance(idle_delay, bool)
            or not math.isfinite(idle_delay)
            or idle_delay < 0
        ):
            raise ValueError("idle_delay must be finite and non-negative")
        if (
            not isinstance(state_readiness_timeout, (int, float))
            or isinstance(state_readiness_timeout, bool)
            or not math.isfinite(state_readiness_timeout)
            or state_readiness_timeout <= 0
            or state_readiness_timeout > _MAX_STATE_READINESS_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "state_readiness_timeout must be finite and in (0, 5] seconds"
            )
        if (
            not isinstance(state_readiness_retry_delay, (int, float))
            or isinstance(state_readiness_retry_delay, bool)
            or not math.isfinite(state_readiness_retry_delay)
            or state_readiness_retry_delay <= 0
        ):
            raise ValueError("state_readiness_retry_delay must be finite and positive")
        if not callable(pause_requested):
            raise TypeError("pause_requested must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.session = session
        self.strategy = strategy
        self.pause_requested = pause_requested
        self.idle_delay = idle_delay
        self.state_readiness_timeout = float(state_readiness_timeout)
        self.state_readiness_retry_delay = float(state_readiness_retry_delay)
        self.challenge_poll_interval = challenge_poll_interval
        self._sleep = sleep
        self._clock = clock
        self._presence_initialized = False
        self._strategy_initialized = False
        self._strategy_initialization_error: BattleInterruptedError | None = None
        self._is_isekai: bool | None = None
        self._state_readiness_expires_at: float | None = None
        self._state_readiness_observations = 0
        self._recovery_pending_receipt = False
        self._completed: BattleCompleted | None = None
        self._transition_pending = False
        self._step_lock = asyncio.Lock()

    async def step(self) -> BattleStepResult:
        """Advance to the next safe cooperative yield boundary.

        Progress results mean that an operation returned with positive receipt
        or reconciliation evidence. ``BattleStepIdle`` means no mutation was
        made. Completion is cached, so a repeated call cannot acknowledge the
        final control twice.
        """
        async with self._step_lock:
            if self._completed is not None:
                return self._completed
            try:
                return await self._step_unlocked()
            except BattleInterruptedError:
                raise
            except Exception as error:
                if contains_log_persistence_error(error) or is_browser_generation_error(
                    error
                ):
                    raise
                raise BattleInterruptedError(
                    "Battle outcome is unknown after an active-session error",
                    diagnostic_code="battle.active-session-error",
                ) from error

    async def run_current(self) -> BattleCompleted | BattleStopped | None:
        """Run the current battle by consuming cooperative steps to a result."""
        while True:
            result = await self.step()
            if isinstance(result, BattleStepProgress):
                continue
            if isinstance(result, BattleStepIdle):
                await self._sleep(result.retry_after)
                continue
            if isinstance(result, BattleAbsent):
                return None
            return result

    def reset_for_next_battle(self) -> None:
        """Clear terminal state after the caller reaches a post-battle boundary.

        The caller must not reset an active, stopped, or uncertain battle.  A
        completion result is cached specifically to prevent duplicate final
        acknowledgement until campaign-level maintenance and realm restoration
        have finished.
        """

        if self._completed is None:
            raise RuntimeError(
                "battle runner can reset only after confirmed completion"
            )
        self._presence_initialized = False
        self._strategy_initialized = False
        self._strategy_initialization_error = None
        self._is_isekai = None
        self._clear_state_readiness(log_recovery=False)
        self._recovery_pending_receipt = False
        self._completed = None
        self._clear_transition_confirmation()

    async def _step_unlocked(self) -> BattleStepResult:
        initialized_result = await self._ensure_presence_initialized()
        if initialized_result is not None:
            return initialized_result
        assert self._is_isekai is not None

        while True:
            try:
                if self._state_readiness_expires_at is None:
                    pause_result = await self._service_ponychart_or_pause()
                    if pause_result is not None:
                        return pause_result
                    self._state_readiness_expires_at = (
                        self._read_clock() + self.state_readiness_timeout
                    )
                if self._transition_pending and self._state_readiness_remaining() <= 0:
                    raise self._missing_completion_evidence()
                try:
                    prepared = await self._probe_pending_state_readiness()
                except BattleStateReadinessError as error:
                    if self._transition_pending:
                        raise self._missing_completion_evidence() from error
                    raise
                if prepared.phase is BattleTurnPhase.NOT_READY:
                    return await self._defer_state_not_ready()
                if prepared.phase is not BattleTurnPhase.ABSENT:
                    self._clear_state_readiness()
                    self._clear_transition_confirmation()
                if await self.session.resolve_ponychart():
                    return self._confirmed_progress(
                        BattleStepProgressKind.PONYCHART_RESOLVED
                    )
                if prepared.phase is BattleTurnPhase.CHALLENGE:
                    # The challenge may have disappeared between the state
                    # probe and the second resolver call. Refresh before policy
                    # code is allowed to act.
                    continue
                if prepared.phase is BattleTurnPhase.COMPLETE:
                    pause_result = self._defer_if_paused()
                    if pause_result is not None:
                        return pause_result
                    return await self._acknowledge_completion()
                if prepared.phase is BattleTurnPhase.ABSENT:
                    if self.session.battle_completion_observed:
                        self._clear_state_readiness()
                        self._clear_transition_confirmation()
                        pause_result = self._defer_if_paused()
                        if pause_result is not None:
                            return pause_result
                        return await self._acknowledge_completion()
                    if not self._transition_pending:
                        self._transition_pending = True
                    remaining = self._state_readiness_remaining()
                    if remaining <= 0:
                        raise self._missing_completion_evidence()
                    return BattleStepIdle(
                        retry_after=min(self.idle_delay, remaining),
                        reason=BattleStepIdleReason.TRANSITION_CONFIRMATION,
                    )
                if prepared.phase is BattleTurnPhase.NEXT_FLOOR:
                    pause_result = self._defer_if_paused()
                    if pause_result is not None:
                        return pause_result
                    if not await self.session.go_next_floor():
                        raise TimeoutError(
                            "Next-floor control disappeared before progression"
                        )
                    self._recovery_pending_receipt = False
                    return self._confirmed_progress(
                        BattleStepProgressKind.NEXT_FLOOR_CONFIRMED
                    )
                if not prepared.strategy_actionable:
                    raise RuntimeError(
                        f"Unsupported battle turn phase: {prepared.phase!r}"
                    )

                pause_result = self._defer_if_paused()
                if pause_result is not None:
                    return pause_result

                await self._ensure_strategy_initialized()

                decision = await self.strategy.take_turn(self.session)
                if decision is TurnDecision.STOP:
                    return BattleStopped(
                        is_isekai=self._is_isekai,
                        decision_count=self._decision_count(),
                        current_round=self._local_counter("current_round"),
                        total_rounds=self._local_counter("total_rounds"),
                    )
                if decision is TurnDecision.IDLE:
                    return BattleStepIdle(retry_after=self.idle_delay)
                if decision is TurnDecision.ACTED:
                    # A normally returned ACTED decision has already waited
                    # for its authoritative receipt through BattleSession.
                    self._recovery_pending_receipt = False
                    return self._confirmed_progress(
                        BattleStepProgressKind.TURN_ACTION_CONFIRMED
                    )
                raise TypeError("BattleStrategy.take_turn() must return TurnDecision")
            except BattleActionOutcomeUnknownError as error:
                return await self._reconcile_unknown_action(error)
            except ZendriverOperationTimeout as error:
                logger.error(
                    "Battle browser operation timed out and remains in flight",
                    exc_info=True,
                )
                raise BattleInterruptedError(
                    "Battle browser operation remains in flight after timeout",
                    diagnostic_code="battle.browser-operation-timeout",
                ) from error
            except TimeoutError as error:
                logger.error(
                    "Battle turn timed out; no unclassified timeout is retried "
                    "error_type=%s",
                    type(error).__name__,
                )
                logger.debug("Battle turn timeout error detail", exc_info=True)
                raise BattleInterruptedError(
                    "Battle outcome is unknown after a turn timeout",
                    diagnostic_code="battle.turn-timeout",
                ) from error

    async def _ensure_presence_initialized(self) -> BattleStepResult | None:
        if self._presence_initialized:
            return None

        pause_result = await self._service_ponychart_or_pause()
        if pause_result is not None:
            return pause_result
        presence = await self.session.inspect_battle_presence()
        if presence is BattlePresence.ABSENT:
            return BattleAbsent()

        self._is_isekai = await self.session.is_isekai
        if presence is BattlePresence.COMPLETION:
            pause_result = await self._service_ponychart_or_pause()
            if pause_result is not None:
                return pause_result
            return await self._acknowledge_completion()
        if presence is not BattlePresence.ACTIVE:
            raise TypeError("BattleSession returned an unsupported battle presence")
        self.session.reset_battle_tracking()

        if await self.session.resolve_ponychart():
            return self._confirmed_progress(BattleStepProgressKind.PONYCHART_RESOLVED)

        self._presence_initialized = True
        self._clear_transition_confirmation()
        return None

    async def _ensure_strategy_initialized(self) -> None:
        if self._strategy_initialized:
            return
        if self._strategy_initialization_error is not None:
            raise self._strategy_initialization_error
        lifecycle_declared = inspect.getattr_static(
            self.strategy, "on_battle_started", None
        )
        if lifecycle_declared is not None:
            try:
                on_battle_started = getattr(self.strategy, "on_battle_started")
                lifecycle_result = on_battle_started(self.session)
                if not inspect.isawaitable(lifecycle_result):
                    raise TypeError("BattleLifecycle.on_battle_started() must be async")
                await lifecycle_result
            except BattleInterruptedError as error:
                self._strategy_initialization_error = error
                raise
            except Exception as error:
                if contains_log_persistence_error(error) or is_browser_generation_error(
                    error
                ):
                    raise
                interrupted = BattleInterruptedError(
                    "Battle strategy lifecycle failed before the first turn",
                    diagnostic_code="battle.strategy-lifecycle-failed",
                )
                self._strategy_initialization_error = interrupted
                raise interrupted from error
        self._strategy_initialized = True

    def _read_clock(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise RuntimeError("BattleRunner clock returned a non-finite value")
        return now

    def _state_readiness_error(self) -> BattleStateReadinessError:
        logger.error(
            "Battle turn state readiness deadline exhausted observations=%d "
            "diagnostic_skipped=deadline-exhausted",
            self._state_readiness_observations,
        )
        return BattleStateReadinessError(
            observation_count=self._state_readiness_observations,
            diagnostic_path=None,
            diagnostic_error_type=None,
        )

    def _state_readiness_remaining(self, *, now: float | None = None) -> float:
        expires_at = self._state_readiness_expires_at
        if expires_at is None:
            raise RuntimeError("Battle state readiness deadline is not active")
        observed_at = self._read_clock() if now is None else now
        return max(0.0, expires_at - observed_at)

    async def _probe_pending_state_readiness(self) -> BattleTurnState:
        """Run one bounded probe without starting work at or after expiry."""

        remaining = self._state_readiness_remaining()
        if remaining <= 0:
            raise self._state_readiness_error()
        try:
            prepared = await self.session.prepare_turn_state(timeout=remaining)
        except ZendriverOperationTimeout:
            raise
        except TimeoutError:
            if self._state_readiness_remaining() <= 0:
                self._state_readiness_observations += 1
                raise self._state_readiness_error() from None
            raise
        if self._state_readiness_remaining() <= 0:
            # A result that arrives at the exact boundary is late. In
            # particular, do not accept an ACTIVE result from an over-budget
            # final probe and allow policy to mutate the page.
            self._state_readiness_observations += 1
            raise self._state_readiness_error()
        return prepared

    async def _defer_state_not_ready(self) -> BattleStepIdle:
        now = self._read_clock()
        if self._state_readiness_expires_at is None:
            raise RuntimeError("Battle state readiness deadline was not started")
        if self._state_readiness_observations == 0:
            logger.info(
                "Battle document is present; waiting for turn state readiness",
                extra={"activity": "Battle"},
            )
        self._state_readiness_observations += 1
        remaining = self._state_readiness_remaining(now=now)
        if remaining > 0:
            logger.debug(
                "Battle turn state is not ready observation=%d remaining=%.1fs",
                self._state_readiness_observations,
                remaining,
            )
            return BattleStepIdle(
                retry_after=min(self.state_readiness_retry_delay, remaining),
                reason=BattleStepIdleReason.STATE_NOT_READY,
            )

        raise self._state_readiness_error()

    def _clear_state_readiness(self, *, log_recovery: bool = True) -> None:
        if log_recovery and self._state_readiness_observations:
            logger.info(
                "Battle document left the not-ready state after %d observations",
                self._state_readiness_observations,
                extra={"activity": "Battle"},
            )
        self._state_readiness_expires_at = None
        self._state_readiness_observations = 0

    async def _reconcile_unknown_action(
        self,
        error: BattleActionOutcomeUnknownError,
    ) -> BattleStepProgress:
        recovered = False
        evidence = error.recovery_evidence
        recovery_eligible = bool(
            evidence is not None and evidence.allows_same_browser_recovery
        )
        if (
            recovery_eligible
            and not self._recovery_pending_receipt
            and self._is_isekai is not None
        ):
            recover = getattr(self.session, "recover_unknown_action", None)
            if recover is not None:
                try:
                    recovered = bool(
                        await recover(
                            error,
                            expected_is_isekai=self._is_isekai,
                        )
                    )
                except Exception as recovery_error:
                    if is_browser_generation_error(recovery_error):
                        raise
                    logger.debug(
                        "Battle action reload recovery failed error_type=%s",
                        type(recovery_error).__name__,
                        exc_info=True,
                    )
        if recovered:
            self._recovery_pending_receipt = True
            logger.warning(
                "Battle action outcome was unknown after a verified receipt-loss "
                "incident; continuing from reloaded server state"
            )
            return self._confirmed_progress(BattleStepProgressKind.RECOVERY_RECONCILED)

        logger.error(
            "Battle action completion could not be confirmed: error_type=%s",
            type(error).__name__,
        )
        interruption_type = (
            BattleRecoveryExhaustedError
            if recovery_eligible or self._recovery_pending_receipt
            else BattleInterruptedError
        )
        raise interruption_type(
            "Battle outcome is unknown because the submitted action did not "
            "produce completion evidence",
            diagnostic_code=(
                "battle.action-recovery-exhausted"
                if recovery_eligible or self._recovery_pending_receipt
                else "battle.action-outcome-unknown"
            ),
        ) from error

    async def _acknowledge_completion(self) -> BattleCompleted:
        assert self._is_isekai is not None
        result = BattleCompleted(
            is_isekai=self._is_isekai,
            decision_count=self._decision_count(),
            final_round=self._local_counter("current_round"),
            total_rounds=self._local_counter("total_rounds"),
        )
        try:
            await self.session.acknowledge_battle_completion(
                expected_is_isekai=result.is_isekai
            )
        except BattleActionOutcomeUnknownError as error:
            logger.error(
                "Final battle completion acknowledgement could not be confirmed: "
                "error_type=%s",
                type(error).__name__,
            )
            raise BattleInterruptedError(
                "Battle outcome is unknown because the final completion "
                "acknowledgement did not produce positive exit evidence",
                diagnostic_code="battle.completion-acknowledgement-unknown",
            ) from error

        self._completed = result
        realm = "Isekai" if result.is_isekai else "Persistent"
        log_extra = {
            "activity": "Battle",
            "realm": realm,
            "tab_role": realm.casefold(),
        }
        if result.final_round > 0 and result.total_rounds > 0:
            logger.info(
                "Completed after %d decisions (round %d/%d)",
                result.decision_count,
                result.final_round,
                result.total_rounds,
                extra=log_extra,
            )
        else:
            logger.info(
                "Completed after %d decisions; round data was unavailable",
                result.decision_count,
                extra=log_extra,
            )
            logger.debug(
                "The positive completion control appeared before round metadata "
                "became available",
                extra=log_extra,
            )
        return result

    def _confirmed_progress(
        self,
        kind: BattleStepProgressKind,
    ) -> BattleStepProgress:
        self._clear_state_readiness()
        return BattleStepProgress(
            kind=kind,
            is_isekai=self._is_isekai,
            decision_count=self._decision_count(),
            current_round=self._local_counter("current_round"),
            total_rounds=self._local_counter("total_rounds"),
        )

    def _local_counter(self, name: str) -> int:
        """Read already-parsed metadata without risking a post-receipt probe."""
        try:
            value = getattr(self.session, name)
        except Exception:
            return 0
        return max(value, 0) if type(value) is int else 0

    def _decision_count(self) -> int:
        try:
            turn = self.session.turn
        except Exception:
            return 0
        return max(turn + 1, 0) if type(turn) is int else 0

    def _clear_transition_confirmation(self) -> None:
        self._transition_pending = False

    @staticmethod
    def _missing_completion_evidence() -> BattleInterruptedError:
        return BattleInterruptedError(
            "Battle page disappeared without positive completion evidence",
            diagnostic_code="battle.completion-evidence-missing",
        )

    async def _service_ponychart_or_pause(
        self,
    ) -> BattleStepProgress | BattleStepIdle | None:
        """Resolve a timed challenge before cooperatively deferring for Pause."""

        if await self.session.resolve_ponychart():
            return self._confirmed_progress(BattleStepProgressKind.PONYCHART_RESOLVED)
        return self._defer_if_paused()

    def _defer_if_paused(self) -> BattleStepIdle | None:
        """Read the local pause gate without issuing another browser probe."""

        paused = self.pause_requested()
        if not isinstance(paused, bool):
            raise TypeError("pause_requested() must return bool")
        if not paused:
            return None
        return BattleStepIdle(
            retry_after=self.challenge_poll_interval,
            reason=BattleStepIdleReason.PAUSED,
        )

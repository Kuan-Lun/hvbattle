"""Policy-neutral cooperative runner for exactly one already-active battle."""

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable

from hvbrowser.runtime import setup_logger

from ._zendriver import ZendriverOperationTimeout
from .contracts import (
    BattleAbsent,
    BattleActionOutcomeUnknownError,
    BattleCompleted,
    BattleInterruptedError,
    BattlePresence,
    BattleRecoveryExhaustedError,
    BattleStepIdle,
    BattleStepIdleReason,
    BattleStepProgress,
    BattleStepProgressKind,
    BattleStepResult,
    BattleStopped,
    BattleTurnPhase,
    TurnDecision,
)
from .session import BattleSession
from .strategy import BattleStrategy

logger = setup_logger(__name__)


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
        timeout_retries: int = 3,
        idle_delay: float = 2,
        retry_delay: float = 5,
        transition_checks: int = 3,
        challenge_poll_interval: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if timeout_retries < 1:
            raise ValueError("timeout_retries must be at least 1")
        if transition_checks < 1:
            raise ValueError("transition_checks must be at least 1")
        if (
            not isinstance(challenge_poll_interval, (int, float))
            or isinstance(challenge_poll_interval, bool)
            or not math.isfinite(challenge_poll_interval)
            or challenge_poll_interval <= 0
        ):
            raise ValueError("challenge_poll_interval must be finite and positive")
        if idle_delay < 0:
            raise ValueError("idle_delay must not be negative")
        if not callable(pause_requested):
            raise TypeError("pause_requested must be callable")
        self.session = session
        self.strategy = strategy
        self.pause_requested = pause_requested
        self.timeout_retries = timeout_retries
        self.idle_delay = idle_delay
        self.retry_delay = retry_delay
        self.transition_checks = transition_checks
        self.challenge_poll_interval = challenge_poll_interval
        self._sleep = sleep
        self._initialized = False
        self._is_isekai: bool | None = None
        self._retry_count = 0
        self._recovery_pending_receipt = False
        self._completed: BattleCompleted | None = None
        self._transition_pending = False
        self._transition_checks_completed = 0
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
                raise BattleInterruptedError(
                    "Battle outcome is unknown after an active-session error"
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
        self._initialized = False
        self._is_isekai = None
        self._retry_count = 0
        self._recovery_pending_receipt = False
        self._completed = None
        self._clear_transition_confirmation()

    async def _step_unlocked(self) -> BattleStepResult:
        initialized_result = await self._ensure_initialized()
        if initialized_result is not None:
            return initialized_result
        assert self._is_isekai is not None

        while True:
            try:
                pause_result = await self._service_ponychart_or_pause()
                if pause_result is not None:
                    return pause_result
                if await self.session.resolve_ponychart():
                    return self._confirmed_progress(
                        BattleStepProgressKind.PONYCHART_RESOLVED
                    )

                prepared = await self.session.prepare_turn_state()
                if prepared.phase is not BattleTurnPhase.ABSENT:
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
                    pause_result = await self._service_ponychart_or_pause()
                    if pause_result is not None:
                        return pause_result
                    return await self._acknowledge_completion()
                if prepared.phase is BattleTurnPhase.ABSENT:
                    if self.session.battle_completion_observed:
                        self._clear_transition_confirmation()
                        pause_result = await self._service_ponychart_or_pause()
                        if pause_result is not None:
                            return pause_result
                        return await self._acknowledge_completion()
                    if not self._transition_pending:
                        self._transition_pending = True
                        self._transition_checks_completed = 0
                        return BattleStepIdle(
                            retry_after=self.idle_delay,
                            reason=BattleStepIdleReason.TRANSITION_CONFIRMATION,
                        )
                    if await self.session.is_in_battle():
                        self._clear_transition_confirmation()
                        continue
                    if self.session.battle_completion_observed:
                        self._clear_transition_confirmation()
                        pause_result = await self._service_ponychart_or_pause()
                        if pause_result is not None:
                            return pause_result
                        return await self._acknowledge_completion()
                    self._transition_checks_completed += 1
                    if self._transition_checks_completed >= self.transition_checks:
                        raise BattleInterruptedError(
                            "Battle page disappeared without positive completion "
                            "evidence"
                        )
                    return BattleStepIdle(
                        retry_after=self.idle_delay,
                        reason=BattleStepIdleReason.TRANSITION_CONFIRMATION,
                    )
                if prepared.phase is BattleTurnPhase.NEXT_FLOOR:
                    pause_result = await self._service_ponychart_or_pause()
                    if pause_result is not None:
                        return pause_result
                    if await self.session.resolve_ponychart():
                        return self._confirmed_progress(
                            BattleStepProgressKind.PONYCHART_RESOLVED
                        )
                    if not await self.session.go_next_floor():
                        raise TimeoutError(
                            "Next-floor control disappeared before progression"
                        )
                    self._recovery_pending_receipt = False
                    self._retry_count = 0
                    return self._confirmed_progress(
                        BattleStepProgressKind.NEXT_FLOOR_CONFIRMED
                    )
                if not prepared.strategy_actionable:
                    raise RuntimeError(
                        f"Unsupported battle turn phase: {prepared.phase!r}"
                    )

                pause_result = await self._service_ponychart_or_pause()
                if pause_result is not None:
                    return pause_result
                if await self.session.resolve_ponychart():
                    return self._confirmed_progress(
                        BattleStepProgressKind.PONYCHART_RESOLVED
                    )

                decision = await self.strategy.take_turn(self.session)
                if decision is TurnDecision.STOP:
                    self._retry_count = 0
                    return BattleStopped(
                        is_isekai=self._is_isekai,
                        decision_count=self._decision_count(),
                        current_round=self._local_counter("current_round"),
                        total_rounds=self._local_counter("total_rounds"),
                    )
                if decision is TurnDecision.IDLE:
                    self._retry_count = 0
                    return BattleStepIdle(retry_after=self.idle_delay)
                if decision is TurnDecision.ACTED:
                    # A normally returned ACTED decision has already waited
                    # for its authoritative receipt through BattleSession.
                    self._recovery_pending_receipt = False
                    self._retry_count = 0
                    return self._confirmed_progress(
                        BattleStepProgressKind.TURN_ACTION_CONFIRMED
                    )
                raise TypeError("BattleStrategy.take_turn() must return TurnDecision")
            except BattleActionOutcomeUnknownError as error:
                return await self._reconcile_unknown_action(error)
            except ZendriverOperationTimeout as error:
                logger.error("Battle browser operation timed out and remains in flight")
                raise BattleInterruptedError(
                    "Battle browser operation remains in flight after timeout"
                ) from error
            except TimeoutError as error:
                self._retry_count += 1
                if self._retry_count >= self.timeout_retries:
                    logger.error(
                        "Battle turn timed out; retry limit reached (%d/%d) "
                        "error_type=%s",
                        self._retry_count,
                        self.timeout_retries,
                        type(error).__name__,
                    )
                    raise BattleInterruptedError(
                        "Battle outcome is unknown after a turn timeout"
                    ) from error
                logger.warning(
                    "Battle turn timed out; retrying (%d/%d) error_type=%s",
                    self._retry_count,
                    self.timeout_retries,
                    type(error).__name__,
                )
                logger.debug("Battle turn timeout error detail", exc_info=True)
                return BattleStepIdle(
                    retry_after=self.retry_delay,
                    reason=BattleStepIdleReason.RETRYABLE_TIMEOUT,
                )

    async def _ensure_initialized(self) -> BattleStepResult | None:
        if self._initialized:
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

        lifecycle_declared = inspect.getattr_static(
            self.strategy, "on_battle_started", None
        )
        if lifecycle_declared is not None:
            on_battle_started = getattr(self.strategy, "on_battle_started")
            lifecycle_result = on_battle_started(self.session)
            if not inspect.isawaitable(lifecycle_result):
                raise TypeError("BattleLifecycle.on_battle_started() must be async")
            await lifecycle_result

        self._initialized = True
        self._clear_transition_confirmation()
        return None

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
                    logger.debug(
                        "Battle action reload recovery failed error_type=%s",
                        type(recovery_error).__name__,
                    )
        if recovered:
            self._recovery_pending_receipt = True
            self._retry_count = 0
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
            "produce completion evidence"
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
                "acknowledgement did not produce positive exit evidence"
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
        self._retry_count = 0
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
        self._transition_checks_completed = 0

    async def _service_ponychart_or_pause(
        self,
    ) -> BattleStepProgress | BattleStepIdle | None:
        """Resolve a timed challenge before cooperatively deferring for Pause."""

        if await self.session.resolve_ponychart():
            return self._confirmed_progress(BattleStepProgressKind.PONYCHART_RESOLVED)
        paused = self.pause_requested()
        if not isinstance(paused, bool):
            raise TypeError("pause_requested() must return bool")
        if not paused:
            return None
        return BattleStepIdle(
            retry_after=self.challenge_poll_interval,
            reason=BattleStepIdleReason.PAUSED,
        )

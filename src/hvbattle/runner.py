"""Policy-neutral runner for exactly one already-active battle."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable

from hvbrowser.runtime import setup_logger

from .contracts import (
    BattleActionOutcomeUnknownError,
    BattleCompleted,
    BattleInterruptedError,
    BattleStopped,
    BattleTurnPhase,
    TurnDecision,
)
from .session import BattleSession
from .strategy import BattleStrategy

logger = setup_logger(__name__)


async def _not_paused() -> None:
    return


class BattleRunner:
    """Drive one active battle while delegating every turn decision."""

    def __init__(
        self,
        session: BattleSession,
        strategy: BattleStrategy,
        *,
        wait_if_paused: Callable[[], Awaitable[None]] = _not_paused,
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
        if challenge_poll_interval <= 0:
            raise ValueError("challenge_poll_interval must be positive")
        self.session = session
        self.strategy = strategy
        self.wait_if_paused = wait_if_paused
        self.timeout_retries = timeout_retries
        self.idle_delay = idle_delay
        self.retry_delay = retry_delay
        self.transition_checks = transition_checks
        self.challenge_poll_interval = challenge_poll_interval
        self._sleep = sleep

    async def run_current(self) -> BattleCompleted | BattleStopped | None:
        """Run only the current battle; never maintain or start another one."""
        try:
            await self.session.resolve_ponychart()
            await self._wait_while_servicing_ponychart()
            if not await self.session.is_in_battle():
                return None

            is_isekai = await self.session.is_isekai
            self.session.reset_battle_tracking()
            retry_count = 0

            await self.session.resolve_ponychart()
            lifecycle_declared = inspect.getattr_static(
                self.strategy, "on_battle_started", None
            )
            if lifecycle_declared is not None:
                on_battle_started = getattr(self.strategy, "on_battle_started")
                lifecycle_result = on_battle_started(self.session)
                if not inspect.isawaitable(lifecycle_result):
                    raise TypeError("BattleLifecycle.on_battle_started() must be async")
                await lifecycle_result

            while True:
                try:
                    if await self.session.resolve_ponychart():
                        retry_count = 0
                        continue

                    await self._wait_while_servicing_ponychart()
                    if await self.session.resolve_ponychart():
                        retry_count = 0
                        continue

                    prepared = await self.session.prepare_turn_state()
                    if await self.session.resolve_ponychart():
                        retry_count = 0
                        continue
                    if prepared.phase is BattleTurnPhase.CHALLENGE:
                        # The challenge may have disappeared between the state
                        # probe and the second resolver call.  Refresh the page
                        # before allowing strategy code to act.
                        continue
                    if prepared.phase is BattleTurnPhase.COMPLETE:
                        break
                    if prepared.phase is BattleTurnPhase.ABSENT:
                        if await self._confirm_completion_or_transition():
                            break
                        continue
                    if prepared.phase is BattleTurnPhase.NEXT_FLOOR:
                        await self._wait_while_servicing_ponychart()
                        if await self.session.resolve_ponychart():
                            retry_count = 0
                            continue
                        if not await self.session.go_next_floor():
                            raise TimeoutError(
                                "Next-floor control disappeared before progression"
                            )
                        retry_count = 0
                        continue
                    if not prepared.strategy_actionable:
                        raise RuntimeError(
                            f"Unsupported battle turn phase: {prepared.phase!r}"
                        )

                    await self._wait_while_servicing_ponychart()
                    if await self.session.resolve_ponychart():
                        retry_count = 0
                        continue

                    decision = await self.strategy.take_turn(self.session)
                    if decision is TurnDecision.STOP:
                        return BattleStopped(
                            is_isekai=is_isekai,
                            decision_count=self.session.turn + 1,
                            current_round=self.session.current_round,
                            total_rounds=self.session.total_rounds,
                        )
                    if decision is TurnDecision.IDLE:
                        await self._sleep(self.idle_delay)
                    elif decision is not TurnDecision.ACTED:
                        raise TypeError(
                            "BattleStrategy.take_turn() must return TurnDecision"
                        )
                    retry_count = 0
                except BattleActionOutcomeUnknownError as error:
                    logger.error(
                        "Battle action completion could not be confirmed: %r", error
                    )
                    raise BattleInterruptedError(
                        "Battle outcome is unknown because the submitted action "
                        "did not produce completion evidence"
                    ) from error
                except TimeoutError as error:
                    retry_count += 1
                    if retry_count >= self.timeout_retries:
                        logger.error(
                            "Battle turn timed out; retry limit reached (%d/%d): %r",
                            retry_count,
                            self.timeout_retries,
                            error,
                        )
                        raise BattleInterruptedError(
                            "Battle outcome is unknown after a turn timeout"
                        ) from error
                    logger.warning(
                        "Battle turn timed out: %r; retrying (%d/%d)",
                        error,
                        retry_count,
                        self.timeout_retries,
                    )
                    await self._sleep(self.retry_delay)

            result = BattleCompleted(
                is_isekai=is_isekai,
                decision_count=self.session.turn + 1,
                final_round=self.session.current_round,
                total_rounds=self.session.total_rounds,
            )
            if result.final_round > 0 and result.total_rounds > 0:
                logger.info("Battle complete: %s", result)
            else:
                logger.info(
                    "Battle complete: is_isekai=%s decision_count=%d "
                    "round=<unknown>; the positive completion control appeared "
                    "before round metadata became available",
                    result.is_isekai,
                    result.decision_count,
                )
            return result
        except BattleInterruptedError:
            raise
        except Exception as error:
            raise BattleInterruptedError(
                "Battle outcome is unknown after an active-session error"
            ) from error

    async def _confirm_completion_or_transition(self) -> bool:
        """Return completion, resume a transition, or fail on unknown exit."""
        if self.session.battle_completion_observed:
            return True
        for _ in range(self.transition_checks):
            await self._sleep(self.idle_delay)
            if await self.session.resolve_ponychart():
                return False
            if await self.session.is_in_battle():
                return False
            if self.session.battle_completion_observed:
                return True
        raise BattleInterruptedError(
            "Battle page disappeared without positive completion evidence"
        )

    async def _wait_while_servicing_ponychart(self) -> None:
        """Honor pause without allowing a timed challenge to expire."""
        pause_task: asyncio.Future[None] = asyncio.ensure_future(self.wait_if_paused())
        try:
            while not pause_task.done():
                done, _ = await asyncio.wait(
                    {pause_task}, timeout=self.challenge_poll_interval
                )
                if pause_task not in done:
                    await self.session.resolve_ponychart()
            await pause_task
        finally:
            if not pause_task.done():
                pause_task.cancel()
                await asyncio.gather(pause_task, return_exceptions=True)

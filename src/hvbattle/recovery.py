"""Composable recovery boundary for one ambiguous submitted battle action."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from hvbrowser.runtime import setup_logger

from .contracts import BattleActionRecoveryEvidence, BattleTurnPhase

logger = setup_logger(__name__)


class ActionDialogTracker:
    """Bind one sanitized JavaScript-dialog category to one action token."""

    def __init__(self) -> None:
        self._active_action_id: str | None = None
        self._observation: tuple[str, str] | None = None

    def begin(self, action_id: str) -> None:
        if not action_id:
            raise ValueError("action_id must be non-empty")
        self._active_action_id = action_id
        self._observation = None

    def record(self, category: str) -> None:
        if self._active_action_id is not None:
            self._observation = (self._active_action_id, category)

    def category_for(self, action_id: str) -> str | None:
        if self._observation is None or self._observation[0] != action_id:
            return None
        return self._observation[1]

    def clear(self) -> None:
        self._active_action_id = None
        self._observation = None


@dataclass(frozen=True, slots=True)
class BattleRecoveryState:
    """One internally consistent, non-mutating view of a battle document."""

    document_id: str
    realm: str
    ready_state: str
    phase: BattleTurnPhase | None
    log_revision: str | None
    completion_revision: str | None
    action_controls: int

    def signature(self) -> tuple[object, ...]:
        """Fields that must remain unchanged through final reconciliation."""
        return (
            self.document_id,
            self.realm,
            self.ready_state,
            self.phase,
            self.log_revision,
            self.completion_revision,
            self.action_controls,
        )


class _RecoveryActions(Protocol):
    async def read_recovery_state(
        self, *, probe_timeout: float
    ) -> BattleRecoveryState | None: ...

    async def reload_current_page(self, *, probe_timeout: float) -> None: ...

    async def clear_page_action_state(self, *, probe_timeout: float) -> str | None: ...


class _RecoveryStateStore(Protocol):
    async def inspect(self) -> Any: ...

    def reset(self) -> None: ...


class _RecoveryProbeKind(StrEnum):
    STATE = "state"
    RECONCILIATION_MISMATCH = "reconciliation-mismatch"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class _RecoveryProbe:
    kind: _RecoveryProbeKind
    state: BattleRecoveryState | None = None


class _DocumentObservationKind(StrEnum):
    CHANGED = "changed"
    CONFIRMED_UNCHANGED = "confirmed-unchanged"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class _DocumentObservation:
    kind: _DocumentObservationKind
    state: BattleRecoveryState | None = None


class BattleRecoveryCoordinator:
    """Reconcile a reload without deciding battle policy or replaying an action."""

    def __init__(
        self,
        actions: _RecoveryActions,
        state_store: _RecoveryStateStore,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._actions = actions
        self._state_store = state_store
        self._sleep = sleep

    @staticmethod
    def _eligible_state(
        state: BattleRecoveryState,
        *,
        pre_click_document_id: str,
        expected_realm: str,
    ) -> bool:
        return bool(
            state.document_id != pre_click_document_id
            and state.realm == expected_realm
            and state.ready_state in {"interactive", "complete"}
            and state.phase
            in {
                BattleTurnPhase.ACTIVE,
                BattleTurnPhase.NEXT_FLOOR,
                BattleTurnPhase.COMPLETE,
                BattleTurnPhase.CHALLENGE,
            }
        )

    async def _read_probe(self, *, probe_timeout: float) -> _RecoveryProbe:
        try:
            state = await self._actions.read_recovery_state(probe_timeout=probe_timeout)
        except Exception as error:
            logger.debug(
                "Battle reload recovery probe failed error_type=%s",
                type(error).__name__,
            )
            return _RecoveryProbe(_RecoveryProbeKind.ERROR)
        if state is None:
            return _RecoveryProbe(_RecoveryProbeKind.RECONCILIATION_MISMATCH)
        return _RecoveryProbe(_RecoveryProbeKind.STATE, state)

    async def _observe_document_change(
        self,
        pre_click_document_id: str,
        *,
        checks: int,
        check_interval: float,
        probe_timeout: float,
    ) -> _DocumentObservation:
        confirmed_unchanged = 0
        for check in range(checks):
            probe = await self._read_probe(probe_timeout=probe_timeout)
            if probe.kind is _RecoveryProbeKind.ERROR:
                return _DocumentObservation(_DocumentObservationKind.INDETERMINATE)
            if probe.kind is _RecoveryProbeKind.RECONCILIATION_MISMATCH:
                if check + 1 < checks:
                    await self._sleep(check_interval)
                continue
            if probe.state is None:
                raise RuntimeError("state probe kind requires a recovery state")
            if probe.state.document_id != pre_click_document_id:
                return _DocumentObservation(
                    _DocumentObservationKind.CHANGED,
                    probe.state,
                )
            confirmed_unchanged += 1
            if check + 1 < checks:
                await self._sleep(check_interval)
        if confirmed_unchanged == checks:
            return _DocumentObservation(_DocumentObservationKind.CONFIRMED_UNCHANGED)
        return _DocumentObservation(_DocumentObservationKind.INDETERMINATE)

    async def _wait_for_stable_state(
        self,
        pre_click_document_id: str,
        expected_realm: str,
        *,
        initial_state: BattleRecoveryState | None,
        checks: int,
        stable_checks: int,
        check_interval: float,
        probe_timeout: float,
    ) -> BattleRecoveryState | None:
        last_signature: tuple[object, ...] | None = None
        matching_reads = 0
        state = initial_state
        for check in range(checks):
            if state is None:
                probe = await self._read_probe(probe_timeout=probe_timeout)
                if probe.kind is _RecoveryProbeKind.ERROR:
                    return None
                if probe.kind is _RecoveryProbeKind.RECONCILIATION_MISMATCH:
                    last_signature = None
                    matching_reads = 0
                    if check + 1 < checks:
                        await self._sleep(check_interval)
                    continue
                state = probe.state
                if state is None:
                    raise RuntimeError("state probe kind requires a recovery state")
            if state is not None and self._eligible_state(
                state,
                pre_click_document_id=pre_click_document_id,
                expected_realm=expected_realm,
            ):
                signature = state.signature()
                if signature == last_signature:
                    matching_reads += 1
                else:
                    last_signature = signature
                    matching_reads = 1
                if matching_reads >= stable_checks:
                    if state.phase is not BattleTurnPhase.ACTIVE:
                        return state
                    try:
                        snapshot = await self._state_store.inspect()
                    except TimeoutError:
                        logger.error(
                            "Reloaded active battle parse timed out; refusing "
                            "another parse while the browser command may remain live"
                        )
                        return None
                    except Exception as error:
                        logger.debug(
                            "Reloaded active battle parse probe failed "
                            "error_type=%s",
                            type(error).__name__,
                        )
                    else:
                        if any(monster.alive for monster in snapshot.monsters.values()):
                            return state
            else:
                last_signature = None
                matching_reads = 0
            state = None
            if check + 1 < checks:
                await self._sleep(check_interval)
        return None

    async def recover(
        self,
        evidence: BattleActionRecoveryEvidence,
        *,
        expected_realm: str,
        auto_reload_checks: int = 4,
        recovery_checks: int = 12,
        stable_checks: int = 2,
        check_interval: float = 0.25,
        probe_timeout: float = 2.0,
    ) -> bool:
        """Accept only a verified new state; never replay the submitted action."""
        if expected_realm not in {"persistent", "isekai"}:
            raise ValueError("expected_realm must be persistent or isekai")
        if auto_reload_checks < 1 or recovery_checks < 1:
            raise ValueError("battle recovery check counts must be positive")
        if stable_checks < 2:
            raise ValueError("stable_checks must be at least 2")
        if check_interval <= 0 or probe_timeout <= 0:
            raise ValueError("battle recovery timeouts must be positive")
        if not evidence.matches_server_communication_failure:
            return False

        pre_click_document_id = evidence.pre_click_document_id
        observation = await self._observe_document_change(
            pre_click_document_id,
            checks=auto_reload_checks,
            check_interval=check_interval,
            probe_timeout=probe_timeout,
        )
        if observation.kind is _DocumentObservationKind.INDETERMINATE:
            logger.error(
                "Battle reload observation was indeterminate; refusing an "
                "additional current-page reload"
            )
            return False

        initial_state = observation.state
        if observation.kind is _DocumentObservationKind.CONFIRMED_UNCHANGED:
            logger.warning(
                "Server communication failure left the old battle document "
                "stable; reloading the current page once"
            )
            try:
                await self._actions.reload_current_page(probe_timeout=probe_timeout)
            except Exception as reload_error:
                logger.error(
                    "Current-page battle reload failed error_type=%s",
                    type(reload_error).__name__,
                )
                return False

        stable = await self._wait_for_stable_state(
            pre_click_document_id,
            expected_realm,
            initial_state=initial_state,
            checks=recovery_checks,
            stable_checks=stable_checks,
            check_interval=check_interval,
            probe_timeout=probe_timeout,
        )
        if stable is None:
            logger.error("Reloaded battle did not establish a stable same-realm state")
            return False

        try:
            cleanup_document_id = await self._actions.clear_page_action_state(
                probe_timeout=probe_timeout
            )
        except Exception as cleanup_error:
            logger.error(
                "Reloaded battle action state cleanup failed error_type=%s",
                type(cleanup_error).__name__,
            )
            return False
        if cleanup_document_id != stable.document_id:
            logger.error("Battle document changed during recovery cleanup")
            return False

        final_probe = await self._read_probe(probe_timeout=probe_timeout)
        if (
            final_probe.kind is not _RecoveryProbeKind.STATE
            or final_probe.state is None
            or final_probe.state.signature() != stable.signature()
        ):
            logger.error("Battle recovery state changed after final verification")
            return False

        if stable.phase is not BattleTurnPhase.COMPLETE:
            self._state_store.reset()
        logger.warning(
            "Battle action recovery accepted a verified document phase=%s realm=%s",
            stable.phase,
            stable.realm,
        )
        return True

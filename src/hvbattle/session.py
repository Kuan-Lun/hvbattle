"""State inspection and atomic actions for one HentaiVerse battle session."""

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Self

from hv_bie.types import BattleSnapshot
from hvbrowser import HentaiVerseSession, Realm
from hvbrowser.runtime import (
    is_browser_generation_error,
    setup_logger,
    wait_for_zendriver,
)
from zendriver import cdp

from .battle_launcher import BattleLauncher
from .battle_state import BattleStateStore
from .contracts import (
    ArenaOption,
    BattleActionOutcomeUnknownError,
    BattleInterruptedError,
    BattlePresence,
    BattleTurnPhase,
    BattleTurnState,
    GrindfestOption,
    RingOfBloodOption,
    RingOfBloodSnapshot,
    RingOfBloodStartOutcome,
)
from .hv_battle_action_manager import ElementActionManager
from .hv_battle_buff_manager import BuffManager
from .hv_battle_item_provider import ItemProvider
from .hv_battle_ponychart import PonyChart, preload_ponychart_classifier
from .hv_battle_skill_manager import SkillManager
from .recovery import ActionDialogTracker, BattleRecoveryCoordinator

logger = setup_logger(__name__)

_BATTLE_PHASE_ACTIVE = "active"
_BATTLE_PHASE_COMPLETE = "complete"
_BATTLE_PHASE_NEXT_FLOOR = "next-floor"
_FINAL_COMPLETION_SELECTOR = '#pane_completion img[src*="finishbattle.png"]'
_DIALOG_MUTATION_TIMEOUT_SECONDS = 15.0
_BATTLE_PHASE_JS = r"""
(() => {
    const pane = document.getElementById("pane_completion");
    if (
        pane
        && pane.querySelector('img[src*="finishbattle.png"]')
    ) {
        return "complete";
    }
    if (document.getElementById("btcp")) return "next-floor";
    return "active";
})()
"""


class BattleSession:
    """Battle facade backed by one explicitly composed HentaiVerse session."""

    def __init__(
        self,
        *args: Any,
        auto_accept_dialogs: bool = False,
        hentaiverse: HentaiVerseSession | None = None,
        ponychart_image_directory: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        if hentaiverse is not None and (args or kwargs):
            raise TypeError(
                "Browser options cannot be combined with an injected "
                "HentaiVerseSession"
            )
        self.hentaiverse = (
            hentaiverse
            if hentaiverse is not None
            else HentaiVerseSession(*args, **kwargs)
        )
        self.auto_accept_dialogs = auto_accept_dialogs
        image_directory = (
            Path(ponychart_image_directory)
            if ponychart_image_directory is not None
            else None
        )
        self._ponychart = PonyChart(
            self.hentaiverse.browser,
            image_directory=image_directory,
        )
        self._launcher = BattleLauncher(
            self.hentaiverse.browser,
            self.hentaiverse.realm,
        )
        self.battle_state: BattleStateStore | None = None
        self.element_action_manager: ElementActionManager | None = None
        self._item_provider: ItemProvider | None = None
        self._skill_manager: SkillManager | None = None
        self._buff_manager: BuffManager | None = None
        self._completion_observed = False
        self.action_dialog_tracker = ActionDialogTracker()
        self._last_parser_warning_signature: tuple[int, tuple[str, ...]] | None = None
        self._last_reported_round_progress: tuple[int, int] | None = None
        self._classifier_prepared = False
        self._browser_hooks_initialized = False
        self.turn = -1
        self.round = -1
        self._initialize_battle_components()

    @property
    def page(self) -> Any:
        """Expose the page owned by the composed HentaiVerse session."""
        hentaiverse = self.__dict__.get("hentaiverse")
        if hentaiverse is not None:
            return hentaiverse.browser.page
        return self.__dict__["_page_override"]

    @page.setter
    def page(self, value: Any) -> None:
        """Keep lightweight, uninitialized test doubles usable."""
        hentaiverse = self.__dict__.get("hentaiverse")
        if hentaiverse is not None:
            hentaiverse.browser.page = value
        else:
            self.__dict__["_page_override"] = value

    @property
    def headless(self) -> bool:
        return bool(self.hentaiverse.browser.headless)

    @property
    async def is_isekai(self) -> bool:
        """Expose the realm needed by battle-run completion results."""
        return await self.hentaiverse.realm.current() is Realm.ISEKAI

    async def __aenter__(self) -> Self:
        async def capture(operation: Awaitable[Any]) -> BaseException | None:
            try:
                await operation
            except BaseException as error:
                return error
            return None

        async def startup() -> tuple[BaseException | None, BaseException | None]:
            browser_error, classifier_error = await asyncio.gather(
                capture(
                    self.hentaiverse.start(
                        on_browser_ready=self._on_browser_ready,
                    )
                ),
                capture(self._ensure_classifier()),
            )
            return browser_error, classifier_error

        startup_task = asyncio.create_task(startup())
        startup_errors, delayed_cancellation = await self._wait_for_startup_task(
            startup_task
        )
        browser_error, classifier_error = startup_errors
        primary_error = browser_error or classifier_error
        if primary_error is None and delayed_cancellation is None:
            return self

        cleanup_cause = delayed_cancellation or primary_error
        assert cleanup_cause is not None
        cleanup_task = asyncio.create_task(
            self.hentaiverse.__aexit__(
                type(cleanup_cause),
                cleanup_cause,
                cleanup_cause.__traceback__,
            )
        )
        cleanup_error, cleanup_cancellation = await self._wait_for_cleanup_task(
            cleanup_task
        )
        cancellation = delayed_cancellation or cleanup_cancellation
        secondary_error = classifier_error if browser_error is not None else None
        if cancellation is not None:
            if primary_error is not None:
                cancellation.add_note(
                    "battle session startup also failed: "
                    f"{type(primary_error).__name__}"
                )
            if secondary_error is not None:
                cancellation.add_note(
                    "parallel battle session startup also failed: "
                    f"{type(secondary_error).__name__}"
                )
            if cleanup_error is not None:
                cancellation.add_note(
                    "battle session startup cleanup also failed: "
                    f"{type(cleanup_error).__name__}"
                )
            raise cancellation from None

        assert primary_error is not None
        if secondary_error is not None:
            primary_error.add_note(
                "parallel battle session startup also failed: "
                f"{type(secondary_error).__name__}"
            )
        if cleanup_error is not None:
            primary_error.add_note(
                "battle session startup cleanup also failed: "
                f"{type(cleanup_error).__name__}"
            )
        raise primary_error

    @staticmethod
    async def _wait_for_startup_task(
        task: asyncio.Task[tuple[BaseException | None, BaseException | None]],
    ) -> tuple[
        tuple[BaseException | None, BaseException | None],
        asyncio.CancelledError | None,
    ]:
        """Settle both startup branches before propagating cancellation."""

        delayed_cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if delayed_cancellation is None:
                    delayed_cancellation = error
        return task.result(), delayed_cancellation

    @staticmethod
    async def _wait_for_cleanup_task(
        task: asyncio.Task[None],
    ) -> tuple[BaseException | None, asyncio.CancelledError | None]:
        """Settle startup cleanup before propagating repeated cancellation."""

        delayed_cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if delayed_cancellation is None:
                    delayed_cancellation = error
        try:
            task.result()
        except BaseException as error:
            return error, delayed_cancellation
        return None, delayed_cancellation

    async def attach_browser_hooks(self) -> Self:
        """Install browser hooks without waiting for classifier preload.

        Account-level orchestration can use this boundary immediately after
        login, inspect every fixed realm tab, and await one shared classifier
        preload task only after those latency-sensitive checks are complete.
        The operation is idempotent and never claims browser ownership.
        """

        await self._on_browser_ready()
        return self

    async def prepare_attached(self) -> Self:
        """Prepare an injected, externally owned authenticated browser session.

        This method installs BattleSession's browser hooks without starting,
        authenticating, or later claiming ownership of the composed
        :class:`HentaiVerseSession`.  It is intended for an account context
        that already owns a fixed realm tab.
        """

        await self.attach_browser_hooks()
        await self._ensure_classifier()
        return self

    async def _ensure_classifier(self) -> None:
        if self._classifier_prepared:
            return
        await asyncio.to_thread(preload_ponychart_classifier)
        self._classifier_prepared = True

    async def __aexit__(
        self,
        exc_type: Any,
        exc_value: Any,
        traceback: Any,
    ) -> None:
        await self.hentaiverse.__aexit__(exc_type, exc_value, traceback)

    async def _on_browser_ready(self) -> None:
        if self._browser_hooks_initialized:
            return
        if self.auto_accept_dialogs:
            await self._setup_alert_handler()
        self._browser_hooks_initialized = True

    def _initialize_battle_components(self) -> None:
        """Create battle adapters without parsing a non-battle login page."""
        if self.battle_state is not None:
            return
        browser = self.hentaiverse.browser
        state_store = BattleStateStore(browser)
        actions = ElementActionManager(
            browser,
            begin_dialog_observation=self.action_dialog_tracker.begin,
            get_dialog_category=self.action_dialog_tracker.category_for,
        )
        items = ItemProvider(browser, state_store, actions)
        skills = SkillManager(browser, state_store, actions)
        buffs = BuffManager(
            browser,
            state_store,
            actions,
            items,
            skills,
        )
        self.battle_state = state_store
        self.element_action_manager = actions
        self._item_provider = items
        self._skill_manager = skills
        self._buff_manager = buffs
        self.battle_recovery = BattleRecoveryCoordinator(actions, state_store)

    async def _setup_alert_handler(self) -> None:
        page = self.page

        async def dialog_handler(
            event: cdp.page.JavascriptDialogOpening,
        ) -> None:
            message = str(getattr(event, "message", ""))
            normalized = message.casefold()
            if "server communication failed" in normalized:
                category = "server-communication-failed"
            elif "login" in normalized or "session" in normalized:
                category = "session-or-login"
            else:
                category = "other"
            self.action_dialog_tracker.record(category)
            logger.warning(
                "Auto-accepting JavaScript dialog category=%s message_length=%d",
                category,
                len(message),
            )
            await wait_for_zendriver(
                page.send(cdp.page.handle_java_script_dialog(accept=True)),
                timeout=_DIALOG_MUTATION_TIMEOUT_SECONDS,
                owner=page,
            )

        page.add_handler(cdp.page.JavascriptDialogOpening, dialog_handler)

    def _state(self) -> BattleStateStore:
        if self.battle_state is None:
            raise RuntimeError("Battle components have not been initialized")
        return self.battle_state

    def _actions(self) -> ElementActionManager:
        if self.element_action_manager is None:
            raise RuntimeError("Battle components have not been initialized")
        return self.element_action_manager

    def _items(self) -> ItemProvider:
        if self._item_provider is None:
            raise RuntimeError("Battle components have not been initialized")
        return self._item_provider

    def _skills(self) -> SkillManager:
        if self._skill_manager is None:
            raise RuntimeError("Battle components have not been initialized")
        return self._skill_manager

    def _buffs(self) -> BuffManager:
        if self._buff_manager is None:
            raise RuntimeError("Battle components have not been initialized")
        return self._buff_manager

    @property
    def snapshot(self) -> BattleSnapshot:
        snapshot = self._state().snap
        if snapshot is None:
            raise RuntimeError(
                "No battle snapshot is available before prepare_turn_state()"
            )
        return snapshot

    @property
    def alive_monster_ids(self) -> tuple[int, ...]:
        return tuple(self._state().overview_monsters.alive_monster)

    @property
    def alive_monster_ids_by_name(self) -> dict[str, int]:
        return dict(self._state().overview_monsters.alive_monster_name)

    def monster_ids_with_buff(self, buff: str) -> tuple[int, ...]:
        return tuple(
            self._state().overview_monsters.alive_monster_with_buff.get(buff, ())
        )

    @property
    def current_round(self) -> int:
        return self._state().log_entries.current_round

    @property
    def total_rounds(self) -> int:
        return self._state().log_entries.total_round

    def reset_battle_tracking(self) -> None:
        self.turn = -1
        self.round = -1
        self._completion_observed = False
        self._last_parser_warning_signature = None
        self._last_reported_round_progress = None
        if self.battle_state is not None:
            self.battle_state.reset()

    async def recover_unknown_action(
        self,
        error: BattleActionOutcomeUnknownError,
        *,
        expected_is_isekai: bool,
        auto_reload_checks: int = 4,
        recovery_timeout: float = 10.0,
        stable_checks: int = 2,
        check_interval: float = 0.25,
        probe_timeout: float = 2.0,
        reload_timeout: float = 15.0,
    ) -> bool:
        """Delegate reload reconciliation, then clear only session-owned caches."""
        if not isinstance(error, BattleActionOutcomeUnknownError):
            raise TypeError("error must be BattleActionOutcomeUnknownError")
        if not isinstance(expected_is_isekai, bool):
            raise TypeError("expected_is_isekai must be bool")
        evidence = error.recovery_evidence
        if evidence is None:
            return False

        expected_realm = "isekai" if expected_is_isekai else "persistent"
        recovered = await self.battle_recovery.recover(
            evidence,
            expected_realm=expected_realm,
            auto_reload_checks=auto_reload_checks,
            recovery_timeout=recovery_timeout,
            stable_checks=stable_checks,
            check_interval=check_interval,
            probe_timeout=probe_timeout,
            reload_timeout=reload_timeout,
        )
        if not recovered:
            return False

        self.action_dialog_tracker.clear()
        self.round = -1
        self._completion_observed = False
        self._last_parser_warning_signature = None
        self._last_reported_round_progress = None
        return True

    def _round_progress_text(self) -> str:
        """Render unavailable resumed-battle metadata without inventing round zero."""
        if self.current_round <= 0 or self.total_rounds <= 0:
            return "Round   ? / ?  "
        return f"Round {self.current_round:>3} / {self.total_rounds:<3}"

    async def prepare_turn_state(self) -> BattleTurnState:
        """Refresh the page and return one explicit turn-preparation state."""
        if not await self._has_battle_marker():
            return BattleTurnState(BattleTurnPhase.ABSENT)

        phase = await self._read_battle_phase()
        if phase == _BATTLE_PHASE_COMPLETE:
            self._completion_observed = True
            logger.debug("Final battle completion control is ready.")
            return BattleTurnState(BattleTurnPhase.COMPLETE)
        if phase == _BATTLE_PHASE_NEXT_FLOOR:
            return self._prepare_round_transition()
        if await self.is_ponychart_present():
            return BattleTurnState(BattleTurnPhase.CHALLENGE)

        try:
            await self._state().update()
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            if await self.is_ponychart_present():
                return BattleTurnState(BattleTurnPhase.CHALLENGE)
            phase = await self._read_battle_phase()
            if phase == _BATTLE_PHASE_COMPLETE:
                self._completion_observed = True
                logger.debug("Final battle completion control appeared while parsing.")
                return BattleTurnState(BattleTurnPhase.COMPLETE)
            if phase == _BATTLE_PHASE_NEXT_FLOOR:
                return self._prepare_round_transition()
            raise

        phase = await self._read_battle_phase()
        if phase == _BATTLE_PHASE_COMPLETE:
            self._completion_observed = True
            logger.debug("Final battle completion control appeared after parsing.")
            return BattleTurnState(BattleTurnPhase.COMPLETE)
        if phase == _BATTLE_PHASE_NEXT_FLOOR:
            return self._prepare_round_transition()
        parser_warnings = tuple(self.snapshot.warnings)
        if parser_warnings:
            displayed_warnings = parser_warnings[:5]
            warning_signature = (len(parser_warnings), displayed_warnings)
            log_parser_warnings = (
                logger.warning
                if warning_signature
                != getattr(self, "_last_parser_warning_signature", None)
                else logger.debug
            )
            log_parser_warnings(
                "Battle parser warnings count=%d first=%s",
                len(parser_warnings),
                ", ".join(displayed_warnings),
            )
            self._last_parser_warning_signature = warning_signature
        else:
            self._last_parser_warning_signature = None
        if not self.alive_monster_ids:
            raise TimeoutError(
                "Battle DOM has no monsters, round transition, or final "
                "completion marker"
            )
        current_round = self.current_round
        total_rounds = self.total_rounds
        progress_key = (
            (current_round, total_rounds)
            if current_round > 0 and total_rounds > 0
            else (0, 0)
        )
        self.turn += 1
        self.round = current_round
        turn_text = f"Turn {self.turn:>5}"
        round_text = self._round_progress_text()
        if progress_key != getattr(self, "_last_reported_round_progress", None):
            if progress_key == (0, 0):
                logger.info(
                    "Battle detected; round data is not available yet",
                    extra={"activity": "Battle"},
                )
            else:
                logger.info(
                    "Round %d/%d HP %.1f%% MP %.1f%% SP %.1f%% OC %d",
                    current_round,
                    total_rounds,
                    self.hp_percent,
                    self.mp_percent,
                    self.sp_percent,
                    int(self.overcharge),
                    extra={"activity": "Battle"},
                )
            self._last_reported_round_progress = progress_key
        lines = tuple(
            f"{turn_text} {round_text} {line}"
            for line in self._state().log_entries.current_lines
        )
        for line in lines:
            logger.debug("%s", line)
        return BattleTurnState(BattleTurnPhase.ACTIVE, lines)

    def _prepare_round_transition(self) -> BattleTurnState:
        """Expose a transition as a strategy decision without parsing monsters."""
        self.turn += 1
        logger.debug("Turn %5d: next-round control is ready.", self.turn)
        return BattleTurnState(BattleTurnPhase.NEXT_FLOOR)

    async def is_ponychart_present(self) -> bool:
        """Inspect challenge presence without parsing ordinary battle HTML."""
        return await self._ponychart.is_present()

    async def _read_battle_phase(self) -> str:
        """Read final and next-floor controls atomically, with final priority."""
        phase = await wait_for_zendriver(
            self.page.evaluate(_BATTLE_PHASE_JS),
            timeout=8.0,
            owner=self.page,
        )
        if phase not in {
            _BATTLE_PHASE_ACTIVE,
            _BATTLE_PHASE_COMPLETE,
            _BATTLE_PHASE_NEXT_FLOOR,
        }:
            raise RuntimeError(f"Invalid battle phase payload: {phase!r}")
        return str(phase)

    async def _has_battle_completion_marker(self) -> bool:
        """Recognize the game's explicit final-battle image control."""
        return await self._read_battle_phase() == _BATTLE_PHASE_COMPLETE

    async def _has_battle_marker(self) -> bool:
        """Check once whether #battle_main is present right now.

        Deliberately evaluate() rather than xpath(): zendriver's xpath()
        retries in an enable/find/disable DOM-domain loop until its element
        appears or its timeout elapses, so it always burns the full timeout
        whenever the marker is genuinely absent -- the common case whenever
        no battle is running. A production incident (2026-08-19) showed this
        combined with an equal outer wait_for_zendriver bound to make that
        completely normal "no battle" result race-lose against the outer
        bound and surface as a fatal, non-retried ZendriverOperationTimeout
        on every single startup. A single evaluate() has no such retry loop.
        """
        return bool(
            await wait_for_zendriver(
                self.page.evaluate("Boolean(document.getElementById('battle_main'))"),
                timeout=5.0,
                owner=self.page,
            )
        )

    @property
    def hp_percent(self) -> float:
        return float(self.snapshot.player.hp_percent)

    @property
    def mp_percent(self) -> float:
        return float(self.snapshot.player.mp_percent)

    @property
    def sp_percent(self) -> float:
        return float(self.snapshot.player.sp_percent)

    @property
    def overcharge(self) -> float:
        """Return the raw overcharge value; it is not a percentage."""
        return float(self.snapshot.player.overcharge_value)

    async def cast_skill(self, key: str) -> bool:
        """Cast a non-targeted skill and wait for the resulting game action."""
        return await self._skills().cast(key, iswait=True)

    async def select_targeted_skill(self, key: str) -> bool:
        """Select a targeted skill without claiming a completed game action."""
        return await self._skills().cast(key, iswait=False)

    async def use_item(self, key: str) -> bool:
        return await self._items().use(key)

    async def apply_buff(self, key: str, *, force: bool = False) -> bool:
        return await self._buffs().apply_buff(key=key, force=force)

    def is_buff_action_needed(self, key: str, *, force: bool = False) -> bool:
        """Inspect whether applying a named buff would be useful now."""
        return self._buffs().is_action_needed(key, force=force)

    def has_buff(self, key: str) -> bool:
        return self._buffs().has_buff(key)

    def get_buff_remaining_turns(self, key: str) -> int | float:
        return self._buffs().get_buff_remaining_turns(key)

    def get_buff_observed_max_turns(self, key: str) -> int:
        return self._buffs().get_observed_max_turns(key)

    def get_max_skill_mp_cost(self, key: str) -> int:
        return self._skills().get_max_skill_mp_cost_by_name(key)

    async def attack_monster(self, slot: int) -> bool:
        selector = f'[id="mkey_{slot}"]'
        elements = await wait_for_zendriver(
            self.page.query_selector_all(selector),
            timeout=3.0,
            owner=self.page,
        )
        if not elements:
            return False
        await self._actions().click_and_wait_log_locator(selector)
        return True

    async def attack_monster_by_skill(self, slot: int, skill_name: str) -> bool:
        if not await self.select_targeted_skill(skill_name):
            return False
        return await self.submit_selected_skill_target(slot, skill_name)

    async def submit_selected_skill_target(
        self,
        slot: int,
        skill_name: str,
    ) -> bool:
        """Submit the target for an already-selected skill through one boundary."""
        try:
            acted = await self.attack_monster(slot)
        except BattleActionOutcomeUnknownError:
            # Recovery can only be authorized later from the immutable evidence.
            raise
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise BattleInterruptedError(
                f"Target submission did not clear armed skill {skill_name!r}",
                diagnostic_code="battle.skill-target-submission-unknown",
            ) from error
        if not acted:
            raise BattleInterruptedError(
                f"Target disappeared while skill {skill_name!r} was armed",
                diagnostic_code="battle.skill-target-disappeared",
            )
        return True

    @property
    def battle_completion_observed(self) -> bool:
        """Whether the game's explicit final-battle control was observed."""
        return self._completion_observed

    async def resolve_ponychart(self) -> bool:
        return await self._ponychart.check()

    async def goto_arena(self) -> bool:
        return await self._launcher.goto_arena()

    async def goto_ring_of_blood(self) -> bool:
        return await self._launcher.goto_ring_of_blood()

    async def goto_grindfest(self) -> bool:
        return await self._launcher.goto_grindfest()

    async def go_next_floor(self) -> bool:
        elements = await wait_for_zendriver(
            self.page.query_selector_all("#btcp"),
            timeout=3.0,
            owner=self.page,
        )
        if not elements:
            return False
        await self._actions().click_and_wait_transition_locator("#btcp")
        return True

    async def acknowledge_battle_completion(self, *, expected_is_isekai: bool) -> None:
        """Click the observed final control once and confirm battle exit."""
        if not self._completion_observed:
            raise BattleActionOutcomeUnknownError(
                "Final battle completion was not observed before acknowledgement"
            )
        await self._actions().click_and_wait_battle_exit_locator(
            _FINAL_COMPLETION_SELECTOR,
            expected_is_isekai=expected_is_isekai,
        )

    async def list_arena_options(self) -> tuple[ArenaOption, ...]:
        return await self._launcher.list_arena_options()

    async def start_arena(self, option: ArenaOption) -> bool:
        return await self._launcher.start_arena(option)

    async def inspect_ring_of_blood(self) -> RingOfBloodSnapshot:
        return await self._launcher.inspect_ring_of_blood()

    async def start_ring_of_blood(
        self,
        option: RingOfBloodOption,
        *,
        expected_before: RingOfBloodSnapshot,
    ) -> RingOfBloodStartOutcome:
        return await self._launcher.start_ring_of_blood(
            option,
            expected_before=expected_before,
        )

    async def list_grindfest_options(self) -> tuple[GrindfestOption, ...]:
        return await self._launcher.list_grindfest_options()

    async def start_grindfest(self, option: GrindfestOption) -> bool:
        return await self._launcher.start_grindfest(option)

    async def inspect_battle_presence(self) -> BattlePresence:
        """Distinguish an absent, active, or awaiting-acknowledgement battle."""

        if await self.is_ponychart_present():
            return BattlePresence.ACTIVE
        phase = await self._read_battle_phase()
        if phase == _BATTLE_PHASE_COMPLETE:
            self._completion_observed = True
            return BattlePresence.COMPLETION
        if phase == _BATTLE_PHASE_NEXT_FLOOR:
            return BattlePresence.ACTIVE
        if not await self._has_battle_marker():
            if await self.is_ponychart_present():
                return BattlePresence.ACTIVE
            logger.debug("No active battle detected.")
            return BattlePresence.ABSENT

        last_error: Exception | None = None
        parse_attempts = 2
        retry_delay = 1.0
        for attempt in range(parse_attempts):
            try:
                snapshot = await self._state().inspect()
                if any(monster.alive for monster in snapshot.monsters.values()):
                    return BattlePresence.ACTIVE
                if await self.is_ponychart_present():
                    return BattlePresence.ACTIVE
                phase = await self._read_battle_phase()
                if phase == _BATTLE_PHASE_COMPLETE:
                    self._completion_observed = True
                    return BattlePresence.COMPLETION
                if phase == _BATTLE_PHASE_NEXT_FLOOR:
                    return BattlePresence.ACTIVE
                warnings = ", ".join(snapshot.warnings[:5]) or "none"
                raise RuntimeError(
                    "Battle marker is present without monsters, a transition, "
                    "or completion evidence; parser warnings=" + warnings
                )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                last_error = error
                if await self.is_ponychart_present():
                    return BattlePresence.ACTIVE
                phase = await self._read_battle_phase()
                if phase == _BATTLE_PHASE_COMPLETE:
                    self._completion_observed = True
                    return BattlePresence.COMPLETION
                if phase == _BATTLE_PHASE_NEXT_FLOOR:
                    return BattlePresence.ACTIVE
                if isinstance(error, TimeoutError):
                    raise
                if attempt + 1 < parse_attempts:
                    logger.warning(
                        "Battle state parse failed; retrying next_attempt=%d/%d "
                        "delay=%.1fs error_type=%s",
                        attempt + 2,
                        parse_attempts,
                        retry_delay,
                        type(error).__name__,
                    )
                    logger.debug(
                        "Battle state parse retry error detail",
                        exc_info=True,
                    )
                    await wait_for_zendriver(
                        self.page.wait(retry_delay),
                        timeout=retry_delay + 2.0,
                        owner=self.page,
                    )

        phase = await self._read_battle_phase()
        if phase == _BATTLE_PHASE_COMPLETE:
            self._completion_observed = True
            return BattlePresence.COMPLETION
        if phase == _BATTLE_PHASE_NEXT_FLOOR:
            return BattlePresence.ACTIVE
        if await self._has_battle_marker():
            logger.error(
                "Battle state parse failed after %d attempts on an active battle "
                "page; refusing to report no battle: error_type=%s",
                parse_attempts,
                type(last_error).__name__,
            )
            assert last_error is not None
            raise last_error

        logger.debug("No active battle detected.")
        return BattlePresence.ABSENT

    async def is_in_battle(self) -> bool:
        """Return whether the current page represents an active battle."""

        return await self.inspect_battle_presence() is BattlePresence.ACTIVE

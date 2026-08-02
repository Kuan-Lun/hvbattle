"""Exactly-once battle actions with authoritative completion evidence."""

import asyncio
import json
import re
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from hvbrowser import HVDriver
from hvbrowser.runtime import ElementAction, setup_logger

from .contracts import BattleActionOutcomeUnknownError

logger = setup_logger(__name__)


# HentaiVerse submits a turn through one JSON XHR, then mutates battle panes and
# prepends rows to #textlog.  Observe that protocol directly.  A body-wide hash
# is not useful here because the game's flash loop continuously changes styles.
_ACTION_STATE_JS = r"""
(() => {
    const monitorId = __MONITOR_ID__;
    const armMonitor = __ARM_MONITOR__;

    const fingerprint = (value) => {
        if (value === null || value === undefined) return null;
        let hash = 2166136261;
        for (let i = 0; i < value.length; i++) {
            hash ^= value.charCodeAt(i);
            hash = Math.imul(hash, 16777619);
        }
        return `${(hash >>> 0).toString(16).padStart(8, "0")}:${value.length}`;
    };

    const makeId = () =>
        `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

    if (!globalThis.__hvbattleDocumentId) {
        globalThis.__hvbattleDocumentId = makeId();
    }

    const battleMain = document.getElementById("battle_main");
    if (battleMain && !battleMain.__hvbattleNodeId) {
        battleMain.__hvbattleNodeId = makeId();
    }

    if (armMonitor) {
        const previous = globalThis.__hvbattleActionMonitor;
        if (previous && typeof previous.restore === "function") {
            previous.restore();
        }

        const originalOpen = XMLHttpRequest.prototype.open;
        const originalSend = XMLHttpRequest.prototype.send;
        const requestMetadata = new WeakMap();
        const monitor = {
            id: monitorId,
            sent: false,
            sentCount: 0,
            completed: false,
            status: null,
            outcome: null,
            logMutations: 0,
            response: null,
            observer: null,
            restore: null,
        };

        const log = document.getElementById("textlog");
        if (log) {
            monitor.observer = new MutationObserver((mutations) => {
                for (const mutation of mutations) {
                    if (
                        mutation.type === "childList"
                        && (mutation.addedNodes.length || mutation.removedNodes.length)
                    ) {
                        monitor.logMutations += 1;
                    }
                }
            });
            monitor.observer.observe(log, {childList: true, subtree: true});
        }

        const wrappedOpen = function (...args) {
            requestMetadata.set(this, {
                method: typeof args[0] === "string"
                    ? args[0].toUpperCase()
                    : "",
                url: typeof args[1] === "string" ? args[1] : "",
            });
            return originalOpen.apply(this, args);
        };

        const wrappedSend = function (...args) {
            let isBattleAction = false;
            try {
                const payload = typeof args[0] === "string"
                    ? JSON.parse(args[0])
                    : null;
                const metadata = requestMetadata.get(this);
                const endpoint = metadata
                    ? new URL(metadata.url, document.baseURI).pathname
                    : "";
                const hasOwn = (key) =>
                    Object.prototype.hasOwnProperty.call(payload, key);
                isBattleAction = Boolean(
                    payload
                    && metadata
                    && metadata.method === "POST"
                    && endpoint.endsWith("/json")
                    && payload.type === "battle"
                    && payload.method === "action"
                    && hasOwn("token")
                    && hasOwn("mode")
                    && hasOwn("target")
                    && hasOwn("skill")
                );
            } catch (_) {
                isBattleAction = false;
            }

            if (isBattleAction) {
                monitor.sentCount += 1;
                monitor.sent = true;
                if (monitor.sentCount === 1) {
                    // Ignore any third-party log mutation that happened after
                    // arming but before this exact request was dispatched.
                    if (
                        monitor.observer
                        && typeof monitor.observer.takeRecords === "function"
                    ) {
                        monitor.observer.takeRecords();
                    }
                    monitor.logMutations = 0;
                    for (const outcome of ["load", "error", "abort", "timeout"]) {
                        this.addEventListener(
                            outcome,
                            () => { monitor.outcome = outcome; },
                            {once: true},
                        );
                    }
                    this.addEventListener(
                        "loadend",
                        () => {
                            monitor.completed = true;
                            monitor.status = this.status;
                            const flags = {
                                parseOk: false,
                                hasTextlog: false,
                                hasPaneCompletion: false,
                                hasError: false,
                                hasReload: false,
                                hasLogin: false,
                            };
                            try {
                                const response = JSON.parse(this.responseText);
                                if (response && typeof response === "object") {
                                    const present = (key) =>
                                        response[key] !== undefined
                                        && response[key] !== null;
                                    flags.parseOk = true;
                                    flags.hasTextlog = present("textlog");
                                    flags.hasPaneCompletion = present(
                                        "pane_completion"
                                    );
                                    flags.hasError = present("error");
                                    flags.hasReload = present("reload");
                                    flags.hasLogin = present("login");
                                }
                            } catch (_) {
                                flags.parseOk = false;
                            }
                            monitor.response = flags;
                        },
                        {once: true},
                    );
                }
            }
            return originalSend.apply(this, args);
        };

        monitor.restore = () => {
            if (XMLHttpRequest.prototype.open === wrappedOpen) {
                XMLHttpRequest.prototype.open = originalOpen;
            }
            if (XMLHttpRequest.prototype.send === wrappedSend) {
                XMLHttpRequest.prototype.send = originalSend;
            }
            if (monitor.observer) monitor.observer.disconnect();
        };
        globalThis.__hvbattleActionMonitor = monitor;
        XMLHttpRequest.prototype.open = wrappedOpen;
        XMLHttpRequest.prototype.send = wrappedSend;
    }

    const log = document.getElementById("textlog");
    const completion = document.getElementById("pane_completion");
    const nextFloor = document.getElementById("btcp");
    const monitor = globalThis.__hvbattleActionMonitor;
    const matchingMonitor = monitor && monitor.id === monitorId ? monitor : null;
    const firstLogCell = log ? log.querySelector("td") : null;
    const completionHtml = completion ? completion.innerHTML : null;
    const initializingCell = log
        ? Array.from(log.querySelectorAll("td")).find(
            (cell) => /Initializing .*\(Round \d+ \/ \d+\)/.test(
                cell.textContent || "",
            ),
        )
        : null;
    const response = matchingMonitor ? matchingMonitor.response : null;
    const completionPresent = Boolean(
        completion && completion.innerHTML.trim().length > 0
    );
    const finishImagePresent = Boolean(
        completion
        && completion.querySelector('img[src*="finishbattle.png"]')
    );

    return {
        documentId: globalThis.__hvbattleDocumentId,
        battleNodeId: battleMain ? battleMain.__hvbattleNodeId : null,
        readyState: document.readyState,
        battlePresent: Boolean(battleMain),
        logRevision: log ? fingerprint(log.innerHTML) : null,
        logRows: log ? log.querySelectorAll("tr").length : 0,
        latestLog: firstLogCell ? firstLogCell.textContent.trim() : null,
        roundText: initializingCell
            ? initializingCell.textContent.trim()
            : null,
        completionPresent,
        battleCompletePresent: finishImagePresent,
        finishImagePresent,
        completionRevision: completionHtml === null
            ? null
            : fingerprint(completionHtml),
        nextFloorPresent: Boolean(nextFloor),
        ponychartPresent: Boolean(document.getElementById("riddlesubmit")),
        actionControls: document.querySelectorAll(
            '#pane_monster [id^="mkey_"][onclick]'
        ).length,
        monitor: matchingMonitor ? {
            sent: matchingMonitor.sent,
            sentCount: matchingMonitor.sentCount,
            completed: matchingMonitor.completed,
            status: matchingMonitor.status,
            outcome: matchingMonitor.outcome,
            logMutations: matchingMonitor.logMutations,
            responseParseOk: response ? response.parseOk : null,
            responseHasTextlog: response ? response.hasTextlog : false,
            responseHasPaneCompletion: response
                ? response.hasPaneCompletion
                : false,
            responseHasError: response ? response.hasError : false,
            responseHasReload: response ? response.hasReload : false,
            responseHasLogin: response ? response.hasLogin : false,
        } : null,
    };
})()
"""

_CLEANUP_ACTION_MONITOR_JS = """
(() => {
    const monitor = globalThis.__hvbattleActionMonitor;
    if (!monitor || monitor.id !== __MONITOR_ID__) return false;
    if (typeof monitor.restore === "function") monitor.restore();
    delete globalThis.__hvbattleActionMonitor;
    return true;
})()
"""


@dataclass(frozen=True, slots=True)
class _ActionMonitorState:
    sent: bool
    sent_count: int
    completed: bool
    status: int | None
    outcome: str | None
    log_mutations: int
    response_parse_ok: bool | None
    response_has_textlog: bool
    response_has_pane_completion: bool
    response_has_error: bool
    response_has_reload: bool
    response_has_login: bool


@dataclass(frozen=True, slots=True)
class _BattleActionState:
    document_id: str
    battle_node_id: str | None
    ready_state: str
    battle_present: bool
    log_revision: str | None
    log_rows: int
    latest_log: str | None
    round_text: str | None
    completion_present: bool
    battle_complete_present: bool
    finish_image_present: bool
    completion_revision: str | None
    next_floor_present: bool
    ponychart_present: bool
    action_controls: int
    monitor: _ActionMonitorState | None

    @classmethod
    def from_raw(cls, raw: object) -> _BattleActionState:
        if not isinstance(raw, Mapping):
            raise RuntimeError("Invalid battle action state payload")
        monitor_raw = raw.get("monitor")
        monitor = None
        if isinstance(monitor_raw, Mapping):
            status_raw = monitor_raw.get("status")
            monitor = _ActionMonitorState(
                sent=bool(monitor_raw.get("sent")),
                sent_count=int(monitor_raw.get("sentCount") or 0),
                completed=bool(monitor_raw.get("completed")),
                status=(
                    int(status_raw)
                    if isinstance(status_raw, int) and not isinstance(status_raw, bool)
                    else None
                ),
                outcome=(
                    str(monitor_raw["outcome"])
                    if monitor_raw.get("outcome") is not None
                    else None
                ),
                log_mutations=int(monitor_raw.get("logMutations") or 0),
                response_parse_ok=(
                    bool(monitor_raw["responseParseOk"])
                    if monitor_raw.get("responseParseOk") is not None
                    else None
                ),
                response_has_textlog=bool(monitor_raw.get("responseHasTextlog")),
                response_has_pane_completion=bool(
                    monitor_raw.get("responseHasPaneCompletion")
                ),
                response_has_error=bool(monitor_raw.get("responseHasError")),
                response_has_reload=bool(monitor_raw.get("responseHasReload")),
                response_has_login=bool(monitor_raw.get("responseHasLogin")),
            )
        return cls(
            document_id=str(raw.get("documentId") or "unknown"),
            battle_node_id=(
                str(raw["battleNodeId"])
                if raw.get("battleNodeId") is not None
                else None
            ),
            ready_state=str(raw.get("readyState") or "unknown"),
            battle_present=bool(raw.get("battlePresent")),
            log_revision=(
                str(raw["logRevision"]) if raw.get("logRevision") is not None else None
            ),
            log_rows=int(raw.get("logRows") or 0),
            latest_log=(
                str(raw["latestLog"]) if raw.get("latestLog") is not None else None
            ),
            round_text=(
                str(raw["roundText"]) if raw.get("roundText") is not None else None
            ),
            completion_present=bool(raw.get("completionPresent")),
            battle_complete_present=bool(raw.get("battleCompletePresent")),
            finish_image_present=bool(raw.get("finishImagePresent")),
            completion_revision=(
                str(raw["completionRevision"])
                if raw.get("completionRevision") is not None
                else None
            ),
            next_floor_present=bool(raw.get("nextFloorPresent")),
            ponychart_present=bool(raw.get("ponychartPresent")),
            action_controls=int(raw.get("actionControls") or 0),
            monitor=monitor,
        )

    def summary(self) -> str:
        latest_log = (self.latest_log or "")[:160]
        if self.monitor is None:
            xhr = "unavailable"
        else:
            xhr = (
                f"sent={int(self.monitor.sent)},"
                f"sent_count={self.monitor.sent_count},"
                f"completed={int(self.monitor.completed)},"
                f"status={self.monitor.status},outcome={self.monitor.outcome},"
                f"parsed={self.monitor.response_parse_ok},"
                f"log_mutations={self.monitor.log_mutations},"
                f"response_error={int(self.monitor.response_has_error)},"
                f"response_reload={int(self.monitor.response_has_reload)},"
                f"response_login={int(self.monitor.response_has_login)}"
            )
        return (
            f"doc={self.document_id[:12]},node={(self.battle_node_id or '')[:12]},"
            f"ready={self.ready_state},battle={int(self.battle_present)},"
            f"log={self.log_revision},rows={self.log_rows},"
            f"round={self.round_text!r},"
            f"completion={int(self.completion_present)},"
            f"battle_complete={int(self.battle_complete_present)},"
            f"finish_image={int(self.finish_image_present)},"
            f"next_floor={int(self.next_floor_present)},"
            f"ponychart={int(self.ponychart_present)},"
            f"actions={self.action_controls},xhr=({xhr}),"
            f"latest_log={latest_log!r}"
        )


def _normal_action_response(monitor: _ActionMonitorState | None) -> bool:
    return bool(
        monitor
        and monitor.sent
        and monitor.sent_count == 1
        and monitor.completed
        and monitor.status is not None
        and monitor.status == 200
        and monitor.outcome == "load"
        and monitor.response_parse_ok
        and not monitor.response_has_error
        and not monitor.response_has_reload
        and not monitor.response_has_login
    )


def _confirmed_action_evidence(
    before: _BattleActionState, current: _BattleActionState
) -> str | None:
    """Return evidence only for a matched, acknowledged turn action."""
    if current.document_id != before.document_id:
        return None
    monitor = current.monitor
    if not _normal_action_response(monitor) or monitor is None:
        return None
    if monitor.response_has_textlog and monitor.log_mutations > 0:
        return "xhr-ack+combat-log-mutation"
    if (
        monitor.response_has_textlog
        and current.log_revision is not None
        and current.log_revision != before.log_revision
    ):
        return "xhr-ack+combat-log-revision"
    if monitor.response_has_pane_completion and (
        current.completion_revision != before.completion_revision
        or (current.completion_present and not before.completion_present)
    ):
        if current.battle_complete_present:
            return "xhr-ack+battle-completion"
        if current.next_floor_present:
            return "xhr-ack+round-completion"
        return "xhr-ack+completion-pane"
    return None


_ROUND_PATTERN = re.compile(r"\bRound\s+(\d+)\s*/\s*(\d+)\b")


def _round_number(state: _BattleActionState) -> int | None:
    match = _ROUND_PATTERN.search(state.round_text or "")
    return int(match.group(1)) if match else None


def _confirmed_transition_evidence(
    before: _BattleActionState, current: _BattleActionState
) -> str | None:
    """Return positive evidence that a next-floor navigation committed."""
    if current.ponychart_present and not before.ponychart_present:
        return "ponychart-present"
    if current.battle_complete_present and not before.battle_complete_present:
        return "battle-completion-present"

    before_round = _round_number(before)
    current_round = _round_number(current)
    generation_changed = (
        current.document_id != before.document_id
        or current.battle_node_id != before.battle_node_id
    )
    round_advanced = (
        before_round is not None
        and current_round is not None
        and current_round > before_round
    )
    round_initialized = (
        before.next_floor_present and before_round is None and current_round is not None
    )
    actionable_battle = bool(
        current.battle_present
        and current.log_revision is not None
        and not current.next_floor_present
        and current.action_controls > 0
    )
    if round_advanced and actionable_battle:
        if generation_changed and current.ready_state == "complete":
            return "battle-generation+round-advanced"
        if not generation_changed:
            return "battle-round-advanced"
    if round_initialized and actionable_battle:
        if generation_changed and current.ready_state == "complete":
            return "battle-generation+round-initialized"
        if (
            not generation_changed
            and current.log_revision is not None
            and current.log_revision != before.log_revision
        ):
            return "battle-round-initialized"
    return None


class ElementActionManager:
    def __init__(self, driver: HVDriver) -> None:
        self.hvdriver = driver
        self._action = ElementAction(lambda: driver.page)
        self._action_lock = asyncio.Lock()

    @property
    def page(self) -> Any:
        return self.hvdriver.page

    async def _click(self, element: Any) -> None:
        await self._action.click(element)

    async def click_resilient(
        self,
        get_element: Callable[[], Coroutine[Any, Any, Any]],
        retries: int = 3,
        delay: float = 0.1,
    ) -> None:
        """Click a local UI control with retries; never use for a turn action."""
        await self._action.click_resilient(get_element, retries=retries, delay=delay)

    async def click_until(
        self,
        get_element: Callable[[], Coroutine[Any, Any, Any]],
        condition: Callable[[], Coroutine[Any, Any, bool]],
        max_attempts: int = 5,
        delay: float = 0.1,
        timeout: float = 0.3,
    ) -> None:
        """Click a local UI control repeatedly until a local condition is true."""
        await self._action.click_until(
            get_element,
            condition,
            max_attempts=max_attempts,
            delay=delay,
            timeout=timeout,
        )

    async def click_locator(
        self,
        selector: str,
        retries: int = 3,
        wait_timeout: float = 2.0,
        delay: float = 0.1,
    ) -> None:
        """Click a local UI control that does not submit a battle turn."""
        await self._action.click_locator(
            selector, retries=retries, wait_timeout=wait_timeout, delay=delay
        )

    @staticmethod
    def _state_script(monitor_id: str, *, arm_monitor: bool) -> str:
        return _ACTION_STATE_JS.replace(
            "__MONITOR_ID__", json.dumps(monitor_id)
        ).replace("__ARM_MONITOR__", "true" if arm_monitor else "false")

    async def _read_action_state(
        self,
        monitor_id: str,
        *,
        arm_monitor: bool = False,
        probe_timeout: float = 3.0,
    ) -> _BattleActionState:
        raw = await asyncio.wait_for(
            self.page.evaluate(self._state_script(monitor_id, arm_monitor=arm_monitor)),
            timeout=probe_timeout,
        )
        return _BattleActionState.from_raw(raw)

    async def _cleanup_action_monitor(
        self, monitor_id: str, *, probe_timeout: float
    ) -> None:
        script = _CLEANUP_ACTION_MONITOR_JS.replace(
            "__MONITOR_ID__", json.dumps(monitor_id)
        )
        try:
            await asyncio.wait_for(
                self.page.evaluate(script),
                timeout=probe_timeout,
            )
        except Exception as error:
            logger.debug(
                "Unable to clean up battle action monitor %s: %r",
                monitor_id[:8],
                error,
            )

    async def _select_for_single_click(
        self,
        selector: str,
        *,
        retries: int,
        wait_timeout: float,
        delay: float,
    ) -> Any:
        if retries < 1:
            raise ValueError("stale_retries must be at least 1")
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                return await self.page.select(selector, timeout=wait_timeout)
            except Exception as error:
                last_error = error
                if attempt + 1 < retries:
                    await asyncio.sleep(delay)
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Unable to locate {selector!r}")

    async def _final_action_probe(
        self,
        monitor_id: str,
        *,
        probe_timeout: float,
        fallback: _BattleActionState,
    ) -> tuple[_BattleActionState, Exception | None]:
        try:
            return (
                await self._read_action_state(
                    monitor_id,
                    probe_timeout=probe_timeout,
                ),
                None,
            )
        except Exception as error:
            return fallback, error

    async def click_and_wait_log_locator(
        self,
        selector: str,
        stale_retries: int = 3,
        timeout: float = 15.0,
        check_interval: float = 0.2,
        probe_timeout: float = 3.0,
    ) -> None:
        """Submit one battle action exactly once and await its formal receipt.

        Only element lookup is retried.  Once the click begins, an ambiguous
        error is reconciled but the click is never repeated.
        """
        if timeout <= 0 or check_interval <= 0 or probe_timeout <= 0:
            raise ValueError("battle action timeouts must be positive")

        async with self._action_lock:
            element = await self._select_for_single_click(
                selector,
                retries=stale_retries,
                wait_timeout=2.0,
                delay=0.1,
            )
            monitor_id = uuid4().hex
            try:
                before = await self._read_action_state(
                    monitor_id,
                    arm_monitor=True,
                    probe_timeout=probe_timeout,
                )
                last = before
                click_started = False
                click_error: Exception | None = None
                probe_error: Exception | None = None
                post_click_probe_succeeded = False
                started = asyncio.get_running_loop().time()
                click_started = True
                try:
                    await self._click(element)
                except Exception as error:
                    click_error = error

                deadline = started + timeout
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        current = await self._read_action_state(
                            monitor_id,
                            probe_timeout=probe_timeout,
                        )
                    except Exception as error:
                        probe_error = error
                    else:
                        last = current
                        post_click_probe_succeeded = True
                        evidence = _confirmed_action_evidence(before, current)
                        if evidence is not None:
                            elapsed = asyncio.get_running_loop().time() - started
                            status = current.monitor.status if current.monitor else None
                            logger.info(
                                "Battle action confirmed selector=%r "
                                "evidence=%s elapsed=%.2fs xhr_status=%s",
                                selector,
                                evidence,
                                elapsed,
                                status,
                            )
                            logger.debug("Battle action state: %s", current.summary())
                            return
                        monitor = current.monitor
                        if (
                            monitor
                            and monitor.completed
                            and not _normal_action_response(monitor)
                        ):
                            break
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining > 0:
                        await asyncio.sleep(min(check_interval, remaining))

                last, final_error = await self._final_action_probe(
                    monitor_id,
                    probe_timeout=probe_timeout,
                    fallback=last,
                )
                if final_error is not None:
                    probe_error = final_error
                else:
                    post_click_probe_succeeded = True
                evidence = _confirmed_action_evidence(before, last)
                if evidence is not None:
                    elapsed = asyncio.get_running_loop().time() - started
                    status = last.monitor.status if last.monitor else None
                    logger.info(
                        "Battle action confirmed during final reconciliation "
                        "selector=%r evidence=%s elapsed=%.2fs xhr_status=%s",
                        selector,
                        evidence,
                        elapsed,
                        status,
                    )
                    return

                if (
                    click_started
                    and post_click_probe_succeeded
                    and last.document_id == before.document_id
                    and last.monitor is not None
                    and not last.monitor.sent
                ):
                    raise TimeoutError(
                        "Battle action was not dispatched after one click; "
                        f"selector={selector!r}; state={last.summary()}"
                    ) from click_error

                logger.error(
                    "Battle action outcome unknown selector=%r before=(%s) last=(%s)",
                    selector,
                    before.summary(),
                    last.summary(),
                )
                unknown = BattleActionOutcomeUnknownError(
                    "Submitted battle action lacks an authoritative receipt; "
                    f"selector={selector!r}; last_state={last.summary()}"
                )
                cause = click_error or probe_error
                if cause is not None:
                    raise unknown from cause
                raise unknown
            finally:
                await self._cleanup_action_monitor(
                    monitor_id,
                    probe_timeout=probe_timeout,
                )

    async def click_and_wait_transition_locator(
        self,
        selector: str,
        stale_retries: int = 3,
        timeout: float = 20.0,
        check_interval: float = 0.25,
        probe_timeout: float = 3.0,
    ) -> None:
        """Click a round-transition control once and await a valid next phase."""
        if timeout <= 0 or check_interval <= 0 or probe_timeout <= 0:
            raise ValueError("battle transition timeouts must be positive")

        async with self._action_lock:
            element = await self._select_for_single_click(
                selector,
                retries=stale_retries,
                wait_timeout=2.0,
                delay=0.1,
            )
            state_id = uuid4().hex
            before = await self._read_action_state(
                state_id,
                probe_timeout=probe_timeout,
            )
            started = asyncio.get_running_loop().time()
            click_error: Exception | None = None
            probe_error: Exception | None = None
            last = before
            try:
                await self._click(element)
            except Exception as error:
                click_error = error

            deadline = started + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    current = await self._read_action_state(
                        state_id,
                        probe_timeout=probe_timeout,
                    )
                except Exception as error:
                    probe_error = error
                else:
                    last = current
                    evidence = _confirmed_transition_evidence(before, current)
                    if evidence is not None:
                        elapsed = asyncio.get_running_loop().time() - started
                        logger.info(
                            "Battle transition confirmed selector=%r "
                            "evidence=%s elapsed=%.2fs",
                            selector,
                            evidence,
                            elapsed,
                        )
                        logger.debug("Battle transition state: %s", current.summary())
                        return
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining > 0:
                    await asyncio.sleep(min(check_interval, remaining))

            try:
                last = await self._read_action_state(
                    state_id,
                    probe_timeout=probe_timeout,
                )
            except Exception as error:
                probe_error = error
            evidence = _confirmed_transition_evidence(before, last)
            if evidence is not None:
                logger.info(
                    "Battle transition confirmed during final reconciliation "
                    "selector=%r evidence=%s",
                    selector,
                    evidence,
                )
                return

            logger.error(
                "Battle transition outcome unknown selector=%r before=(%s) last=(%s)",
                selector,
                before.summary(),
                last.summary(),
            )
            unknown = BattleActionOutcomeUnknownError(
                "Battle transition lacks positive next-phase evidence; "
                f"selector={selector!r}; last_state={last.summary()}"
            )
            cause = click_error or probe_error
            if cause is not None:
                raise unknown from cause
            raise unknown

"""Exactly-once battle actions with authoritative completion evidence."""

import asyncio
import json
import math
import re
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from hvbrowser import HVDriver
from hvbrowser.runtime import (
    ElementAction,
    is_browser_generation_error,
    setup_logger,
    wait_for_zendriver,
)

from .contracts import (
    BattleActionKind,
    BattleActionOutcomeUnknownError,
    BattleActionRecoveryEvidence,
    BattleTurnPhase,
)
from .recovery import BattleRecoveryState

logger = setup_logger(__name__)

_STALLED_XHR_MINIMUM_AGE_MS = 5_000.0
_BATTLE_MUTATION_TIMEOUT_SECONDS = 15.0
_SELECTOR_OUTER_TIMEOUT_MARGIN_SECONDS = 2.0


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
            sentAt: null,
            completedAt: null,
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
                    monitor.sentAt = performance.now();
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
                            monitor.completedAt = performance.now();
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
    const requestAgeMs = (
        matchingMonitor
        && matchingMonitor.sentAt !== null
    ) ? Math.max(
        0,
        (matchingMonitor.completedAt ?? performance.now())
            - matchingMonitor.sentAt,
    ) : null;
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
            requestAgeMs,
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

_CLEAR_PAGE_ACTION_STATE_JS = """
(() => {
    const monitor = globalThis.__hvbattleActionMonitor;
    if (monitor && typeof monitor.restore === "function") monitor.restore();
    delete globalThis.__hvbattleActionMonitor;
    return globalThis.__hvbattleDocumentId || null;
})()
"""


# Final completion is neither a combat XHR nor a next-floor transition. Its
# dedicated receipt returns only realm/DOM markers, never the current URL.
_BATTLE_EXIT_STATE_JS = r"""
(() => {
    const makeId = () =>
        `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

    if (!globalThis.__hvbattleDocumentId) {
        globalThis.__hvbattleDocumentId = makeId();
    }

    let realm = "outside";
    try {
        const url = new URL(window.location.href);
        const trustedOrigin = (
            url.protocol.toLowerCase() === "https:"
            && url.hostname.toLowerCase() === "hentaiverse.org"
            && (url.port === "" || url.port === "443")
            && url.username === ""
            && url.password === ""
        );
        if (trustedOrigin) {
            realm = (
                url.pathname === "/isekai"
                || url.pathname.startsWith("/isekai/")
            ) ? "isekai" : "persistent";
        }
    } catch (_) {
        realm = "outside";
    }

    const completion = document.getElementById("pane_completion");
    return {
        documentId: globalThis.__hvbattleDocumentId,
        realm,
        readyState: document.readyState,
        battlePresent: Boolean(document.getElementById("battle_main")),
        finishImagePresent: Boolean(
            completion
            && completion.querySelector('img[src*="finishbattle.png"]')
        ),
        nextFloorPresent: Boolean(document.getElementById("btcp")),
        ponychartPresent: Boolean(document.getElementById("riddlesubmit")),
    };
})()
"""


@dataclass(frozen=True, slots=True)
class _ActionMonitorState:
    sent: bool
    sent_count: int
    request_age_ms: float | None
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


def _payload_value(raw: Mapping[object, object], key: str, *, context: str) -> object:
    if key not in raw:
        raise RuntimeError(f"Invalid {context}: missing {key}")
    return raw[key]


def _payload_bool(raw: Mapping[object, object], key: str, *, context: str) -> bool:
    value = _payload_value(raw, key, context=context)
    if not isinstance(value, bool):
        raise RuntimeError(f"Invalid {context}: {key} must be boolean")
    return value


def _payload_count(raw: Mapping[object, object], key: str, *, context: str) -> int:
    value = _payload_value(raw, key, context=context)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"Invalid {context}: {key} must be a non-negative integer")
    return value


def _payload_optional_nonnegative_number(
    raw: Mapping[object, object], key: str, *, context: str
) -> float | None:
    value = _payload_value(raw, key, context=context)
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise RuntimeError(
            f"Invalid {context}: {key} must be a non-negative finite number or null"
        )
    return float(value)


def _payload_optional_bool(
    raw: Mapping[object, object], key: str, *, context: str
) -> bool | None:
    value = _payload_value(raw, key, context=context)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise RuntimeError(f"Invalid {context}: {key} must be boolean or null")
    return value


def _payload_optional_string(
    raw: Mapping[object, object],
    key: str,
    *,
    context: str,
    require_nonempty: bool = False,
) -> str | None:
    value = _payload_value(raw, key, context=context)
    if value is None:
        return None
    if not isinstance(value, str) or (require_nonempty and not value):
        qualifier = "a non-empty string" if require_nonempty else "a string"
        raise RuntimeError(f"Invalid {context}: {key} must be {qualifier} or null")
    return value


def _parse_action_monitor(raw: Mapping[object, object]) -> _ActionMonitorState:
    context = "battle action monitor payload"
    status_raw = _payload_value(raw, "status", context=context)
    if status_raw is not None and (
        not isinstance(status_raw, int)
        or isinstance(status_raw, bool)
        or status_raw < 0
    ):
        raise RuntimeError(f"Invalid {context}: status must be a non-negative integer")

    outcome_raw = _payload_value(raw, "outcome", context=context)
    if outcome_raw is not None and (
        not isinstance(outcome_raw, str)
        or outcome_raw not in {"load", "error", "abort", "timeout"}
    ):
        raise RuntimeError(f"Invalid {context}: unknown outcome")

    return _ActionMonitorState(
        sent=_payload_bool(raw, "sent", context=context),
        sent_count=_payload_count(raw, "sentCount", context=context),
        request_age_ms=_payload_optional_nonnegative_number(
            raw, "requestAgeMs", context=context
        ),
        completed=_payload_bool(raw, "completed", context=context),
        status=status_raw,
        outcome=outcome_raw,
        log_mutations=_payload_count(raw, "logMutations", context=context),
        response_parse_ok=_payload_optional_bool(
            raw, "responseParseOk", context=context
        ),
        response_has_textlog=_payload_bool(raw, "responseHasTextlog", context=context),
        response_has_pane_completion=_payload_bool(
            raw, "responseHasPaneCompletion", context=context
        ),
        response_has_error=_payload_bool(raw, "responseHasError", context=context),
        response_has_reload=_payload_bool(raw, "responseHasReload", context=context),
        response_has_login=_payload_bool(raw, "responseHasLogin", context=context),
    )


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

        context = "battle action state payload"
        document_id = _payload_value(raw, "documentId", context=context)
        if (
            not isinstance(document_id, str)
            or not document_id
            or document_id == "unknown"
        ):
            raise RuntimeError(f"Invalid {context}: documentId must be non-empty")

        battle_node_id = _payload_optional_string(
            raw,
            "battleNodeId",
            context=context,
            require_nonempty=True,
        )
        if battle_node_id == "unknown":
            raise RuntimeError(f"Invalid {context}: reserved battleNodeId")

        ready_state = _payload_value(raw, "readyState", context=context)
        if not isinstance(ready_state, str) or ready_state not in {
            "loading",
            "interactive",
            "complete",
        }:
            raise RuntimeError(f"Invalid {context}: unknown readyState")

        monitor_raw = _payload_value(raw, "monitor", context=context)
        if monitor_raw is None:
            monitor = None
        elif isinstance(monitor_raw, Mapping):
            monitor = _parse_action_monitor(monitor_raw)
        else:
            raise RuntimeError(f"Invalid {context}: monitor must be an object or null")

        return cls(
            document_id=document_id,
            battle_node_id=battle_node_id,
            ready_state=ready_state,
            battle_present=_payload_bool(raw, "battlePresent", context=context),
            log_revision=_payload_optional_string(raw, "logRevision", context=context),
            log_rows=_payload_count(raw, "logRows", context=context),
            latest_log=_payload_optional_string(raw, "latestLog", context=context),
            round_text=_payload_optional_string(raw, "roundText", context=context),
            completion_present=_payload_bool(raw, "completionPresent", context=context),
            battle_complete_present=_payload_bool(
                raw, "battleCompletePresent", context=context
            ),
            finish_image_present=_payload_bool(
                raw, "finishImagePresent", context=context
            ),
            completion_revision=_payload_optional_string(
                raw, "completionRevision", context=context
            ),
            next_floor_present=_payload_bool(raw, "nextFloorPresent", context=context),
            ponychart_present=_payload_bool(raw, "ponychartPresent", context=context),
            action_controls=_payload_count(raw, "actionControls", context=context),
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
                f"request_age_ms={self.monitor.request_age_ms},"
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


@dataclass(frozen=True, slots=True)
class _BattleExitState:
    document_id: str
    realm: str
    ready_state: str
    battle_present: bool
    finish_image_present: bool
    next_floor_present: bool
    ponychart_present: bool

    @classmethod
    def from_raw(cls, raw: object) -> _BattleExitState:
        if not isinstance(raw, Mapping):
            raise RuntimeError("Invalid battle exit state payload")

        def strict_bool(key: str) -> bool:
            value = raw.get(key)
            if value is True:
                return True
            if value is False:
                return False
            raise RuntimeError("Invalid battle exit marker payload")

        document_id = raw.get("documentId")
        realm = raw.get("realm")
        ready_state = raw.get("readyState")
        if (
            not isinstance(document_id, str)
            or not document_id
            or document_id == "unknown"
        ):
            raise RuntimeError("Invalid battle exit document identity")
        if not isinstance(realm, str) or realm not in {
            "persistent",
            "isekai",
            "outside",
        }:
            raise RuntimeError("Invalid battle exit realm")
        if not isinstance(ready_state, str) or ready_state not in {
            "loading",
            "interactive",
            "complete",
        }:
            raise RuntimeError("Invalid battle exit document readiness")
        return cls(
            document_id=document_id,
            realm=str(realm),
            ready_state=str(ready_state),
            battle_present=strict_bool("battlePresent"),
            finish_image_present=strict_bool("finishImagePresent"),
            next_floor_present=strict_bool("nextFloorPresent"),
            ponychart_present=strict_bool("ponychartPresent"),
        )

    def summary(self) -> str:
        return (
            f"doc={self.document_id[:12]},realm={self.realm},"
            f"ready={self.ready_state},battle={int(self.battle_present)},"
            f"finish_image={int(self.finish_image_present)},"
            f"next_floor={int(self.next_floor_present)},"
            f"ponychart={int(self.ponychart_present)}"
        )


def _reconcile_recovery_state(
    action: _BattleActionState,
    exit_state: _BattleExitState,
) -> BattleRecoveryState | None:
    """Reject a reload that raced between the two bounded DOM probes."""
    if (
        action.document_id == "unknown"
        or action.document_id != exit_state.document_id
        or action.ready_state != exit_state.ready_state
        or action.battle_present != exit_state.battle_present
        or action.finish_image_present != exit_state.finish_image_present
        or action.next_floor_present != exit_state.next_floor_present
        or action.ponychart_present != exit_state.ponychart_present
    ):
        return None

    phase: BattleTurnPhase | None = None
    if action.battle_present and action.finish_image_present:
        phase = BattleTurnPhase.COMPLETE
    elif action.battle_present and action.next_floor_present:
        phase = BattleTurnPhase.NEXT_FLOOR
    elif action.ponychart_present:
        # PonyChart may temporarily replace the battle container, so its exact
        # submit control is authoritative without ``battle_main``.
        phase = BattleTurnPhase.CHALLENGE
    elif (
        action.battle_present
        and action.log_revision is not None
        and action.action_controls > 0
    ):
        phase = BattleTurnPhase.ACTIVE

    return BattleRecoveryState(
        document_id=action.document_id,
        realm=exit_state.realm,
        ready_state=action.ready_state,
        phase=phase,
        log_revision=action.log_revision,
        completion_revision=action.completion_revision,
        action_controls=action.action_controls,
    )


def _normal_action_response(monitor: _ActionMonitorState | None) -> bool:
    return bool(
        monitor is not None
        and type(monitor.sent) is bool
        and monitor.sent
        and type(monitor.sent_count) is int
        and monitor.sent_count == 1
        and type(monitor.completed) is bool
        and monitor.completed
        and type(monitor.status) is int
        and monitor.status == 200
        and monitor.outcome == "load"
        and type(monitor.log_mutations) is int
        and monitor.log_mutations >= 0
        and monitor.response_parse_ok is True
        and type(monitor.response_has_textlog) is bool
        and type(monitor.response_has_pane_completion) is bool
        and monitor.response_has_error is False
        and monitor.response_has_reload is False
        and monitor.response_has_login is False
    )


def _transition_receipt_has_at_most_one_dispatch(
    monitor: _ActionMonitorState | None,
) -> bool:
    """Reject known duplicate XHRs even when transition DOM already advanced."""
    return bool(
        monitor is None
        or (
            type(monitor.sent) is bool
            and type(monitor.sent_count) is int
            and (
                (not monitor.sent and monitor.sent_count == 0)
                or (monitor.sent and monitor.sent_count == 1)
            )
        )
    )


def _action_recovery_evidence(
    *,
    action_id: str,
    action_kind: BattleActionKind,
    selector: str,
    click_started: bool,
    pre_click_document_id: str,
    post_click_document_id: str,
    dialog_category: str | None,
    monitor: _ActionMonitorState | None,
) -> BattleActionRecoveryEvidence:
    """Freeze the best matching receipt seen before a navigation hid it."""
    return BattleActionRecoveryEvidence(
        action_id=action_id,
        action_kind=action_kind,
        selector=selector,
        click_started=click_started,
        xhr_pending_at_least_five_seconds=bool(
            action_kind is BattleActionKind.TURN
            and monitor is not None
            and monitor.completed is False
            and isinstance(monitor.request_age_ms, (int, float))
            and not isinstance(monitor.request_age_ms, bool)
            and math.isfinite(monitor.request_age_ms)
            and monitor.request_age_ms >= _STALLED_XHR_MINIMUM_AGE_MS
        ),
        pre_click_document_id=pre_click_document_id,
        post_click_document_id=post_click_document_id,
        dialog_action_id=action_id if dialog_category is not None else None,
        dialog_category=dialog_category,
        xhr_sent=monitor.sent if monitor is not None else False,
        xhr_sent_count=monitor.sent_count if monitor is not None else 0,
        xhr_completed=monitor.completed if monitor is not None else False,
        xhr_status=monitor.status if monitor is not None else None,
        xhr_outcome=monitor.outcome if monitor is not None else None,
    )


def _error_type_name(error: BaseException | None) -> str:
    return type(error).__name__ if error is not None else "none"


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
    generation_ready = current.ready_state in {"interactive", "complete"}
    if round_advanced and actionable_battle:
        if generation_changed and generation_ready:
            return "battle-generation+round-advanced"
        if not generation_changed:
            return "battle-round-advanced"
    if round_initialized and actionable_battle:
        if generation_changed and generation_ready:
            return "battle-generation+round-initialized"
        if (
            not generation_changed
            and current.log_revision is not None
            and current.log_revision != before.log_revision
        ):
            return "battle-round-initialized"
    return None


def _expected_realm(expected_is_isekai: bool) -> str:
    if not isinstance(expected_is_isekai, bool):
        raise TypeError("expected_is_isekai must be bool")
    return "isekai" if expected_is_isekai else "persistent"


def _confirmed_battle_exit_evidence(
    expected_is_isekai: bool,
    before: _BattleExitState,
    current: _BattleExitState,
) -> str | None:
    """Return evidence only for a new, ready, same-realm landing page."""
    if (
        current.document_id != before.document_id
        and current.realm == _expected_realm(expected_is_isekai)
        and current.ready_state in {"interactive", "complete"}
        and not current.battle_present
        and not current.finish_image_present
        and not current.next_floor_present
        and not current.ponychart_present
    ):
        return "new-document+same-realm-ready+battle-controls-absent"
    return None


def _final_completion_control_ready(
    expected_is_isekai: bool, current: _BattleExitState
) -> bool:
    """Require the exact final control in a ready battle on the same realm."""
    return bool(
        current.realm == _expected_realm(expected_is_isekai)
        and current.ready_state in {"interactive", "complete"}
        and current.battle_present
        and current.finish_image_present
    )


class ElementActionManager:
    def __init__(
        self,
        driver: HVDriver,
        *,
        begin_dialog_observation: Callable[[str], None] | None = None,
        get_dialog_category: Callable[[str], str | None] | None = None,
    ) -> None:
        self.hvdriver = driver
        self._action = ElementAction(lambda: driver.page)
        self._action_lock = asyncio.Lock()
        self._begin_dialog_observation = begin_dialog_observation
        self._get_dialog_category = get_dialog_category

    @property
    def page(self) -> Any:
        return self.hvdriver.page

    async def _click(self, element: Any) -> None:
        await self._action.click(
            element,
            operation_timeout=_BATTLE_MUTATION_TIMEOUT_SECONDS,
        )

    def _begin_submitted_action(self, action_id: str) -> None:
        begin = getattr(self, "_begin_dialog_observation", None)
        if begin is not None:
            begin(action_id)

    def _submitted_action_dialog_category(self, action_id: str) -> str | None:
        get_category = getattr(self, "_get_dialog_category", None)
        if get_category is None:
            return None
        category = get_category(action_id)
        return category if isinstance(category, str) else None

    async def click_resilient(
        self,
        get_element: Callable[[], Coroutine[Any, Any, Any]],
        retries: int = 3,
        delay: float = 0.1,
    ) -> None:
        """Click a local UI control with retries; never use for a turn action."""
        await self._action.click_resilient(
            get_element,
            retries=retries,
            delay=delay,
            operation_timeout=_BATTLE_MUTATION_TIMEOUT_SECONDS,
        )

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
            operation_timeout=_BATTLE_MUTATION_TIMEOUT_SECONDS,
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
            selector,
            retries=retries,
            wait_timeout=wait_timeout,
            delay=delay,
            operation_timeout=_BATTLE_MUTATION_TIMEOUT_SECONDS,
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
        raw = await wait_for_zendriver(
            self.page.evaluate(self._state_script(monitor_id, arm_monitor=arm_monitor)),
            timeout=probe_timeout,
            owner=self.page,
        )
        return _BattleActionState.from_raw(raw)

    async def _read_battle_exit_state(
        self, *, probe_timeout: float = 3.0
    ) -> _BattleExitState:
        raw = await wait_for_zendriver(
            self.page.evaluate(_BATTLE_EXIT_STATE_JS),
            timeout=probe_timeout,
            owner=self.page,
        )
        return _BattleExitState.from_raw(raw)

    async def read_recovery_state(
        self, *, probe_timeout: float = 3.0
    ) -> BattleRecoveryState | None:
        """Read a reload state twice and reject cross-document DOM races."""
        deadline = asyncio.get_running_loop().time() + probe_timeout
        state_id = uuid4().hex
        action = await self._read_action_state(
            state_id,
            probe_timeout=probe_timeout,
        )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Battle recovery probe budget was exhausted")
        exit_state = await self._read_battle_exit_state(probe_timeout=remaining)
        return _reconcile_recovery_state(action, exit_state)

    async def reload_current_page(self, *, operation_timeout: float) -> None:
        """Reload the tab's current URL once without creating a browser session."""
        await wait_for_zendriver(
            self.page.reload(),
            timeout=operation_timeout,
            owner=self.page,
        )

    async def clear_page_action_state(
        self, *, probe_timeout: float = 3.0
    ) -> str | None:
        """Discard action hooks that belong to the newly accepted document."""
        document_id = await wait_for_zendriver(
            self.page.evaluate(_CLEAR_PAGE_ACTION_STATE_JS),
            timeout=probe_timeout,
            owner=self.page,
        )
        return document_id if isinstance(document_id, str) and document_id else None

    async def _cleanup_action_monitor(
        self, monitor_id: str, *, probe_timeout: float
    ) -> None:
        script = _CLEANUP_ACTION_MONITOR_JS.replace(
            "__MONITOR_ID__", json.dumps(monitor_id)
        )
        try:
            await wait_for_zendriver(
                self.page.evaluate(script),
                timeout=probe_timeout,
                owner=self.page,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
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
                return await wait_for_zendriver(
                    self.page.select(selector, timeout=wait_timeout),
                    timeout=wait_timeout + _SELECTOR_OUTER_TIMEOUT_MARGIN_SECONDS,
                    owner=self.page,
                )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
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
            if is_browser_generation_error(error):
                raise
            return fallback, error

    async def _final_battle_exit_probe(
        self,
        *,
        probe_timeout: float,
        fallback: _BattleExitState,
    ) -> tuple[_BattleExitState, Exception | None]:
        try:
            return (
                await self._read_battle_exit_state(probe_timeout=probe_timeout),
                None,
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
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
            monitor_cleanup_safe = True
            try:
                try:
                    before = await self._read_action_state(
                        monitor_id,
                        arm_monitor=True,
                        probe_timeout=probe_timeout,
                    )
                except Exception as error:
                    if is_browser_generation_error(error):
                        monitor_cleanup_safe = False
                    raise
                last = before
                click_started = False
                click_error: Exception | None = None
                probe_error: Exception | None = None
                post_click_probe_succeeded = False
                probe_timed_out = False
                request_deadline_bound = False
                receipt_monitor: _ActionMonitorState | None = None
                started = asyncio.get_running_loop().time()
                self._begin_submitted_action(monitor_id)
                click_started = True
                try:
                    await self._click(element)
                except Exception as error:
                    if is_browser_generation_error(error):
                        # A generation failure makes the click outcome
                        # unobservable.  Never issue a follow-up probe or
                        # cleanup transaction on that generation.
                        monitor_cleanup_safe = False
                        raise
                    click_error = error

                # A slow CDP click must not consume the XHR's receipt window.
                # Rebind this provisional post-click deadline to the browser's
                # first observed XMLHttpRequest.send() timestamp below.
                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        current = await self._read_action_state(
                            monitor_id,
                            # Keep one CDP probe in flight for at most the
                            # remaining action deadline.  Short per-probe
                            # cancellation corrupts Zendriver's response mapper.
                            probe_timeout=remaining,
                        )
                    except Exception as error:
                        if is_browser_generation_error(error):
                            monitor_cleanup_safe = False
                            raise
                        probe_error = error
                        if isinstance(error, TimeoutError):
                            probe_timed_out = True
                            break
                    else:
                        last = current
                        if current.monitor is not None:
                            receipt_monitor = current.monitor
                            if (
                                not request_deadline_bound
                                and current.monitor.sent
                                and current.monitor.request_age_ms is not None
                            ):
                                remaining_request_time = max(
                                    0.0,
                                    timeout - current.monitor.request_age_ms / 1_000.0,
                                )
                                deadline = (
                                    asyncio.get_running_loop().time()
                                    + remaining_request_time
                                )
                                request_deadline_bound = True
                        post_click_probe_succeeded = True
                        evidence = _confirmed_action_evidence(before, current)
                        if evidence is not None:
                            elapsed = asyncio.get_running_loop().time() - started
                            status = current.monitor.status if current.monitor else None
                            if click_error is None and probe_error is None:
                                logger.debug(
                                    "Battle action confirmed selector=%r "
                                    "evidence=%s elapsed=%.2fs xhr_status=%s",
                                    selector,
                                    evidence,
                                    elapsed,
                                    status,
                                )
                            else:
                                logger.warning(
                                    "Battle action confirmed after transient error "
                                    "selector=%r evidence=%s elapsed=%.2fs "
                                    "xhr_status=%s click_error=%s probe_error=%s",
                                    selector,
                                    evidence,
                                    elapsed,
                                    status,
                                    _error_type_name(click_error),
                                    _error_type_name(probe_error),
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

                if not probe_timed_out:
                    try:
                        last, final_error = await self._final_action_probe(
                            monitor_id,
                            probe_timeout=probe_timeout,
                            fallback=last,
                        )
                    except Exception as error:
                        if is_browser_generation_error(error):
                            monitor_cleanup_safe = False
                        raise
                    if final_error is not None:
                        probe_error = final_error
                        if isinstance(final_error, TimeoutError):
                            probe_timed_out = True
                            monitor_cleanup_safe = False
                    else:
                        post_click_probe_succeeded = True
                    if last.monitor is not None:
                        receipt_monitor = last.monitor
                evidence = _confirmed_action_evidence(before, last)
                if evidence is not None:
                    elapsed = asyncio.get_running_loop().time() - started
                    status = last.monitor.status if last.monitor else None
                    logger.warning(
                        "Battle action confirmed during final reconciliation "
                        "selector=%r evidence=%s elapsed=%.2fs xhr_status=%s "
                        "click_error=%s probe_error=%s",
                        selector,
                        evidence,
                        elapsed,
                        status,
                        _error_type_name(click_error),
                        _error_type_name(probe_error),
                    )
                    return

                if (
                    click_started
                    and click_error is None
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
                dialog_category = self._submitted_action_dialog_category(monitor_id)
                unknown = BattleActionOutcomeUnknownError(
                    "Submitted battle action lacks an authoritative receipt; "
                    f"selector={selector!r}; last_state={last.summary()}",
                    recovery_evidence=_action_recovery_evidence(
                        action_id=monitor_id,
                        action_kind=BattleActionKind.TURN,
                        selector=selector,
                        click_started=click_started,
                        pre_click_document_id=before.document_id,
                        post_click_document_id=(
                            "unknown"
                            if probe_timed_out or not post_click_probe_succeeded
                            else last.document_id
                        ),
                        dialog_category=dialog_category,
                        monitor=receipt_monitor,
                    ),
                )
                # A timed-out cleanup would leave a non-cancelled CDP command
                # live just before recovery probes begin.  Unknown paths leave
                # the hook for the accepted reload or browser close to discard.
                monitor_cleanup_safe = False
                cause = click_error or probe_error
                if cause is not None:
                    raise unknown from cause
                raise unknown
            except asyncio.CancelledError:
                monitor_cleanup_safe = False
                raise
            finally:
                if monitor_cleanup_safe:
                    await self._cleanup_action_monitor(
                        monitor_id,
                        probe_timeout=probe_timeout,
                    )

    async def click_and_wait_transition_locator(
        self,
        selector: str,
        stale_retries: int = 3,
        timeout: float = 60.0,
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
            monitor_cleanup_safe = True
            try:
                try:
                    before = await self._read_action_state(
                        state_id,
                        arm_monitor=True,
                        probe_timeout=probe_timeout,
                    )
                except Exception as error:
                    if is_browser_generation_error(error):
                        monitor_cleanup_safe = False
                    raise
                started = asyncio.get_running_loop().time()
                click_error: Exception | None = None
                probe_error: Exception | None = None
                probe_timed_out = False
                post_click_probe_succeeded = False
                last = before
                receipt_monitor = before.monitor
                self._begin_submitted_action(state_id)
                try:
                    await self._click(element)
                except Exception as error:
                    if is_browser_generation_error(error):
                        monitor_cleanup_safe = False
                        raise
                    click_error = error

                # A slow CDP click must not consume the transition observation
                # window used to reconcile its possibly submitted result.
                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        current = await self._read_action_state(
                            state_id,
                            # A navigation-time probe may be delayed well beyond
                            # the normal three-second read bound.  Await that one
                            # command through the transition deadline instead of
                            # stacking cancellable probes every few seconds.
                            probe_timeout=remaining,
                        )
                    except Exception as error:
                        if is_browser_generation_error(error):
                            monitor_cleanup_safe = False
                            raise
                        probe_error = error
                        if isinstance(error, TimeoutError):
                            probe_timed_out = True
                            break
                    else:
                        last = current
                        post_click_probe_succeeded = True
                        if current.monitor is not None:
                            receipt_monitor = current.monitor
                        evidence = _confirmed_transition_evidence(before, current)
                        if evidence is not None and (
                            _transition_receipt_has_at_most_one_dispatch(
                                receipt_monitor
                            )
                        ):
                            elapsed = asyncio.get_running_loop().time() - started
                            if click_error is None and probe_error is None:
                                logger.debug(
                                    "Battle transition confirmed selector=%r "
                                    "evidence=%s elapsed=%.2fs",
                                    selector,
                                    evidence,
                                    elapsed,
                                )
                            else:
                                logger.warning(
                                    "Battle transition confirmed after transient "
                                    "error selector=%r evidence=%s elapsed=%.2fs "
                                    "click_error=%s probe_error=%s",
                                    selector,
                                    evidence,
                                    elapsed,
                                    _error_type_name(click_error),
                                    _error_type_name(probe_error),
                                )
                            logger.debug(
                                "Battle transition state: %s", current.summary()
                            )
                            return
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining > 0:
                        await asyncio.sleep(min(check_interval, remaining))

                if not probe_timed_out:
                    try:
                        last = await self._read_action_state(
                            state_id,
                            probe_timeout=probe_timeout,
                        )
                    except Exception as error:
                        if is_browser_generation_error(error):
                            monitor_cleanup_safe = False
                            raise
                        probe_error = error
                        if isinstance(error, TimeoutError):
                            probe_timed_out = True
                    else:
                        post_click_probe_succeeded = True
                        if last.monitor is not None:
                            receipt_monitor = last.monitor
                evidence = _confirmed_transition_evidence(before, last)
                if evidence is not None and (
                    _transition_receipt_has_at_most_one_dispatch(receipt_monitor)
                ):
                    logger.warning(
                        "Battle transition confirmed during final reconciliation "
                        "selector=%r evidence=%s click_error=%s probe_error=%s",
                        selector,
                        evidence,
                        _error_type_name(click_error),
                        _error_type_name(probe_error),
                    )
                    return

                logger.error(
                    "Battle transition outcome unknown selector=%r before=(%s) "
                    "last=(%s)",
                    selector,
                    before.summary(),
                    last.summary(),
                )
                dialog_category = self._submitted_action_dialog_category(state_id)
                unknown = BattleActionOutcomeUnknownError(
                    "Battle transition lacks positive next-phase evidence; "
                    f"selector={selector!r}; last_state={last.summary()}",
                    recovery_evidence=_action_recovery_evidence(
                        action_id=state_id,
                        action_kind=BattleActionKind.NEXT_FLOOR,
                        selector=selector,
                        click_started=True,
                        pre_click_document_id=before.document_id,
                        post_click_document_id=(
                            "unknown"
                            if probe_timed_out or not post_click_probe_succeeded
                            else last.document_id
                        ),
                        dialog_category=dialog_category,
                        monitor=receipt_monitor,
                    ),
                )
                # See the TURN path above: do not risk stacking recovery on a
                # cleanup transaction whose timeout cannot cancel Zendriver.
                monitor_cleanup_safe = False
                cause = click_error or probe_error
                if cause is not None:
                    raise unknown from cause
                raise unknown
            except asyncio.CancelledError:
                monitor_cleanup_safe = False
                raise
            finally:
                if monitor_cleanup_safe:
                    await self._cleanup_action_monitor(
                        state_id,
                        probe_timeout=probe_timeout,
                    )

    async def click_and_wait_battle_exit_locator(
        self,
        selector: str,
        *,
        expected_is_isekai: bool,
        stale_retries: int = 3,
        timeout: float = 20.0,
        check_interval: float = 0.25,
        probe_timeout: float = 3.0,
    ) -> None:
        """Click final completion once and require a new same-realm page."""
        _expected_realm(expected_is_isekai)
        if timeout <= 0 or check_interval <= 0 or probe_timeout <= 0:
            raise ValueError("battle exit timeouts must be positive")

        async with self._action_lock:
            try:
                before = await self._read_battle_exit_state(probe_timeout=probe_timeout)
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                raise BattleActionOutcomeUnknownError(
                    "Final battle completion acknowledgement could not establish "
                    "its pre-click state"
                ) from error

            if not _final_completion_control_ready(expected_is_isekai, before):
                logger.error(
                    "Final battle completion acknowledgement precondition unknown "
                    "selector=%r state=(%s)",
                    selector,
                    before.summary(),
                )
                raise BattleActionOutcomeUnknownError(
                    "Final completion control lacks a safe same-realm pre-click "
                    f"state; selector={selector!r}; state={before.summary()}"
                )

            try:
                element = await self._select_for_single_click(
                    selector,
                    retries=stale_retries,
                    wait_timeout=2.0,
                    delay=0.1,
                )
            except Exception as select_error:
                if is_browser_generation_error(select_error):
                    raise
                last, _ = await self._final_battle_exit_probe(
                    probe_timeout=probe_timeout,
                    fallback=before,
                )
                evidence = _confirmed_battle_exit_evidence(
                    expected_is_isekai, before, last
                )
                if evidence is not None:
                    logger.warning(
                        "Final battle completion reconciled before click "
                        "selector=%r evidence=%s select_error_type=%s "
                        "no_click_issued=true state=(%s)",
                        selector,
                        evidence,
                        _error_type_name(select_error),
                        last.summary(),
                    )
                    return
                logger.error(
                    "Final battle completion selector outcome unknown selector=%r "
                    "before=(%s) last=(%s)",
                    selector,
                    before.summary(),
                    last.summary(),
                )
                raise BattleActionOutcomeUnknownError(
                    "Final completion control could not be selected and no "
                    "positive out-of-battle evidence appeared; "
                    f"selector={selector!r}; last_state={last.summary()}"
                ) from select_error

            try:
                selected_state = await self._read_battle_exit_state(
                    probe_timeout=probe_timeout
                )
            except Exception as selection_probe_error:
                if is_browser_generation_error(selection_probe_error):
                    raise
                last, _ = await self._final_battle_exit_probe(
                    probe_timeout=probe_timeout,
                    fallback=before,
                )
                evidence = _confirmed_battle_exit_evidence(
                    expected_is_isekai, before, last
                )
                if evidence is not None:
                    logger.warning(
                        "Final battle completion reconciled after selection "
                        "selector=%r evidence=%s selection_probe_error_type=%s "
                        "no_click_issued=true state=(%s)",
                        selector,
                        evidence,
                        _error_type_name(selection_probe_error),
                        last.summary(),
                    )
                    return
                raise BattleActionOutcomeUnknownError(
                    "Final completion control state could not be revalidated "
                    "before its single click; "
                    f"selector={selector!r}; last_state={last.summary()}"
                ) from selection_probe_error

            evidence = _confirmed_battle_exit_evidence(
                expected_is_isekai, before, selected_state
            )
            if evidence is not None:
                logger.warning(
                    "Final battle completion already exited before click "
                    "selector=%r evidence=%s no_click_issued=true state=(%s)",
                    selector,
                    evidence,
                    selected_state.summary(),
                )
                return
            if (
                selected_state.document_id != before.document_id
                or not _final_completion_control_ready(
                    expected_is_isekai, selected_state
                )
            ):
                logger.error(
                    "Final battle completion control changed before click "
                    "selector=%r before=(%s) selected=(%s)",
                    selector,
                    before.summary(),
                    selected_state.summary(),
                )
                raise BattleActionOutcomeUnknownError(
                    "Final completion control changed after selection; refusing "
                    "to click an unverified document; "
                    f"selector={selector!r}; state={selected_state.summary()}"
                )

            started = asyncio.get_running_loop().time()
            click_error: Exception | None = None
            probe_error: Exception | None = None
            probe_timed_out = False
            last = before
            try:
                await self._click(element)
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                click_error = error

            deadline = started + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    current = await self._read_battle_exit_state(
                        probe_timeout=remaining
                    )
                except Exception as error:
                    if is_browser_generation_error(error):
                        raise
                    probe_error = error
                    if isinstance(error, TimeoutError):
                        probe_timed_out = True
                        break
                else:
                    last = current
                    evidence = _confirmed_battle_exit_evidence(
                        expected_is_isekai, before, current
                    )
                    if evidence is not None:
                        elapsed = asyncio.get_running_loop().time() - started
                        if click_error is None and probe_error is None:
                            logger.debug(
                                "Final battle completion acknowledged selector=%r "
                                "evidence=%s elapsed=%.2fs",
                                selector,
                                evidence,
                                elapsed,
                            )
                        else:
                            logger.warning(
                                "Final battle completion acknowledged after transient "
                                "error selector=%r evidence=%s elapsed=%.2fs "
                                "click_error=%s probe_error=%s",
                                selector,
                                evidence,
                                elapsed,
                                _error_type_name(click_error),
                                _error_type_name(probe_error),
                            )
                        logger.debug("Final battle exit state: %s", current.summary())
                        return
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining > 0:
                    await asyncio.sleep(min(check_interval, remaining))

            if not probe_timed_out:
                last, final_error = await self._final_battle_exit_probe(
                    probe_timeout=probe_timeout,
                    fallback=last,
                )
                if final_error is not None:
                    probe_error = final_error
            evidence = _confirmed_battle_exit_evidence(expected_is_isekai, before, last)
            if evidence is not None:
                logger.warning(
                    "Final battle completion acknowledged during final "
                    "reconciliation selector=%r evidence=%s click_error=%s "
                    "probe_error=%s state=(%s)",
                    selector,
                    evidence,
                    _error_type_name(click_error),
                    _error_type_name(probe_error),
                    last.summary(),
                )
                return

            logger.error(
                "Final battle completion acknowledgement outcome unknown "
                "selector=%r before=(%s) last=(%s)",
                selector,
                before.summary(),
                last.summary(),
            )
            unknown = BattleActionOutcomeUnknownError(
                "Final battle completion acknowledgement lacks positive "
                "new-document same-realm out-of-battle evidence; "
                f"selector={selector!r}; last_state={last.summary()}"
            )
            cause = click_error or probe_error
            if cause is not None:
                raise unknown from cause
            raise unknown

import asyncio
import json
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock

from hvbattle import BattleActionOutcomeUnknownError
from hvbattle.hv_battle_action_manager import (
    _CLEANUP_ACTION_MONITOR_JS,
    ElementActionManager,
    _ActionMonitorState,
    _BattleActionState,
    _confirmed_action_evidence,
    _confirmed_transition_evidence,
    _normal_action_response,
)

_NODE_ACTION_HOOK_HARNESS = r"""
const fs = require("node:fs");
const scripts = JSON.parse(fs.readFileSync(0, "utf8"));

const originalPayloads = [];
const originalOpenCalls = [];
const observers = [];

class FakeMutationObserver {
    constructor(callback) {
        this.callback = callback;
        this.disconnected = false;
        this.target = null;
        observers.push(this);
    }

    observe(target, options) {
        this.target = target;
        this.options = options;
    }

    disconnect() {
        this.disconnected = true;
    }

    takeRecords() {
        this.takeRecordsCalls = (this.takeRecordsCalls || 0) + 1;
        return [];
    }

    trigger(mutations) {
        if (!this.disconnected) this.callback(mutations);
    }
}

class FakeXMLHttpRequest {
    constructor() {
        this.listeners = new Map();
        this.status = 0;
        this.responseText = "";
    }

    addEventListener(type, callback, options = {}) {
        const entries = this.listeners.get(type) || [];
        entries.push({callback, once: Boolean(options.once)});
        this.listeners.set(type, entries);
    }

    dispatch(type) {
        const entries = [...(this.listeners.get(type) || [])];
        for (const entry of entries) {
            entry.callback.call(this);
            if (entry.once) {
                const current = this.listeners.get(type) || [];
                this.listeners.set(
                    type,
                    current.filter((candidate) => candidate !== entry),
                );
            }
        }
    }
}

const originalSend = function (payload) {
    originalPayloads.push(payload);
};
const originalOpen = function (method, url) {
    originalOpenCalls.push({method, url});
};
FakeXMLHttpRequest.prototype.open = originalOpen;
FakeXMLHttpRequest.prototype.send = originalSend;

const firstLogCell = {textContent: "Initializing arena (Round 1 / 10)"};
const log = {
    innerHTML: "<tbody><tr><td>Round 1</td></tr></tbody>",
    querySelector(selector) {
        return selector === "td" ? firstLogCell : null;
    },
    querySelectorAll(selector) {
        if (selector === "td") return [firstLogCell];
        if (selector === "tr") return [{}];
        return [];
    },
};
const completion = {
    innerHTML: "",
    textContent: "",
    querySelector() {
        return null;
    },
};
const battleMain = {};
const elements = new Map([
    ["battle_main", battleMain],
    ["textlog", log],
    ["pane_completion", completion],
]);

globalThis.MutationObserver = FakeMutationObserver;
globalThis.XMLHttpRequest = FakeXMLHttpRequest;
globalThis.document = {
    baseURI: "https://hentaiverse.org/",
    readyState: "complete",
    getElementById(id) {
        return elements.get(id) || null;
    },
    querySelectorAll(selector) {
        if (selector === '#pane_monster [id^="mkey_"][onclick]') return [{}];
        return [];
    },
};

const armState = eval(scripts.arm);
const firstWrappedSend = FakeXMLHttpRequest.prototype.send;
const firstObserver = observers.at(-1);

const unrelatedType = new FakeXMLHttpRequest();
unrelatedType.open("POST", "/json");
unrelatedType.send(JSON.stringify({
    type: "other", method: "action", token: "x", mode: "attack", target: 1,
    skill: 0,
}));
const unrelatedMethod = new FakeXMLHttpRequest();
unrelatedMethod.open("POST", "/json");
unrelatedMethod.send(JSON.stringify({
    type: "battle", method: "inspect", token: "x", mode: "attack", target: 1,
    skill: 0,
}));
const unrelatedEndpoint = new FakeXMLHttpRequest();
unrelatedEndpoint.open("POST", "/other");
unrelatedEndpoint.send(JSON.stringify({
    type: "battle", method: "action", token: "x", mode: "attack", target: 1,
    skill: 0,
}));
const unrelatedVerb = new FakeXMLHttpRequest();
unrelatedVerb.open("GET", "/json");
unrelatedVerb.send(JSON.stringify({
    type: "battle", method: "action", token: "x", mode: "attack", target: 1,
    skill: 0,
}));
const afterUnrelated = eval(scripts.read);
const wrapperSurvivedUnrelated =
    FakeXMLHttpRequest.prototype.send === firstWrappedSend;
firstObserver.trigger([{
    type: "childList",
    addedNodes: [{}],
    removedNodes: [],
}]);
const preSendMutationCount = eval(scripts.read).monitor.logMutations;

const battleAction = new FakeXMLHttpRequest();
battleAction.open("POST", "/json");
battleAction.send(JSON.stringify({
    type: "battle",
    method: "action",
    token: "test-only-secret",
    mode: "attack",
    target: 1,
    skill: 0,
}));
const wrapperSurvivedMatchedSend =
    FakeXMLHttpRequest.prototype.send === firstWrappedSend;

const duplicateBattleAction = new FakeXMLHttpRequest();
duplicateBattleAction.open("POST", "https://hentaiverse.org/json");
duplicateBattleAction.send(JSON.stringify({
    type: "battle",
    method: "action",
    token: "test-only-secret",
    mode: "attack",
    target: 2,
    skill: 0,
}));

log.innerHTML = "<tbody><tr><td>You hit Monster.</td></tr></tbody>";
firstLogCell.textContent = "You hit Monster.";
firstObserver.trigger([{
    type: "childList",
    addedNodes: [{}],
    removedNodes: [],
}]);
battleAction.status = 200;
battleAction.responseText = JSON.stringify({
    textlog: [{t: "You hit Monster."}],
    pane_monster: "updated",
    error: null,
    reload: null,
    login: null,
});
battleAction.dispatch("load");
battleAction.dispatch("loadend");
const afterBattle = eval(scripts.read);
const submittedAction = JSON.parse(originalPayloads.at(-2));

completion.innerHTML = '<img src="/y/battle/finishbattle.png">';
completion.querySelector = (selector) =>
    selector.includes("finishbattle.png") ? {} : null;
elements.set("btcp", {});
const finalControlState = eval(scripts.read);

const firstCleanupResult = eval(scripts.cleanup);
const firstCleanup = {
    result: firstCleanupResult,
    monitorRemoved: globalThis.__hvbattleActionMonitor === undefined,
    observerDisconnected: firstObserver.disconnected,
    openRestored: FakeXMLHttpRequest.prototype.open === originalOpen,
    sendRestored: FakeXMLHttpRequest.prototype.send === originalSend,
};

eval(scripts.staleArm);
const staleWrappedSend = FakeXMLHttpRequest.prototype.send;
const staleWrappedOpen = FakeXMLHttpRequest.prototype.open;
const staleObserver = observers.at(-1);
const freshArmState = eval(scripts.freshArm);
const freshObserver = observers.at(-1);
const staleRecovery = {
    staleObserverDisconnected: staleObserver.disconnected,
    staleWrapperReplaced:
        FakeXMLHttpRequest.prototype.send !== staleWrappedSend,
    staleOpenWrapperReplaced:
        FakeXMLHttpRequest.prototype.open !== staleWrappedOpen,
    freshMonitorSent: freshArmState.monitor.sent,
};
const freshCleanupResult = eval(scripts.freshCleanup);
const freshCleanup = {
    result: freshCleanupResult,
    observerDisconnected: freshObserver.disconnected,
    openRestored: FakeXMLHttpRequest.prototype.open === originalOpen,
    sendRestored: FakeXMLHttpRequest.prototype.send === originalSend,
};

console.log(JSON.stringify({
    armSent: armState.monitor.sent,
    unrelatedSent: afterUnrelated.monitor.sent,
    wrapperSurvivedUnrelated,
    preSendMutationCount,
    observerTakeRecordsCalls: firstObserver.takeRecordsCalls || 0,
    originalSendCount: originalPayloads.length,
    submittedAction: {
        type: submittedAction.type,
        method: submittedAction.method,
        mode: submittedAction.mode,
        target: submittedAction.target,
    },
    wrapperSurvivedMatchedSend,
    afterBattleMonitor: afterBattle.monitor,
    finalControlState: {
        completionPresent: finalControlState.completionPresent,
        finishImagePresent: finalControlState.finishImagePresent,
        battleCompletePresent: finalControlState.battleCompletePresent,
        nextFloorPresent: finalControlState.nextFloorPresent,
    },
    firstCleanup,
    staleRecovery,
    freshCleanup,
}));
"""


def _monitor(
    *,
    sent: bool = True,
    sent_count: int = 1,
    completed: bool = True,
    status: int | None = 200,
    outcome: str | None = "load",
    log_mutations: int = 1,
    response_parse_ok: bool | None = True,
    response_has_textlog: bool = True,
    response_has_pane_completion: bool = False,
    response_has_error: bool = False,
    response_has_reload: bool = False,
    response_has_login: bool = False,
) -> _ActionMonitorState:
    return _ActionMonitorState(
        sent=sent,
        sent_count=sent_count,
        completed=completed,
        status=status,
        outcome=outcome,
        log_mutations=log_mutations,
        response_parse_ok=response_parse_ok,
        response_has_textlog=response_has_textlog,
        response_has_pane_completion=response_has_pane_completion,
        response_has_error=response_has_error,
        response_has_reload=response_has_reload,
        response_has_login=response_has_login,
    )


def _pending_monitor() -> _ActionMonitorState:
    return _monitor(
        sent=False,
        sent_count=0,
        completed=False,
        status=None,
        outcome=None,
        log_mutations=0,
        response_parse_ok=None,
        response_has_textlog=False,
    )


def _state(
    *,
    document_id: str = "document-1",
    battle_node_id: str | None = "battle-node-1",
    ready_state: str = "complete",
    log_revision: str | None = "log-1",
    latest_log: str | None = "You hit Monster.",
    round_text: str | None = "Initializing arena (Round 1 / 10)",
    completion_present: bool = False,
    battle_complete_present: bool = False,
    finish_image_present: bool = False,
    completion_revision: str | None = "completion-empty",
    next_floor_present: bool = False,
    ponychart_present: bool = False,
    action_controls: int = 1,
    monitor: _ActionMonitorState | None = None,
) -> _BattleActionState:
    return _BattleActionState(
        document_id=document_id,
        battle_node_id=battle_node_id,
        ready_state=ready_state,
        battle_present=True,
        log_revision=log_revision,
        log_rows=10,
        latest_log=latest_log,
        round_text=round_text,
        completion_present=completion_present,
        battle_complete_present=battle_complete_present,
        finish_image_present=finish_image_present,
        completion_revision=completion_revision,
        next_floor_present=next_floor_present,
        ponychart_present=ponychart_present,
        action_controls=action_controls,
        monitor=monitor,
    )


def _manager() -> ElementActionManager:
    manager = object.__new__(ElementActionManager)
    manager._action_lock = asyncio.Lock()
    manager._select_for_single_click = AsyncMock(return_value=object())
    manager._click = AsyncMock()
    manager._cleanup_action_monitor = AsyncMock()
    return manager


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the JS hook test")
class BattleActionJavaScriptHookTests(unittest.TestCase):
    def test_action_xhr_hook_and_cleanup_execute_in_fake_dom(self) -> None:
        monitor_id = "action-monitor"
        stale_monitor_id = "stale-monitor"
        fresh_monitor_id = "fresh-monitor"
        scripts = {
            "arm": ElementActionManager._state_script(
                monitor_id,
                arm_monitor=True,
            ),
            "read": ElementActionManager._state_script(
                monitor_id,
                arm_monitor=False,
            ),
            "cleanup": _CLEANUP_ACTION_MONITOR_JS.replace(
                "__MONITOR_ID__",
                json.dumps(monitor_id),
            ),
            "staleArm": ElementActionManager._state_script(
                stale_monitor_id,
                arm_monitor=True,
            ),
            "freshArm": ElementActionManager._state_script(
                fresh_monitor_id,
                arm_monitor=True,
            ),
            "freshCleanup": _CLEANUP_ACTION_MONITOR_JS.replace(
                "__MONITOR_ID__",
                json.dumps(fresh_monitor_id),
            ),
        }
        completed = subprocess.run(
            [shutil.which("node") or "node", "-e", _NODE_ACTION_HOOK_HARNESS],
            input=json.dumps(scripts),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)

        self.assertFalse(result["armSent"])
        self.assertFalse(result["unrelatedSent"])
        self.assertTrue(result["wrapperSurvivedUnrelated"])
        self.assertEqual(result["preSendMutationCount"], 1)
        self.assertEqual(result["observerTakeRecordsCalls"], 1)
        self.assertEqual(result["originalSendCount"], 6)
        self.assertEqual(
            result["submittedAction"],
            {
                "type": "battle",
                "method": "action",
                "mode": "attack",
                "target": 1,
            },
        )
        self.assertTrue(result["wrapperSurvivedMatchedSend"])

        monitor = result["afterBattleMonitor"]
        self.assertTrue(monitor["sent"])
        self.assertEqual(monitor["sentCount"], 2)
        self.assertTrue(monitor["completed"])
        self.assertEqual(monitor["status"], 200)
        self.assertEqual(monitor["outcome"], "load")
        self.assertEqual(monitor["logMutations"], 1)
        self.assertTrue(monitor["responseParseOk"])
        self.assertTrue(monitor["responseHasTextlog"])
        self.assertFalse(monitor["responseHasError"])
        self.assertFalse(monitor["responseHasReload"])
        self.assertFalse(monitor["responseHasLogin"])
        self.assertEqual(
            result["finalControlState"],
            {
                "completionPresent": True,
                "finishImagePresent": True,
                "battleCompletePresent": True,
                "nextFloorPresent": True,
            },
        )

        self.assertEqual(
            result["firstCleanup"],
            {
                "result": True,
                "monitorRemoved": True,
                "observerDisconnected": True,
                "openRestored": True,
                "sendRestored": True,
            },
        )
        self.assertEqual(
            result["staleRecovery"],
            {
                "staleObserverDisconnected": True,
                "staleWrapperReplaced": True,
                "staleOpenWrapperReplaced": True,
                "freshMonitorSent": False,
            },
        )
        self.assertEqual(
            result["freshCleanup"],
            {
                "result": True,
                "observerDisconnected": True,
                "openRestored": True,
                "sendRestored": True,
            },
        )


class BattleActionEvidenceTests(unittest.TestCase):
    def test_normal_xhr_and_log_revision_confirm_action(self) -> None:
        before = _state(monitor=_pending_monitor())
        current = _state(
            log_revision="log-2",
            monitor=_monitor(log_mutations=0),
        )

        self.assertEqual(
            _confirmed_action_evidence(before, current),
            "xhr-ack+combat-log-revision",
        )

    def test_identical_log_content_still_confirms_child_list_mutation(self) -> None:
        before = _state(monitor=_pending_monitor())
        current = _state(
            log_revision=before.log_revision,
            latest_log=before.latest_log,
            monitor=_monitor(log_mutations=1),
        )

        self.assertEqual(
            _confirmed_action_evidence(before, current),
            "xhr-ack+combat-log-mutation",
        )

    def test_error_reload_login_duplicate_and_non_200_responses_are_rejected(
        self,
    ) -> None:
        before = _state(monitor=_pending_monitor())
        cases = {
            "error": _monitor(response_has_error=True),
            "reload": _monitor(response_has_reload=True),
            "login": _monitor(response_has_login=True),
            "duplicate": _monitor(sent_count=2),
            "other-2xx": _monitor(status=204),
            "server-error": _monitor(status=503),
        }

        for name, monitor in cases.items():
            with self.subTest(name=name):
                current = _state(
                    log_revision="log-changed",
                    monitor=monitor,
                )
                self.assertFalse(_normal_action_response(monitor))
                self.assertIsNone(_confirmed_action_evidence(before, current))

    def test_next_floor_requires_an_advanced_actionable_round(self) -> None:
        before = _state(
            round_text="Initializing arena (Round 1 / 10)",
            next_floor_present=True,
            action_controls=0,
        )
        current = _state(
            document_id="document-2",
            battle_node_id="battle-node-2",
            round_text="Initializing arena (Round 2 / 10)",
            next_floor_present=False,
            action_controls=3,
        )

        self.assertEqual(
            _confirmed_transition_evidence(before, current),
            "battle-generation+round-advanced",
        )

    def test_same_document_ajax_round_advance_confirms_transition(self) -> None:
        before = _state(
            round_text="Initializing arena (Round 1 / 10)",
            next_floor_present=True,
            action_controls=0,
        )
        current = _state(
            round_text="Initializing arena (Round 2 / 10)",
            log_revision="round-2-log",
            next_floor_present=False,
            action_controls=3,
        )

        self.assertEqual(
            _confirmed_transition_evidence(before, current),
            "battle-round-advanced",
        )

    def test_new_document_must_finish_loading_before_transition_confirms(
        self,
    ) -> None:
        before = _state(
            round_text="Initializing arena (Round 1 / 10)",
            next_floor_present=True,
            action_controls=0,
        )
        current = _state(
            document_id="document-2",
            battle_node_id="battle-node-2",
            ready_state="loading",
            round_text="Initializing arena (Round 2 / 10)",
            log_revision="round-2-log",
            next_floor_present=False,
            action_controls=3,
        )

        self.assertIsNone(_confirmed_transition_evidence(before, current))


class BattleActionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_selector_is_resolved_before_monitor_arm_and_single_click(
        self,
    ) -> None:
        manager = _manager()
        before = _state(monitor=_pending_monitor())
        current = _state(monitor=_monitor())
        events: list[str] = []

        async def select(*_args: object, **_kwargs: object) -> object:
            events.append("select")
            return object()

        async def read(
            _monitor_id: str,
            *,
            arm_monitor: bool = False,
            probe_timeout: float = 3,
        ) -> _BattleActionState:
            del probe_timeout
            events.append("arm" if arm_monitor else "probe")
            return before if arm_monitor else current

        async def click(_element: object) -> None:
            events.append("click")

        manager._select_for_single_click = AsyncMock(side_effect=select)
        manager._read_action_state = AsyncMock(side_effect=read)
        manager._click = AsyncMock(side_effect=click)

        await manager.click_and_wait_log_locator("#mkey_1", timeout=1)

        self.assertEqual(events[:3], ["select", "arm", "click"])
        manager._click.assert_awaited_once()

    async def test_monitor_arm_failure_is_cleaned_without_clicking(self) -> None:
        manager = _manager()
        arm_error = TimeoutError("arm probe failed after possible injection")
        manager._read_action_state = AsyncMock(side_effect=arm_error)

        with self.assertRaises(TimeoutError) as raised:
            await manager.click_and_wait_log_locator("#mkey_1", timeout=1)

        self.assertIs(raised.exception, arm_error)
        manager._select_for_single_click.assert_awaited_once()
        manager._click.assert_not_awaited()
        manager._cleanup_action_monitor.assert_awaited_once()

    async def test_normal_xhr_and_log_mutation_complete_without_sleep(self) -> None:
        manager = _manager()
        before = _state(monitor=_pending_monitor())
        current = _state(monitor=_monitor(log_mutations=1))
        manager._read_action_state = AsyncMock(side_effect=[before, current])

        await manager.click_and_wait_log_locator("#mkey_1", timeout=1)

        manager._click.assert_awaited_once()
        manager._cleanup_action_monitor.assert_awaited_once()

    async def test_rejected_xhr_never_accepts_dom_evidence(self) -> None:
        before = _state(monitor=_pending_monitor())
        cases = {
            "error": _monitor(response_has_error=True),
            "reload": _monitor(response_has_reload=True),
            "login": _monitor(response_has_login=True),
            "non-2xx": _monitor(status=503),
        }

        for name, monitor in cases.items():
            with self.subTest(name=name):
                manager = _manager()
                rejected = _state(
                    log_revision="log-changed",
                    monitor=monitor,
                )
                manager._read_action_state = AsyncMock(
                    side_effect=[before, rejected, rejected]
                )

                with self.assertRaises(BattleActionOutcomeUnknownError):
                    await manager.click_and_wait_log_locator("#mkey_1", timeout=1)

                manager._click.assert_awaited_once()
                manager._cleanup_action_monitor.assert_awaited_once()

    async def test_no_dispatch_raises_retryable_timeout(self) -> None:
        manager = _manager()
        before = _state(monitor=_pending_monitor())

        async def read_state(*_args: object, **_kwargs: object) -> _BattleActionState:
            return before

        manager._read_action_state = AsyncMock(side_effect=read_state)

        with self.assertRaisesRegex(TimeoutError, "was not dispatched"):
            await manager.click_and_wait_log_locator(
                "#mkey_1",
                timeout=1e-9,
                check_interval=1e-9,
            )

        manager._click.assert_awaited_once()
        manager._cleanup_action_monitor.assert_awaited_once()

    async def test_sent_action_without_commit_is_unknown(self) -> None:
        manager = _manager()
        before = _state(monitor=_pending_monitor())
        sent_without_commit = _state(
            monitor=_monitor(log_mutations=0),
        )

        async def read_state(
            _monitor_id: str,
            *,
            arm_monitor: bool = False,
            probe_timeout: float = 3,
        ) -> _BattleActionState:
            del probe_timeout
            return before if arm_monitor else sent_without_commit

        manager._read_action_state = AsyncMock(side_effect=read_state)

        with self.assertRaises(BattleActionOutcomeUnknownError):
            await manager.click_and_wait_log_locator(
                "#mkey_1",
                timeout=1e-9,
                check_interval=1e-9,
            )

        manager._click.assert_awaited_once()
        manager._cleanup_action_monitor.assert_awaited_once()

    async def test_click_exception_is_never_retried(self) -> None:
        manager = _manager()
        click_error = RuntimeError("execution context destroyed after dispatch")
        manager._click = AsyncMock(side_effect=click_error)
        before = _state(monitor=_pending_monitor())
        sent_without_commit = _state(
            monitor=_monitor(log_mutations=0),
        )

        async def read_state(
            _monitor_id: str,
            *,
            arm_monitor: bool = False,
            probe_timeout: float = 3,
        ) -> _BattleActionState:
            del probe_timeout
            return before if arm_monitor else sent_without_commit

        manager._read_action_state = AsyncMock(side_effect=read_state)

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await manager.click_and_wait_log_locator(
                "#mkey_1",
                timeout=1e-9,
                check_interval=1e-9,
            )

        self.assertIs(raised.exception.__cause__, click_error)
        manager._click.assert_awaited_once()
        manager._select_for_single_click.assert_awaited_once()

    async def test_click_exception_without_post_click_probe_is_unknown(self) -> None:
        manager = _manager()
        click_error = RuntimeError("connection lost after click started")
        probe_error = TimeoutError("state probe unavailable")
        manager._click = AsyncMock(side_effect=click_error)
        before = _state(monitor=_pending_monitor())
        manager._read_action_state = AsyncMock(
            side_effect=[before, probe_error, probe_error]
        )

        with self.assertRaises(BattleActionOutcomeUnknownError) as raised:
            await manager.click_and_wait_log_locator(
                "#mkey_1",
                timeout=1e-9,
                check_interval=1e-9,
            )

        self.assertIs(raised.exception.__cause__, click_error)
        manager._click.assert_awaited_once()
        manager._select_for_single_click.assert_awaited_once()

    async def test_final_reconciliation_accepts_late_battle_completion(self) -> None:
        manager = _manager()
        before = _state(monitor=_pending_monitor())
        final = _state(
            completion_present=True,
            battle_complete_present=True,
            finish_image_present=True,
            completion_revision="completion-finished",
            action_controls=0,
            monitor=_monitor(
                log_mutations=0,
                response_has_textlog=False,
                response_has_pane_completion=True,
            ),
        )
        manager._read_action_state = AsyncMock(return_value=before)
        manager._final_action_probe = AsyncMock(return_value=(final, None))

        await manager.click_and_wait_log_locator(
            "#mkey_1",
            timeout=1e-9,
            check_interval=1e-9,
        )

        manager._click.assert_awaited_once()
        manager._final_action_probe.assert_awaited_once()
        manager._cleanup_action_monitor.assert_awaited_once()

    async def test_next_floor_click_waits_for_advanced_round(self) -> None:
        manager = _manager()
        before = _state(
            round_text="Initializing arena (Round 1 / 10)",
            next_floor_present=True,
            action_controls=0,
        )
        current = _state(
            document_id="document-2",
            battle_node_id="battle-node-2",
            round_text="Initializing arena (Round 2 / 10)",
            next_floor_present=False,
            action_controls=3,
        )
        manager._read_action_state = AsyncMock(side_effect=[before, current])

        await manager.click_and_wait_transition_locator("#btcp", timeout=1)

        manager._click.assert_awaited_once()
        manager._select_for_single_click.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

import asyncio
import base64
import binascii
import json
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, Mock, call, patch

from hvbrowser.runtime import ZendriverOperationTimeout, wait_for_zendriver
from zendriver import cdp

import hvbattle.hv_battle_ponychart as ponychart_module
from hvbattle._timing import SemanticDeadline
from hvbattle.contracts import BattleInterruptedError, PonyChartResolutionOutcome
from hvbattle.hv_battle_ponychart import PonyChart, PonyChartResolutionError


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)
    )


def _png_bytes(width: int, height: int) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00\x00\x00\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )


def _canvas_payload(image: bytes, width: int, height: int) -> dict[str, object]:
    encoded = base64.b64encode(image).decode("ascii")
    return {
        "status": "ok",
        "width": width,
        "height": height,
        "dataUrl": f"data:image/png;base64,{encoded}",
    }


def _image_receipt(
    source: str = "challenge",
    width: float = 640,
    height: float = 480,
) -> object:
    return ponychart_module._PonyChartImageState(
        True,
        source,
        width,
        height,
        width,
        height,
    )


def _receipt_context(
    *,
    deadline: SemanticDeadline | None = None,
    expiration_deadline: SemanticDeadline | None = None,
) -> object:
    active_deadline = deadline or SemanticDeadline.after(15.0)
    return ponychart_module._PonyChartReceiptContext(
        "monitor",
        "https://hentaiverse.org/battle",
        "https://hentaiverse.org",
        active_deadline,
        expiration_deadline or active_deadline,
    )


def _page_diagnostic(**overrides: object) -> dict[str, object]:
    diagnostic: dict[str, object] = {
        "readyState": "complete",
        "labelCount": 6,
        "controlCount": 6,
        "checkedCount": 2,
        "submitTag": "input",
        "submitType": "submit",
        "submitSource": "riddlesubmit",
        "submitConnected": True,
        "submitCaptionMatches": True,
        "submitDisabled": False,
        "submitAriaDisabled": False,
        "formAssociated": True,
        "labelScope": "riddler1",
        "riddleMasterPresent": True,
        "riddleOptionsPresent": True,
        "globalLabelCount": 6,
        "labelDescriptors": [
            {
                "name": name.casefold(),
                "controlSource": source,
                "controlType": "checkbox",
                "checked": name in {"Twilight Sparkle", "Applejack"},
                "disabled": False,
                "sameForm": True,
            }
            for name, source in zip(
                ponychart_module._PONYCHART_LABEL_NAMES,
                (
                    "label-control",
                    "label-control",
                    "for",
                    "for",
                    "nested",
                    "nested",
                ),
                strict=True,
            )
        ],
        "countdownSeconds": 20,
        "countdownSource": "riddlecounter-class-sprite",
        "countdownCandidateCount": 1,
        "storageAvailable": True,
        "initialSubmitDisabled": True,
        "initialCountdownSeconds": 30,
        "initialCountdownSource": "riddlecounter-class-sprite",
        "initialCountdownCandidateCount": 1,
        "countdownAtSubmitSeconds": 22,
        "countdownAtSubmitSource": "riddlecounter-class-sprite",
        "countdownAtSubmitCandidateCount": 1,
        "elapsedMs": 100,
        "submitEnabledElapsedMs": 40,
        "selectionElapsedMs": 50,
        "submitCommandElapsedMs": 60,
        "clickEventElapsedMs": 70,
        "formSubmitEventElapsedMs": 80,
        "transitionElapsedMs": 90,
        "mutationCount": 3,
        "selectedCount": 2,
        "submitInvocationCount": 1,
        "commandClickEventCount": 1,
        "commandFormSubmitEventCount": 1,
        "commandSubmitterMatchCount": 1,
        "commandFormSubmitPreventedCount": 0,
    }
    diagnostic.update(overrides)
    return diagnostic


def _receipt_observation(
    *,
    diagnostic_overrides: dict[str, object] | None = None,
    **overrides: object,
) -> dict[str, object]:
    observed: dict[str, object] = {
        "status": "observed",
        "monitorFound": True,
        "storageFound": False,
        "present": False,
        "battlePresent": True,
        "documentUrl": "https://hentaiverse.org/battle",
        "origin": "https://hentaiverse.org",
        "disappeared": True,
        "selectionApplied": True,
        "diagnostic": _page_diagnostic(**(diagnostic_overrides or {})),
    }
    observed.update(overrides)
    return observed


def _submit_acknowledgement(
    status: str,
    *,
    selected_count: int | None = None,
    diagnostic_overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    acknowledgement: dict[str, object] = {
        "status": status,
        "diagnostic": _page_diagnostic(**(diagnostic_overrides or {})),
    }
    if selected_count is not None:
        acknowledgement["selectedCount"] = selected_count
    return acknowledgement


class _ImageEventPage:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state
        self.handlers: dict[type[Any], list[Any]] = defaultdict(list)
        self.observer_armed = asyncio.Event()
        self.snapshot_calls = 0
        self.token: str | None = None

    def add_handler(self, event_type: type[Any], handler: Any) -> None:
        self.handlers[event_type].append(handler)

    def remove_handlers(self, event_type: type[Any], handler: Any) -> None:
        self.handlers[event_type].remove(handler)

    async def send(self, _command: object) -> None:
        return None

    async def evaluate(self, expression: str) -> object:
        if "canvas.toDataURL" in expression:
            image = _png_bytes(64, 64)
            return _canvas_payload(image, 64, 64)
        self.snapshot_calls += 1
        match = re.search(r"const token = (\"[0-9a-f]+\");", expression)
        if match is None:
            raise AssertionError("image observer token was not embedded")
        self.token = json.loads(match.group(1))
        self.observer_armed.set()
        return self.state

    async def emit_image_change(self) -> None:
        event = SimpleNamespace(
            name=ponychart_module._PONYCHART_IMAGE_BINDING,
            payload=self.token,
        )
        for handler in tuple(self.handlers[cdp.runtime.BindingCalled]):
            await handler(event)


_NODE_IMAGE_READY_HARNESS = r"""
const fs = require("node:fs");
const expression = JSON.parse(fs.readFileSync(0, "utf8"));
const observers = [];
const wakeups = [];

class FakeMutationObserver {
    constructor(callback) {
        this.callback = callback;
        this.disconnected = false;
        observers.push(this);
    }
    observe() {}
    disconnect() { this.disconnected = true; }
    trigger() { if (!this.disconnected) this.callback([]); }
}

class FakeImage {
    constructor(src, width, height, complete) {
        this.src = src;
        this.currentSrc = src;
        this.naturalWidth = width;
        this.naturalHeight = height;
        this.complete = complete;
        this.listeners = new Map();
    }
    addEventListener(name, callback) {
        const listeners = this.listeners.get(name) || [];
        listeners.push(callback);
        this.listeners.set(name, listeners);
    }
    removeEventListener(name, callback) {
        const listeners = this.listeners.get(name) || [];
        this.listeners.set(name, listeners.filter((item) => item !== callback));
    }
    dispatch(name) {
        for (const callback of [...(this.listeners.get(name) || [])]) callback();
    }
    getBoundingClientRect() {
        return {width: this.naturalWidth, height: this.naturalHeight};
    }
}

globalThis.window = globalThis;
globalThis.MutationObserver = FakeMutationObserver;
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.__hvbattle_ponychart_image_changed__ = (token) => wakeups.push(token);
let image = new FakeImage("placeholder", 1, 1, true);
globalThis.document = {
    documentElement: {},
    getElementById(id) {
        if (id !== "riddleimage") return null;
        return {querySelector: () => image};
    },
};

const placeholder = eval(expression);
image.src = image.currentSrc = "challenge";
image.complete = false;
image.naturalWidth = 0;
image.naturalHeight = 0;
observers.at(-1).trigger();
const sourceMutationWakeups = wakeups.length;
const loading = eval(expression);
image.complete = true;
image.naturalWidth = 640;
image.naturalHeight = 480;
image.dispatch("load");
const loadWakeups = wakeups.length;
const loaded = eval(expression);

image = new FakeImage("already-real", 800, 600, true);
const initiallyReal = eval(expression);
process.stdout.write(JSON.stringify({
    placeholder,
    loading,
    loaded,
    initiallyReal,
    sourceMutationWakeups,
    loadWakeups,
}));
"""

_NODE_CAPTURE_RECEIPT_HARNESS = r"""
const fs = require("node:fs");
const expression = JSON.parse(fs.readFileSync(0, "utf8"));
let image = {
    src: "challenge",
    currentSrc: "challenge",
    complete: true,
    naturalWidth: 640,
    naturalHeight: 480,
};
globalThis.document = {
    getElementById(id) {
        if (id !== "riddleimage") return null;
        return {querySelector: () => image};
    },
    createElement(name) {
        if (name !== "canvas") throw new Error("unexpected element");
        return {
            width: 0,
            height: 0,
            getContext: () => ({drawImage() {}}),
            toDataURL: () => "data:image/png;base64,eA==",
        };
    },
};
const matching = eval(expression);
image = {...image, src: "placeholder", currentSrc: "placeholder",
    naturalWidth: 1, naturalHeight: 1};
const placeholder = eval(expression);
image = {...image, src: "other-challenge", currentSrc: "other-challenge",
    naturalWidth: 640, naturalHeight: 480};
const changedSource = eval(expression);
process.stdout.write(JSON.stringify({matching, placeholder, changedSource}));
"""

_NODE_PONYCHART_SUBMISSION_HARNESS = r"""
const fs = require("node:fs");
const expressions = JSON.parse(fs.readFileSync(0, "utf8"));
const storage = new Map();
const observers = [];
let clock = 100000;
Date.now = () => clock;

class FakeMutationObserver {
    constructor(callback) {
        this.callback = callback;
        this.disconnected = false;
        observers.push(this);
    }
    observe() {}
    disconnect() { this.disconnected = true; }
    trigger() { if (!this.disconnected) this.callback([]); }
}

class FakeEventTarget {
    constructor() { this.listeners = new Map(); }
    addEventListener(name, callback) {
        const callbacks = this.listeners.get(name) || [];
        callbacks.push(callback);
        this.listeners.set(name, callbacks);
    }
    removeEventListener(name, callback) {
        const callbacks = this.listeners.get(name) || [];
        this.listeners.set(
            name,
            callbacks.filter((candidate) => candidate !== callback),
        );
    }
    dispatch(name, event) {
        for (const callback of [...(this.listeners.get(name) || [])]) {
            callback(event);
        }
    }
}

const spriteCounter = (seconds) => ({
    id: "riddlecounter",
    className: "",
    textContent: "",
    value: "",
    children: [{
        className: "fc f4b",
        children: String(seconds).split("").map((digit) => ({
            className: `c4${digit}`,
        })),
    }],
    getAttribute: () => null,
});
const inlineSpriteCounter = (seconds) => ({
    id: "riddlecounter",
    className: "",
    textContent: "",
    value: "",
    children: [{
        className: "",
        children: String(seconds).split("").reverse().map((digit) => ({
            className: "",
            style: {
                background: `transparent url(font.png) 0px -${12 * Number(digit)}px`,
                backgroundPosition: `0px -${12 * Number(digit)}px`,
                backgroundPositionY: `-${12 * Number(digit)}px`,
            },
        })),
    }],
    getAttribute: () => null,
});

class FakeDocument extends FakeEventTarget {
    constructor({challenge = true, battle = false, autoEnable = true} = {}) {
        super();
        this.readyState = "interactive";
        this.documentElement = {};
        this.challenge = challenge;
        this.battle = battle;
        this.counter = spriteCounter(30);
        this.timerCandidates = [];
        this.form = {};
        this.autoEnable = autoEnable;
        this.labelClicks = 0;
        this.controls = new Map();
        this.labels = [];
        const names = [
            "Twilight Sparkle",
            "Rarity",
            "Fluttershy",
            "Rainbow Dash",
            "Pinkie Pie",
            "Applejack",
        ];
        names.forEach((name, index) => {
            const control = {
                id: `pony-${index}`,
                type: "checkbox",
                checked: false,
                disabled: false,
                form: this.form,
            };
            this.controls.set(control.id, control);
            const label = {
                innerText: name,
                textContent: name,
                control: index < 2 ? control : null,
                htmlFor: index >= 2 && index < 4 ? control.id : "",
                querySelector: () => index >= 4 ? control : null,
                click: () => {
                    this.labelClicks += 1;
                    control.checked = !control.checked;
                    if (this.autoEnable) this.updateSubmitReadiness();
                },
            };
            this.labels.push(label);
        });
        this.externalLabel = {
            innerText: "Userscript option",
            textContent: "Userscript option",
            control: null,
            htmlFor: "",
            querySelector: () => null,
        };
        this.options = {
            querySelectorAll: (selector) => selector === "label.lc"
                ? this.labels : [],
        };
        this.master = {
            querySelectorAll: (selector) => selector === "label.lc"
                ? this.labels : [],
        };
        this.submitClicks = 0;
        this.submit = {
            tagName: "INPUT",
            type: "submit",
            value: "Submit Answer",
            textContent: "",
            isConnected: true,
            disabled: true,
            form: this.form,
            getAttribute: () => null,
            click: () => {
                if (this.submit.disabled) return;
                this.submitClicks += 1;
                const clickEvent = {
                    target: this.submit,
                    defaultPrevented: false,
                    preventDefault() { this.defaultPrevented = true; },
                };
                this.dispatch("click", clickEvent);
                const submitEvent = {
                    target: this.form,
                    submitter: this.submit,
                    defaultPrevented: false,
                    preventDefault() { this.defaultPrevented = true; },
                };
                this.dispatch("submit", submitEvent);
            },
        };
    }
    updateSubmitReadiness() {
        const selected = new Set(this.labels
            .filter((label) => {
                const control = label.control
                    || (label.htmlFor ? this.controls.get(label.htmlFor) : null)
                    || label.querySelector('input[type="checkbox"]');
                return control && control.checked;
            })
            .map((label) => label.textContent));
        this.submit.disabled = !(
            selected.size === 2
            && selected.has("Applejack")
            && selected.has("Twilight Sparkle")
        );
    }
    setCounterSeconds(seconds) {
        this.counter = spriteCounter(seconds);
    }
    getElementById(id) {
        if (id === "riddlesubmit") return this.challenge ? this.submit : null;
        if (id === "riddlecounter") return this.challenge ? this.counter : null;
        if (id === "riddlemaster") return this.challenge ? this.master : null;
        if (id === "riddler1") return this.challenge ? this.options : null;
        if (id === "battle_main") return this.battle ? {} : null;
        return this.controls.get(id) || null;
    }
    querySelectorAll(selector) {
        if (selector === "label.lc") {
            return this.challenge ? [...this.labels, this.externalLabel] : [];
        }
        if (selector.includes('[id*="countdown"]')) {
            return this.challenge ? this.timerCandidates : [];
        }
        if (selector.includes('input[type="submit"]')) {
            return this.challenge ? [this.submit] : [];
        }
        return [];
    }
}

globalThis.window = globalThis;
globalThis.MutationObserver = FakeMutationObserver;
const windowEvents = new FakeEventTarget();
globalThis.addEventListener = windowEvents.addEventListener.bind(windowEvents);
globalThis.removeEventListener = windowEvents.removeEventListener.bind(windowEvents);
globalThis.sessionStorage = {
    getItem: (key) => storage.has(key) ? storage.get(key) : null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
};
globalThis.location = {
    href: "https://hentaiverse.org/battle",
    origin: "https://hentaiverse.org",
};

const triggerMutations = () => {
    for (const observer of observers) observer.trigger();
};
const freshChallenge = (options = {}) => {
    const monitor = globalThis.__hvbattlePonyChartReceiptMonitor;
    if (monitor && typeof monitor.detach === "function") monitor.detach();
    globalThis.document = new FakeDocument(options);
    globalThis.location.href = "https://hentaiverse.org/battle";
    delete globalThis.__hvbattlePonyChartReceiptMonitor;
};
const transitionToBattleDocument = () => {
    const monitor = globalThis.__hvbattlePonyChartReceiptMonitor;
    windowEvents.dispatch("pagehide", {target: globalThis});
    if (monitor && typeof monitor.detach === "function") monitor.detach();
    globalThis.document = new FakeDocument({challenge: false, battle: true});
    globalThis.location.href = "https://hentaiverse.org/battle?next=1";
    delete globalThis.__hvbattlePonyChartReceiptMonitor;
};

freshChallenge();
const autoEnabledArmed = eval(expressions.arm);
const autoEnabledSubmitted = eval(expressions.submit);
const autoEnableClicks = document.submitClicks;

freshChallenge({autoEnable: false});
const armed = eval(expressions.arm);
const disabled = eval(expressions.submit);
const clicksWhileDisabled = document.submitClicks;
const labelClicksBeforeRetry = document.labelClicks;
clock += 4000;
document.submit.disabled = false;
document.setCounterSeconds(26);
triggerMutations();
const submitted = eval(expressions.submit);
const labelClicksAfterRetry = document.labelClicks;
const repeated = eval(expressions.submit);
const selected = document.labels
    .filter((label) => {
        const control = label.control
            || (label.htmlFor ? document.getElementById(label.htmlFor) : null)
            || label.querySelector('input[type="checkbox"]');
        return control.checked;
    })
    .map((label) => label.textContent);
const finalClickCount = document.submitClicks;
clock += 50;
document.challenge = false;
document.battle = true;
triggerMutations();
const sameDocumentReceipt = eval(expressions.read);

freshChallenge();
eval(expressions.arm);
document.submit.disabled = false;
clock += 1000;
const fastSubmitted = eval(expressions.submit);
const fastClickCount = document.submitClicks;
clock += 20;
transitionToBattleDocument();
clock += 5980;
const navigationReceipt = eval(expressions.read);

freshChallenge();
const delayedExecutionArmed = eval(expressions.arm);
clock += 29500;
document.setCounterSeconds(1);
const delayedExecutionSubmit = eval(expressions.submit);
const delayedExecutionClicks = document.submitClicks;

freshChallenge();
const staleTimerArmed = eval(expressions.arm);
clock += 28000;
const staleTimerSubmitted = eval(expressions.submit);
clock += 2000;
transitionToBattleDocument();
const staleTimerReceipt = eval(expressions.read);

freshChallenge();
const naturalArmed = eval(expressions.arm);
clock += 30000;
transitionToBattleDocument();
const naturalReceipt = eval(expressions.read);

freshChallenge();
document.counter.textContent = "Time Left: 27";
document.counter.children = [];
const embeddedCounter = eval(expressions.arm);
freshChallenge();
document.counter = inlineSpriteCounter(27);
const inlineSpriteCounterReceipt = eval(expressions.arm);
freshChallenge();
document.counter.textContent = "20 / 30";
document.counter.children = [];
const ambiguousCounter = eval(expressions.arm);
freshChallenge();
document.counter.textContent = "not-a-time";
document.counter.children = [];
const malformedCounter = eval(expressions.arm);
freshChallenge();
document.counter = null;
document.timerCandidates = [{
    id: "status",
    className: "countdown-primary",
    textContent: "17",
    value: "",
    getAttribute: () => null,
}];
const fallbackCounter = eval(expressions.arm);
freshChallenge();
document.counter.textContent = "";
document.counter.children = [{
    className: "fc f4b",
    children: [{className: "c42"}, {className: "c4x"}],
}];
const malformedSpriteCounter = eval(expressions.arm);
const malformedSpriteSubmit = eval(expressions.submit);
const malformedSpriteClicks = document.submitClicks;
freshChallenge();
document.counter = inlineSpriteCounter(27);
document.counter.children[0].children[0].style.backgroundPositionY = "-13px";
const malformedInlineSpriteCounter = eval(expressions.arm);
freshChallenge();
document.labels[0].control.form = {};
const foreignFormArmed = eval(expressions.arm);
const foreignFormSubmit = eval(expressions.submit);

process.stdout.write(JSON.stringify({
    autoEnabledArmed,
    autoEnabledSubmitted,
    autoEnableClicks,
    armed,
    disabled,
    clicksWhileDisabled,
    labelClicksBeforeRetry,
    submitted,
    labelClicksAfterRetry,
    repeated,
    selected,
    finalClickCount,
    sameDocumentReceipt,
    fastSubmitted,
    fastClickCount,
    navigationReceipt,
    delayedExecutionArmed,
    delayedExecutionSubmit,
    delayedExecutionClicks,
    staleTimerArmed,
    staleTimerSubmitted,
    staleTimerReceipt,
    naturalArmed,
    naturalReceipt,
    embeddedCounter,
    inlineSpriteCounterReceipt,
    ambiguousCounter,
    malformedCounter,
    fallbackCounter,
    malformedSpriteCounter,
    malformedSpriteSubmit,
    malformedSpriteClicks,
    malformedInlineSpriteCounter,
    foreignFormArmed,
    foreignFormSubmit,
}));
"""


class PonyChartArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def test_absent_challenge_has_an_exact_typed_outcome(self) -> None:
        driver = Mock(headless=True)
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=False)

        outcome = await challenge.check()

        self.assertIs(outcome, PonyChartResolutionOutcome.NOT_PRESENT)

    async def test_global_worker_close_timeout_is_capped_at_five_seconds(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, r"\(0, 5\]"):
            await ponychart_module.close_ponychart_workers(timeout=5.01)

    async def test_image_binding_lock_wait_uses_the_semantic_deadline(self) -> None:
        driver = Mock(headless=True)
        driver.page = Mock()
        challenge = PonyChart(driver)
        await challenge._image_binding_lock.acquire()
        try:
            with self.assertRaisesRegex(TimeoutError, "binding setup ownership"):
                await challenge._ensure_image_binding(SemanticDeadline.after(0.01))
        finally:
            challenge._image_binding_lock.release()

        driver.page.send.assert_not_called()

    async def test_late_page_enable_does_not_install_image_binding(self) -> None:
        now = 0.0
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.send = AsyncMock()
        challenge = PonyChart(driver)
        deadline = SemanticDeadline(expires_at=1.0, _clock=lambda: now)

        async def finish_late(
            awaitable: object,
            *,
            timeout: float,
            owner: object,
        ) -> None:
            nonlocal now
            del timeout, owner
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            now = 1.1

        with (
            patch.object(
                ponychart_module,
                "wait_for_zendriver",
                side_effect=finish_late,
            ),
            self.assertRaisesRegex(TimeoutError, "page binding setup"),
        ):
            await challenge._ensure_image_binding(deadline)

        self.assertEqual(driver.page.send.call_count, 1)

    async def test_initial_real_image_is_accepted_without_elapsed_stability(
        self,
    ) -> None:
        driver = Mock(headless=True)
        driver.page = _ImageEventPage(
            {
                "ready": True,
                "source": "challenge",
                "width": 640,
                "height": 480,
                "renderedWidth": 640,
                "renderedHeight": 480,
            }
        )
        challenge = PonyChart(driver)

        receipt = await challenge._wait_for_image_loaded(
            deadline=SemanticDeadline.after(1.0),
        )

        self.assertEqual(receipt.source, "challenge")
        self.assertEqual((receipt.width, receipt.height), (640, 480))
        self.assertEqual(driver.page.snapshot_calls, 1)
        self.assertTrue(all(not handlers for handlers in driver.page.handlers.values()))

    async def test_small_rendered_size_is_diagnostic_not_canvas_readiness(self) -> None:
        state = ponychart_module._decode_image_state(
            {
                "ready": True,
                "source": "challenge",
                "width": 640,
                "height": 480,
                "renderedWidth": 1,
                "renderedHeight": 1,
            }
        )

        self.assertTrue(state.ready)
        self.assertEqual((state.rendered_width, state.rendered_height), (1, 1))

    async def test_complete_one_pixel_placeholder_waits_for_binding_event(
        self,
    ) -> None:
        driver = Mock(headless=True)
        driver.page = _ImageEventPage(
            {
                "ready": False,
                "source": "placeholder",
                "width": 1,
                "height": 1,
                "renderedWidth": 1,
                "renderedHeight": 1,
            }
        )
        challenge = PonyChart(driver)
        waiter = asyncio.create_task(
            challenge._wait_for_image_loaded(
                deadline=SemanticDeadline.after(1.0),
            )
        )
        await asyncio.wait_for(driver.page.observer_armed.wait(), timeout=1.0)
        self.assertFalse(waiter.done())

        driver.page.state = {
            "ready": True,
            "source": "challenge",
            "width": 640,
            "height": 480,
            "renderedWidth": 640,
            "renderedHeight": 480,
        }
        await driver.page.emit_image_change()
        receipt = await waiter

        self.assertEqual(receipt.source, "challenge")
        self.assertEqual(driver.page.snapshot_calls, 2)

    async def test_one_pixel_is_never_sent_to_classifier_before_valid_load(
        self,
    ) -> None:
        driver = Mock(headless=True)
        page = _ImageEventPage(
            {
                "ready": False,
                "source": "placeholder",
                "width": 1,
                "height": 1,
                "renderedWidth": 1,
                "renderedHeight": 1,
            }
        )
        driver.page = page
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)
        challenge._arm_challenge_receipt_monitor = AsyncMock(
            return_value=_receipt_context()
        )
        challenge._predict_labels = AsyncMock(return_value=("Applejack",))
        challenge._select_and_submit_answer = AsyncMock(return_value=True)
        challenge._wait_for_challenge_receipt = AsyncMock()

        resolution = asyncio.create_task(challenge.check())
        await asyncio.wait_for(page.observer_armed.wait(), timeout=1.0)
        challenge._predict_labels.assert_not_awaited()
        page.state = {
            "ready": True,
            "source": "challenge",
            "width": 64,
            "height": 64,
            "renderedWidth": 64,
            "renderedHeight": 64,
        }
        await page.emit_image_change()

        outcome = await resolution

        self.assertIs(outcome, PonyChartResolutionOutcome.SUBMISSION_CONFIRMED)
        classified_image = challenge._predict_labels.await_args.args[0]
        self.assertEqual(ponychart_module._validate_png(classified_image), (64, 64))

    async def test_one_pixel_until_deadline_expires_without_inference(self) -> None:
        driver = Mock(headless=True)
        driver.page = _ImageEventPage(
            {
                "ready": False,
                "source": "placeholder",
                "width": 1,
                "height": 1,
                "renderedWidth": 1,
                "renderedHeight": 1,
            }
        )
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)
        deadline = SemanticDeadline.after(0.02)
        challenge._arm_challenge_receipt_monitor = AsyncMock(
            return_value=_receipt_context(deadline=deadline)
        )
        challenge._predict_labels = AsyncMock()
        challenge._reconcile_natural_expiration = AsyncMock(return_value=True)

        outcome = await challenge.check()

        self.assertIs(
            outcome,
            PonyChartResolutionOutcome.EXPIRED_WITHOUT_SUBMISSION,
        )
        challenge._predict_labels.assert_not_awaited()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JS test")
    async def test_source_mutation_and_image_load_wake_without_fixed_sleep(
        self,
    ) -> None:
        expression = (
            ponychart_module._ARM_PONYCHART_IMAGE_READY_JS.replace(
                "__TOKEN__",
                json.dumps("receipt-token"),
            )
            .replace("__MINIMUM_DIMENSION__", "50")
            .replace("__CLEANUP_MILLISECONDS__", "10000")
        )
        completed = subprocess.run(
            [shutil.which("node") or "node", "-e", _NODE_IMAGE_READY_HARNESS],
            input=json.dumps(expression),
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertFalse(result["placeholder"]["ready"])
        self.assertFalse(result["loading"]["ready"])
        self.assertTrue(result["loaded"]["ready"])
        self.assertTrue(result["initiallyReal"]["ready"])
        self.assertEqual(result["sourceMutationWakeups"], 1)
        self.assertEqual(result["loadWakeups"], 2)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JS test")
    async def test_atomic_canvas_capture_rejects_placeholder_and_changed_source(
        self,
    ) -> None:
        expression = (
            ponychart_module._CANVAS_CAPTURE_JS.replace(
                "__EXPECTED_SOURCE__",
                json.dumps("challenge"),
            )
            .replace("__EXPECTED_WIDTH__", "640")
            .replace("__EXPECTED_HEIGHT__", "480")
            .replace("__MINIMUM_DIMENSION__", "50")
        )
        completed = subprocess.run(
            [shutil.which("node") or "node", "-e", _NODE_CAPTURE_RECEIPT_HARNESS],
            input=json.dumps(expression),
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["matching"]["status"], "ok")
        self.assertEqual(result["placeholder"]["status"], "stale")
        self.assertEqual(result["changedSource"]["status"], "stale")

    async def test_stale_capture_reuses_same_deadline_and_waits_for_new_receipt(
        self,
    ) -> None:
        image = _png_bytes(59, 57)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            side_effect=[
                {"status": "stale", "errorName": "ImageReceiptChanged"},
                _canvas_payload(image, 59, 57),
            ]
        )
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(
            side_effect=[
                _image_receipt("first-challenge", 59, 57),
                _image_receipt("second-challenge", 59, 57),
            ]
        )
        deadline = SemanticDeadline.after(1.0)

        captured = await challenge._capture_pony_chart_image(deadline=deadline)

        self.assertEqual(captured, image)
        self.assertEqual(challenge._wait_for_image_loaded.await_count, 2)
        for awaited in challenge._wait_for_image_loaded.await_args_list:
            self.assertIs(awaited.kwargs["deadline"], deadline)
        first_script = driver.page.evaluate.await_args_list[0].args[0]
        second_script = driver.page.evaluate.await_args_list[1].args[0]
        self.assertIn('"first-challenge"', first_script)
        self.assertIn('"second-challenge"', second_script)

    async def test_canvas_capture_passes_page_as_timeout_owner(self) -> None:
        image = _png_bytes(53, 54)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(return_value=_canvas_payload(image, 53, 54))
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(
            return_value=_image_receipt(width=53, height=54)
        )
        observed_owners: list[object] = []
        original_wait = ponychart_module.wait_for_zendriver

        async def observe_owner(
            awaitable: object,
            *,
            timeout: float,
            owner: object,
        ) -> object:
            observed_owners.append(owner)
            return await original_wait(awaitable, timeout=timeout, owner=owner)

        with patch.object(
            ponychart_module,
            "wait_for_zendriver",
            side_effect=observe_owner,
        ):
            captured = await challenge._capture_pony_chart_image(
                deadline=SemanticDeadline.after(30.0)
            )

        self.assertEqual(captured, image)
        self.assertEqual(observed_owners, [driver.page])

    async def test_one_pixel_canvas_capture_is_rejected_before_inference(
        self,
    ) -> None:
        image = _png_bytes(1, 1)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(return_value=_canvas_payload(image, 1, 1))
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(
            return_value=_image_receipt(width=1, height=1)
        )

        with self.assertRaisesRegex(ValueError, "placeholder dimensions"):
            await challenge._capture_pony_chart_image(
                deadline=SemanticDeadline.after(1.0)
            )

    async def test_unconfigured_successful_resolution_stays_in_memory(
        self,
    ) -> None:
        image = b"challenge"
        driver = Mock(headless=True)
        retention = Mock()
        challenge = PonyChart(driver, retention_owner=retention)
        challenge._check = AsyncMock(return_value=True)
        challenge._capture_pony_chart_image = AsyncMock(return_value=image)
        challenge._predict_labels = AsyncMock(return_value=("Twilight",))
        challenge._arm_challenge_receipt_monitor = AsyncMock(
            return_value=_receipt_context()
        )
        challenge._select_and_submit_answer = AsyncMock(return_value=True)
        challenge._wait_for_challenge_receipt = AsyncMock()

        with patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()):
            detected = await challenge.check()

        self.assertIs(detected, PonyChartResolutionOutcome.SUBMISSION_CONFIRMED)
        challenge._predict_labels.assert_awaited_once_with(image, deadline=ANY)
        challenge._select_and_submit_answer.assert_awaited_once_with(
            ("Twilight",), monitor_id=ANY, deadline=ANY
        )
        challenge._wait_for_challenge_receipt.assert_awaited_once_with(
            ANY, deadline=ANY
        )
        retention.submit.assert_not_called()

    async def test_receipt_monitor_is_armed_before_image_capture(self) -> None:
        events: list[str] = []
        image = b"challenge"
        driver = Mock(headless=True)
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)

        async def arm(_monitor_id: str) -> object:
            events.append("arm")
            return _receipt_context()

        async def capture(*, deadline: SemanticDeadline) -> bytes:
            self.assertGreater(deadline.remaining(), 0)
            events.append("capture")
            return image

        challenge._arm_challenge_receipt_monitor = AsyncMock(side_effect=arm)
        challenge._capture_pony_chart_image = AsyncMock(side_effect=capture)
        challenge._predict_labels = AsyncMock(return_value=("Applejack",))
        challenge._select_and_submit_answer = AsyncMock(return_value=True)
        challenge._wait_for_challenge_receipt = AsyncMock()

        detected = await challenge.check()

        self.assertIs(detected, PonyChartResolutionOutcome.SUBMISSION_CONFIRMED)
        self.assertEqual(events, ["arm", "capture"])

    async def test_countdown_deadline_shrinks_across_all_mutation_phases(
        self,
    ) -> None:
        now = 0.0
        deadline = SemanticDeadline(expires_at=10.0, _clock=lambda: now)
        expiration_deadline = SemanticDeadline(
            expires_at=12.0,
            _clock=lambda: now,
        )
        context = _receipt_context(
            deadline=deadline,
            expiration_deadline=expiration_deadline,
        )
        driver = Mock(headless=True)
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)
        challenge._arm_challenge_receipt_monitor = AsyncMock(return_value=context)
        observed: list[tuple[str, float, object]] = []

        async def capture(*, deadline: SemanticDeadline) -> bytes:
            nonlocal now
            observed.append(("capture", deadline.remaining(), deadline))
            now = 3.0
            return b"challenge"

        async def predict(
            _image: bytes,
            *,
            deadline: SemanticDeadline,
        ) -> tuple[str, ...]:
            nonlocal now
            observed.append(("predict", deadline.remaining(), deadline))
            now = 5.0
            return ("Applejack",)

        async def submit(
            _labels: tuple[str, ...],
            *,
            monitor_id: str,
            deadline: SemanticDeadline,
        ) -> bool:
            nonlocal now
            del monitor_id
            observed.append(("submit", deadline.remaining(), deadline))
            now = 7.0
            return True

        async def receipt(
            _context: object,
            *,
            deadline: SemanticDeadline,
        ) -> None:
            observed.append(("receipt", deadline.remaining(), deadline))

        challenge._capture_pony_chart_image = AsyncMock(side_effect=capture)
        challenge._predict_labels = AsyncMock(side_effect=predict)
        challenge._select_and_submit_answer = AsyncMock(side_effect=submit)
        challenge._wait_for_challenge_receipt = AsyncMock(side_effect=receipt)

        outcome = await challenge.check()

        self.assertIs(outcome, PonyChartResolutionOutcome.SUBMISSION_CONFIRMED)
        self.assertEqual(
            [(phase, remaining) for phase, remaining, _ in observed],
            [("capture", 10.0), ("predict", 7.0), ("submit", 5.0), ("receipt", 5.0)],
        )
        self.assertTrue(
            all(
                observed_deadline is deadline
                for _, _, observed_deadline in observed[:3]
            )
        )
        self.assertIs(observed[-1][2], expiration_deadline)

    async def test_configured_directory_retains_successful_classifier_input(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = b"challenge"
            images = root / "pony_chart"
            driver = Mock(headless=True)
            retention = Mock()
            retention.submit.return_value = "queued"
            challenge = PonyChart(
                driver,
                image_directory=images,
                retention_owner=retention,
            )
            challenge._check = AsyncMock(return_value=True)
            challenge._capture_pony_chart_image = AsyncMock(return_value=image)
            challenge._predict_labels = AsyncMock(return_value=("Twilight",))
            challenge._arm_challenge_receipt_monitor = AsyncMock(
                return_value=_receipt_context()
            )
            challenge._select_and_submit_answer = AsyncMock(return_value=True)
            challenge._wait_for_challenge_receipt = AsyncMock()

            with patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()):
                detected = await challenge.check()

            self.assertIs(detected, PonyChartResolutionOutcome.SUBMISSION_CONFIRMED)
            challenge._predict_labels.assert_awaited_once_with(image, deadline=ANY)
            retention.submit.assert_called_once_with(image, images)
            self.assertFalse(images.exists())

    async def test_configured_directory_retains_image_after_prediction_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = b"challenge"
            images = root / "pony_chart"
            driver = Mock(headless=True)
            retention = Mock()
            retention.submit.return_value = "queued"
            challenge = PonyChart(
                driver,
                image_directory=images,
                retention_owner=retention,
            )
            challenge._check = AsyncMock(side_effect=[True, False])
            challenge._arm_challenge_receipt_monitor = AsyncMock(
                return_value=_receipt_context()
            )
            challenge._capture_pony_chart_image = AsyncMock(return_value=image)
            answer_error = ValueError("bad model")
            challenge._predict_labels = AsyncMock(side_effect=answer_error)
            challenge._reconcile_natural_expiration = AsyncMock(return_value=True)

            with (
                patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
                patch.object(ponychart_module, "logger") as ponychart_logger,
            ):
                detected = await challenge.check()

            self.assertIs(
                detected,
                PonyChartResolutionOutcome.EXPIRED_WITHOUT_SUBMISSION,
            )
            retention.submit.assert_called_once_with(image, images)
            ponychart_logger.warning.assert_called_once_with(
                "PonyChart inference failed before page mutation "
                "error_type=%s image_bytes=%d",
                "ValueError",
                len(image),
            )
            ponychart_logger.debug.assert_called_once_with(
                "PonyChart auto-answer error detail",
                exc_info=True,
            )
            ponychart_logger.error.assert_not_called()
            ponychart_logger.info.assert_not_called()

    async def test_capture_returns_bytes_without_touching_retention_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_directory = Path(directory) / "nested" / "pony_chart"
            image = _png_bytes(63, 57)
            driver = Mock(headless=True)
            driver.page = Mock()
            driver.page.evaluate = AsyncMock(
                return_value=_canvas_payload(image, 63, 57)
            )
            driver.page.send = AsyncMock()
            challenge = PonyChart(driver, image_directory=image_directory)
            challenge._wait_for_image_loaded = AsyncMock(
                return_value=_image_receipt(width=63, height=57)
            )

            first = await challenge._capture_pony_chart_image(
                deadline=SemanticDeadline.after(30.0)
            )
            second = await challenge._capture_pony_chart_image(
                deadline=SemanticDeadline.after(30.0)
            )

            self.assertFalse(image_directory.exists())
            self.assertEqual(first, image)
            self.assertEqual(second, image)
            self.assertEqual(driver.page.evaluate.await_count, 2)
            driver.page.send.assert_not_awaited()

    async def test_retain_pony_chart_image_enqueues_without_waiting_for_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = b"challenge"
            image_directory = root / "nested" / "pony_chart"
            driver = Mock(headless=True)
            retention = Mock()
            retention.submit.return_value = "queued"
            challenge = PonyChart(
                driver,
                image_directory=image_directory,
                retention_owner=retention,
            )

            await challenge._retain_pony_chart_image(image)
            await challenge._retain_pony_chart_image(image)

            self.assertEqual(
                retention.submit.call_args_list,
                [
                    call(image, image_directory),
                    call(image, image_directory),
                ],
            )
            self.assertFalse(image_directory.exists())

    async def test_retain_pony_chart_image_without_directory_is_noop(
        self,
    ) -> None:
        driver = Mock(headless=True)
        retention = Mock()
        challenge = PonyChart(driver, retention_owner=retention)

        await challenge._retain_pony_chart_image(b"challenge")

        retention.submit.assert_not_called()

    async def test_retain_pony_chart_image_logs_queue_full_drop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = b"challenge"
            image_directory = root / "pony_chart"
            driver = Mock(headless=True)
            retention = Mock()
            retention.submit.return_value = "full"
            challenge = PonyChart(
                driver,
                image_directory=image_directory,
                retention_owner=retention,
            )

            with patch.object(ponychart_module, "logger") as ponychart_logger:
                await challenge._retain_pony_chart_image(image)

            ponychart_logger.warning.assert_called_once_with(
                "PonyChart image retention queue is full; capture dropped "
                "image_bytes=%d",
                len(image),
            )

    async def test_canvas_security_error_uses_one_shot_screenshot_fallback(
        self,
    ) -> None:
        image = _png_bytes(67, 55)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            side_effect=[
                {"status": "security-error", "errorName": "SecurityError"},
                {
                    "status": "ok",
                    "x": 0.0,
                    "y": 12.5,
                    "width": 987.25,
                    "height": 543.75,
                },
                {"status": "stable"},
            ]
        )
        driver.page.send = AsyncMock(
            return_value=base64.b64encode(image).decode("ascii")
        )
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(return_value=_image_receipt())

        captured = await challenge._capture_pony_chart_image(
            deadline=SemanticDeadline.after(30.0)
        )

        self.assertEqual(captured, image)
        self.assertEqual(driver.page.evaluate.await_count, 3)
        driver.page.send.assert_awaited_once()

    async def test_changed_image_discards_screenshot_and_recaptures(self) -> None:
        stale_screenshot = _png_bytes(67, 55)
        final_image = _png_bytes(69, 56)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            side_effect=[
                {"status": "security-error", "errorName": "SecurityError"},
                {
                    "status": "ok",
                    "x": 0.0,
                    "y": 0.0,
                    "width": 640.0,
                    "height": 480.0,
                },
                {"status": "stale"},
                _canvas_payload(final_image, 69, 56),
            ]
        )
        driver.page.send = AsyncMock(
            return_value=base64.b64encode(stale_screenshot).decode("ascii")
        )
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(
            side_effect=[
                _image_receipt("first-challenge"),
                _image_receipt("second-challenge", 69, 56),
            ]
        )

        captured = await challenge._capture_pony_chart_image(
            deadline=SemanticDeadline.after(2.0)
        )

        self.assertEqual(captured, final_image)
        self.assertNotEqual(captured, stale_screenshot)
        self.assertEqual(challenge._wait_for_image_loaded.await_count, 2)
        driver.page.send.assert_awaited_once()

    async def test_invalid_canvas_base64_or_png_is_rejected_in_memory(
        self,
    ) -> None:
        header = struct.pack(">IIBBBBB", 3, 4, 8, 6, 0, 0, 0)
        header_only_png = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
        bad_checksum_png = bytearray(_png_bytes(3, 4))
        bad_checksum_png[-1] ^= 1
        cases = {
            "base64": {
                "status": "ok",
                "width": 3,
                "height": 4,
                "dataUrl": "data:image/png;base64,not%%%base64",
            },
            "png": {
                "status": "ok",
                "width": 3,
                "height": 4,
                "dataUrl": (
                    "data:image/png;base64,"
                    + base64.b64encode(b"not a PNG").decode("ascii")
                ),
            },
            "truncated-png": _canvas_payload(header_only_png, 3, 4),
            "bad-png-checksum": _canvas_payload(bytes(bad_checksum_png), 3, 4),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                driver = Mock(headless=True)
                driver.page = Mock()
                driver.page.evaluate = AsyncMock(return_value=payload)
                driver.page.send = AsyncMock()
                challenge = PonyChart(driver)
                challenge._wait_for_image_loaded = AsyncMock(
                    return_value=_image_receipt()
                )

                with self.assertRaises(ValueError):
                    await challenge._capture_pony_chart_image(
                        deadline=SemanticDeadline.after(30.0)
                    )

                driver.page.send.assert_not_awaited()

    async def test_canvas_header_dimension_mismatch_is_rejected_in_memory(
        self,
    ) -> None:
        image = _png_bytes(11, 9)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(return_value=_canvas_payload(image, 11, 10))
        driver.page.send = AsyncMock()
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(return_value=_image_receipt())

        with self.assertRaisesRegex(ValueError, "dimensions did not match"):
            await challenge._capture_pony_chart_image(
                deadline=SemanticDeadline.after(30.0)
            )

        driver.page.send.assert_not_awaited()

    async def test_canvas_timeout_does_not_fallback_or_retry(self) -> None:
        driver = Mock(headless=True)
        driver.page = Mock()

        async def hang(_script: str) -> object:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        driver.page.evaluate = AsyncMock(side_effect=hang)
        driver.page.send = AsyncMock()
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(return_value=_image_receipt())

        with (
            patch.object(
                ponychart_module,
                "_PONYCHART_DOM_CAPTURE_TIMEOUT_SECONDS",
                0.01,
            ),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await challenge._capture_pony_chart_image(
                deadline=SemanticDeadline.after(30.0)
            )

        driver.page.evaluate.assert_awaited_once()
        driver.page.send.assert_not_awaited()

    async def test_image_load_and_capture_share_one_absolute_deadline(self) -> None:
        now = 0.0
        image = _png_bytes(3, 4)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(return_value=_canvas_payload(image, 3, 4))
        driver.page.send = AsyncMock()
        challenge = PonyChart(driver)

        async def consume_load(*, deadline: SemanticDeadline) -> object:
            nonlocal now
            self.assertEqual(deadline.remaining(), 10.0)
            now = 9.2
            return _image_receipt()

        async def finish_canvas_late(
            awaitable: object,
            *,
            timeout: float,
            owner: object,
        ) -> object:
            nonlocal now
            del owner
            self.assertAlmostEqual(timeout, 0.8)
            now = 10.1
            return await awaitable  # type: ignore[misc]

        challenge._wait_for_image_loaded = AsyncMock(side_effect=consume_load)
        deadline = SemanticDeadline(expires_at=10.0, _clock=lambda: now)

        with (
            patch.object(
                ponychart_module,
                "wait_for_zendriver",
                side_effect=finish_canvas_late,
            ),
            self.assertRaisesRegex(TimeoutError, "canvas capture"),
        ):
            await challenge._capture_pony_chart_image(deadline=deadline)

        driver.page.evaluate.assert_awaited_once()
        driver.page.send.assert_not_awaited()

    async def test_screenshot_timeout_does_not_retry(self) -> None:
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            side_effect=[
                {"status": "security-error", "errorName": "SecurityError"},
                {
                    "status": "ok",
                    "x": 0,
                    "y": 0,
                    "width": 90,
                    "height": 80,
                },
            ]
        )

        async def hang(_command: object) -> object:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        driver.page.send = AsyncMock(side_effect=hang)
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(return_value=_image_receipt())

        with (
            patch.object(
                ponychart_module,
                "_PONYCHART_SCREENSHOT_TIMEOUT_SECONDS",
                0.01,
            ),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await challenge._capture_pony_chart_image(
                deadline=SemanticDeadline.after(30.0)
            )

        self.assertEqual(driver.page.evaluate.await_count, 2)
        driver.page.send.assert_awaited_once()


class PonyChartReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_arm_derives_action_and_expiry_deadlines_from_counter(self) -> None:
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            return_value={
                "status": "armed",
                "present": True,
                "documentUrl": "https://hentaiverse.org/battle",
                "origin": "https://hentaiverse.org",
                "diagnostic": _page_diagnostic(
                    initialCountdownSeconds=20,
                ),
            }
        )
        challenge = PonyChart(driver)

        context = await challenge._arm_challenge_receipt_monitor("monitor")

        self.assertIsNotNone(context)
        assert context is not None
        self.assertAlmostEqual(context.deadline.remaining(), 19.0, delta=0.1)
        self.assertAlmostEqual(
            context.expiration_classification_deadline.remaining(),
            21.0,
            delta=0.1,
        )

    async def test_arm_dispatch_latency_only_consumes_the_action_deadline(
        self,
    ) -> None:
        now = 0.0
        page = Mock()
        page.evaluate = Mock(return_value=object())
        driver = Mock(page=page)
        challenge = PonyChart(driver)
        raw = {
            "status": "armed",
            "present": True,
            "documentUrl": "https://hentaiverse.org/battle",
            "origin": "https://hentaiverse.org",
            "diagnostic": _page_diagnostic(initialCountdownSeconds=20),
        }

        async def return_after_dispatch(
            awaitable: object,
            *,
            timeout: float,
            owner: object,
        ) -> object:
            nonlocal now
            del awaitable, timeout, owner
            now = 3.0
            return raw

        fake_loop = SimpleNamespace(time=lambda: now)
        with (
            patch.object(
                ponychart_module.asyncio,
                "get_running_loop",
                return_value=fake_loop,
            ),
            patch.object(
                ponychart_module,
                "wait_for_zendriver",
                side_effect=return_after_dispatch,
            ),
        ):
            context = await challenge._arm_challenge_receipt_monitor("monitor")

        self.assertIsNotNone(context)
        assert context is not None
        self.assertAlmostEqual(context.deadline.remaining(), 16.0)
        self.assertAlmostEqual(
            context.expiration_classification_deadline.remaining(),
            21.0,
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JS test")
    async def test_realistic_dom_waits_then_submits_once_with_event_receipt(
        self,
    ) -> None:
        monitor_id = "dom-contract-monitor"
        arm = ponychart_module._render_ponychart_page_script(
            ponychart_module._ARM_PONYCHART_RECEIPT_JS,
            monitor_id=monitor_id,
        )
        submit = ponychart_module._render_ponychart_page_script(
            ponychart_module._SELECT_AND_SUBMIT_PONYCHART_JS,
            monitor_id=monitor_id,
            predicted_labels=("Applejack", "Twilight Sparkle"),
        )
        read = ponychart_module._render_ponychart_page_script(
            ponychart_module._READ_PONYCHART_RECEIPT_JS,
            monitor_id=monitor_id,
        )

        completed = subprocess.run(
            [
                shutil.which("node") or "node",
                "-e",
                _NODE_PONYCHART_SUBMISSION_HARNESS,
            ],
            input=json.dumps({"arm": arm, "submit": submit, "read": read}),
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertTrue(
            result["autoEnabledArmed"]["diagnostic"]["initialSubmitDisabled"]
        )
        self.assertEqual(result["autoEnabledSubmitted"]["status"], "submitted")
        self.assertEqual(result["autoEnableClicks"], 1)
        self.assertEqual(result["armed"]["status"], "armed")
        self.assertTrue(result["armed"]["diagnostic"]["initialSubmitDisabled"])
        self.assertEqual(result["armed"]["diagnostic"]["initialCountdownSeconds"], 30)
        self.assertEqual(
            result["armed"]["diagnostic"]["initialCountdownSource"],
            "riddlecounter-class-sprite",
        )
        self.assertEqual(result["armed"]["diagnostic"]["labelScope"], "riddler1")
        self.assertEqual(result["armed"]["diagnostic"]["labelCount"], 6)
        self.assertEqual(result["armed"]["diagnostic"]["globalLabelCount"], 7)
        self.assertNotIn(
            "unknown",
            {
                descriptor["name"]
                for descriptor in result["armed"]["diagnostic"]["labelDescriptors"]
            },
        )
        self.assertEqual(result["disabled"]["status"], "submit-not-ready")
        self.assertEqual(result["clicksWhileDisabled"], 0)
        self.assertEqual(result["disabled"]["diagnostic"]["selectedCount"], 2)
        self.assertEqual(result["labelClicksBeforeRetry"], 2)

        self.assertEqual(result["submitted"]["status"], "submitted")
        self.assertEqual(result["labelClicksAfterRetry"], 2)
        self.assertCountEqual(
            result["selected"],
            ["Applejack", "Twilight Sparkle"],
        )
        self.assertEqual(result["repeated"]["status"], "already-submitted")
        self.assertEqual(result["finalClickCount"], 1)
        submitted_diagnostic = result["submitted"]["diagnostic"]
        self.assertEqual(submitted_diagnostic["submitInvocationCount"], 1)
        self.assertEqual(submitted_diagnostic["commandClickEventCount"], 1)
        self.assertEqual(submitted_diagnostic["commandFormSubmitEventCount"], 1)
        self.assertEqual(submitted_diagnostic["commandSubmitterMatchCount"], 1)
        self.assertEqual(submitted_diagnostic["countdownAtSubmitSeconds"], 26)
        self.assertEqual(
            submitted_diagnostic["countdownAtSubmitSource"],
            "riddlecounter-class-sprite",
        )
        self.assertEqual(
            result["sameDocumentReceipt"]["diagnostic"]["transitionElapsedMs"],
            4050,
        )

        self.assertEqual(result["fastSubmitted"]["status"], "submitted")
        self.assertEqual(result["fastClickCount"], 1)
        navigation = result["navigationReceipt"]
        self.assertFalse(navigation["monitorFound"])
        self.assertTrue(navigation["storageFound"])
        self.assertEqual(navigation["diagnostic"]["submitInvocationCount"], 1)
        self.assertEqual(navigation["diagnostic"]["commandClickEventCount"], 1)
        self.assertEqual(navigation["diagnostic"]["commandFormSubmitEventCount"], 1)
        self.assertEqual(
            navigation["diagnostic"]["transitionElapsedMs"],
            navigation["diagnostic"]["formSubmitEventElapsedMs"] + 20,
        )

        delayed = result["delayedExecutionSubmit"]
        self.assertEqual(delayed["status"], "challenge-expiring")
        self.assertEqual(result["delayedExecutionClicks"], 0)
        self.assertEqual(delayed["diagnostic"]["submitInvocationCount"], 0)
        self.assertEqual(delayed["diagnostic"]["commandClickEventCount"], 0)
        self.assertEqual(delayed["diagnostic"]["countdownAtSubmitSeconds"], 0.5)
        self.assertEqual(
            delayed["diagnostic"]["countdownAtSubmitSource"],
            "armed-elapsed",
        )

        stale_timer = result["staleTimerSubmitted"]
        self.assertEqual(stale_timer["status"], "submitted")
        self.assertEqual(stale_timer["diagnostic"]["countdownAtSubmitSeconds"], 2)
        self.assertEqual(
            stale_timer["diagnostic"]["countdownAtSubmitSource"],
            "armed-elapsed",
        )
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(return_value=result["staleTimerReceipt"])
        challenge = PonyChart(driver)
        with self.assertRaises(BattleInterruptedError) as raised:
            await challenge._read_challenge_receipt(
                _receipt_context(),
                deadline=SemanticDeadline.after(1.0),
            )
        self.assertEqual(
            raised.exception.diagnostic_code,
            "battle.ponychart.receipt-timing-inconclusive",
        )

        natural = result["naturalReceipt"]
        self.assertFalse(natural["monitorFound"])
        self.assertTrue(natural["storageFound"])
        self.assertEqual(natural["diagnostic"]["submitInvocationCount"], 0)
        self.assertEqual(natural["diagnostic"]["commandClickEventCount"], 0)
        self.assertEqual(natural["diagnostic"]["commandFormSubmitEventCount"], 0)

        self.assertEqual(
            result["embeddedCounter"]["diagnostic"]["initialCountdownSeconds"],
            27,
        )
        self.assertEqual(
            result["embeddedCounter"]["diagnostic"]["initialCountdownSource"],
            "riddlecounter",
        )
        self.assertEqual(
            result["inlineSpriteCounterReceipt"]["diagnostic"][
                "initialCountdownSeconds"
            ],
            27,
        )
        self.assertEqual(
            result["inlineSpriteCounterReceipt"]["diagnostic"][
                "initialCountdownSource"
            ],
            "riddlecounter-inline-sprite",
        )
        for key in ("ambiguousCounter", "malformedCounter"):
            with self.subTest(counter=key):
                diagnostic = result[key]["diagnostic"]
                self.assertIsNone(diagnostic["initialCountdownSeconds"])
                self.assertEqual(diagnostic["initialCountdownSource"], "none")
                self.assertEqual(diagnostic["initialCountdownCandidateCount"], 1)
        self.assertEqual(
            result["fallbackCounter"]["diagnostic"]["initialCountdownSeconds"],
            17,
        )
        self.assertEqual(
            result["fallbackCounter"]["diagnostic"]["initialCountdownSource"],
            "id:status",
        )
        malformed_sprite = result["malformedSpriteCounter"]["diagnostic"]
        self.assertIsNone(malformed_sprite["initialCountdownSeconds"])
        self.assertEqual(malformed_sprite["initialCountdownSource"], "none")
        self.assertEqual(
            result["malformedSpriteSubmit"]["status"],
            "countdown-unverified",
        )
        self.assertEqual(result["malformedSpriteClicks"], 0)
        self.assertEqual(
            result["malformedSpriteSubmit"]["diagnostic"]["submitInvocationCount"],
            0,
        )
        malformed_inline = result["malformedInlineSpriteCounter"]["diagnostic"]
        self.assertIsNone(malformed_inline["initialCountdownSeconds"])
        self.assertEqual(malformed_inline["initialCountdownSource"], "none")
        self.assertFalse(
            result["foreignFormArmed"]["diagnostic"]["labelDescriptors"][0]["sameForm"]
        )
        self.assertEqual(
            result["foreignFormSubmit"]["status"],
            "label-contract-invalid",
        )

    async def test_cpu_prediction_is_sorted_and_never_mutates_the_page(self) -> None:
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock()
        inference = Mock()
        lease = Mock()
        inference.reserve.return_value = lease
        inference.predict_reserved = AsyncMock(return_value=("Rarity", "Applejack"))
        descriptor = ponychart_module.PonyChartGenerationDescriptor(
            "a" * 64,
            Path("model.onnx"),
            Path("thresholds.json"),
        )
        challenge = PonyChart(driver, inference_owner=inference)

        with patch.object(ponychart_module, "_generation_descriptor", descriptor):
            labels = await challenge._predict_labels(b"challenge")

        self.assertEqual(labels, ("Applejack", "Rarity"))
        inference.reserve.assert_called_once_with(descriptor)
        inference.predict_reserved.assert_awaited_once_with(
            lease,
            b"challenge",
            timeout=ponychart_module._PONYCHART_INFERENCE_DEADLINE_SECONDS,
        )
        driver.page.evaluate.assert_not_awaited()

    async def test_inference_budget_is_capped_by_shared_challenge_deadline(
        self,
    ) -> None:
        now = 0.0
        driver = Mock()
        inference = Mock()
        lease = Mock()
        inference.reserve.return_value = lease
        inference.predict_reserved = AsyncMock(return_value=("Applejack",))
        descriptor = ponychart_module.PonyChartGenerationDescriptor(
            "a" * 64,
            Path("model.onnx"),
            Path("thresholds.json"),
        )
        challenge = PonyChart(driver, inference_owner=inference)
        deadline = SemanticDeadline(expires_at=2.5, _clock=lambda: now)

        with patch.object(ponychart_module, "_generation_descriptor", descriptor):
            labels = await challenge._predict_labels(
                b"challenge",
                deadline=deadline,
            )

        self.assertEqual(labels, ("Applejack",))
        inference.predict_reserved.assert_awaited_once_with(
            lease,
            b"challenge",
            timeout=2.5,
        )

    async def test_inference_timeout_never_mutates_page(self) -> None:
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock()
        inference = Mock()
        lease = Mock()
        inference.reserve.return_value = lease
        inference.predict_reserved = AsyncMock(side_effect=TimeoutError("deadline"))
        descriptor = ponychart_module.PonyChartGenerationDescriptor(
            "a" * 64,
            Path("model.onnx"),
            Path("thresholds.json"),
        )
        challenge = PonyChart(driver, inference_owner=inference)

        with (
            patch.object(
                ponychart_module,
                "_generation_descriptor",
                descriptor,
            ),
            self.assertRaises(TimeoutError),
        ):
            await challenge._predict_labels(b"challenge")

        driver.page.evaluate.assert_not_awaited()

    async def test_all_labels_and_submit_use_one_atomic_page_mutation(self) -> None:
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            return_value=_submit_acknowledgement("submitted", selected_count=2)
        )
        challenge = PonyChart(driver)

        submitted = await challenge._select_and_submit_answer(
            ("Applejack", "Twilight Sparkle"),
            monitor_id="monitor",
            deadline=SemanticDeadline.after(15.0),
        )

        self.assertTrue(submitted)
        driver.page.evaluate.assert_awaited_once()
        script = driver.page.evaluate.await_args.args[0]
        self.assertIn("activeSubmit.click()", script)
        self.assertNotRegex(script, r"submit\.disabled\s*=(?!=)")
        self.assertNotIn("setTimeout", script)
        self.assertIn('"Applejack"', script)
        self.assertIn('"Twilight Sparkle"', script)

    async def test_click_ack_returning_just_after_action_deadline_is_reconciled(
        self,
    ) -> None:
        now = 0.0
        observed_timeouts: list[float] = []
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            return_value=_submit_acknowledgement(
                "submitted",
                selected_count=1,
                diagnostic_overrides={"selectedCount": 1, "checkedCount": 1},
            )
        )
        challenge = PonyChart(driver)
        deadline = SemanticDeadline(expires_at=0.01, _clock=lambda: now)

        async def return_after_deadline(
            awaitable: object,
            *,
            timeout: float,
            owner: object,
        ) -> object:
            nonlocal now
            del owner
            observed_timeouts.append(timeout)
            result = await awaitable  # type: ignore[misc]
            now = 0.011
            return result

        with patch.object(
            ponychart_module,
            "wait_for_zendriver",
            side_effect=return_after_deadline,
        ):
            submitted = await challenge._select_and_submit_answer(
                ("Applejack",),
                monitor_id="monitor",
                deadline=deadline,
            )

        self.assertTrue(submitted)
        self.assertEqual(
            observed_timeouts,
            [ponychart_module._PONYCHART_MUTATION_TIMEOUT_SECONDS],
        )

    async def test_late_semantic_ack_does_not_retire_browser_generation(
        self,
    ) -> None:
        async def evaluate(_expression: str) -> object:
            await asyncio.sleep(0.06)
            return _submit_acknowledgement(
                "submitted",
                selected_count=1,
                diagnostic_overrides={"selectedCount": 1, "checkedCount": 1},
            )

        page = SimpleNamespace(evaluate=evaluate)
        driver = SimpleNamespace(page=page)
        challenge = PonyChart(driver)  # type: ignore[arg-type]

        submitted = await challenge._select_and_submit_answer(
            ("Applejack",),
            monitor_id="monitor",
            deadline=SemanticDeadline.after(0.05),
        )

        async def probe() -> str:
            return "generation-alive"

        self.assertTrue(submitted)
        self.assertEqual(
            await wait_for_zendriver(probe(), timeout=0.1, owner=page),
            "generation-alive",
        )

    async def test_expired_action_deadline_never_dispatches_page_mutation(
        self,
    ) -> None:
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock()
        challenge = PonyChart(driver)
        deadline = SemanticDeadline(expires_at=0.0, _clock=lambda: 0.0)

        with self.assertRaises(BattleInterruptedError) as raised:
            await challenge._select_and_submit_answer(
                ("Applejack",),
                monitor_id="monitor",
                deadline=deadline,
            )

        self.assertEqual(
            raised.exception.diagnostic_code,
            "battle.ponychart.document-not-ready",
        )
        driver.page.evaluate.assert_not_awaited()

    async def test_disabled_submit_is_polled_after_idempotent_selection(
        self,
    ) -> None:
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            side_effect=[
                _submit_acknowledgement(
                    "submit-not-ready",
                    diagnostic_overrides={
                        "submitDisabled": True,
                        "checkedCount": 2,
                        "selectedCount": 2,
                        "selectionElapsedMs": 50,
                        "submitCommandElapsedMs": None,
                        "clickEventElapsedMs": None,
                        "formSubmitEventElapsedMs": None,
                        "transitionElapsedMs": None,
                        "submitInvocationCount": 0,
                        "commandClickEventCount": 0,
                        "commandFormSubmitEventCount": 0,
                    },
                ),
                _submit_acknowledgement("submitted", selected_count=2),
            ]
        )
        challenge = PonyChart(driver)

        with patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()):
            submitted = await challenge._select_and_submit_answer(
                ("Applejack", "Twilight Sparkle"),
                monitor_id="monitor",
                deadline=SemanticDeadline.after(15.0),
            )

        self.assertTrue(submitted)
        self.assertEqual(driver.page.evaluate.await_count, 2)

    async def test_missing_submit_event_evidence_is_not_reported_as_success(
        self,
    ) -> None:
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            return_value=_submit_acknowledgement(
                "submit-evidence-missing",
                diagnostic_overrides={
                    "formSubmitEventElapsedMs": None,
                    "commandFormSubmitEventCount": 0,
                },
            )
        )
        challenge = PonyChart(driver)

        with self.assertRaises(BattleInterruptedError) as raised:
            await challenge._select_and_submit_answer(
                ("Twilight Sparkle",),
                monitor_id="monitor",
                deadline=SemanticDeadline.after(15.0),
            )

        self.assertEqual(
            raised.exception.diagnostic_code,
            "battle.ponychart.submit-evidence-missing",
        )
        driver.page.evaluate.assert_awaited_once()

    async def test_invalid_label_contract_has_a_specific_diagnostic(self) -> None:
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            return_value=_submit_acknowledgement("label-contract-invalid")
        )
        challenge = PonyChart(driver)

        with self.assertRaises(BattleInterruptedError) as raised:
            await challenge._select_and_submit_answer(
                ("Twilight Sparkle",),
                monitor_id="monitor",
                deadline=SemanticDeadline.after(15.0),
            )

        self.assertEqual(
            raised.exception.diagnostic_code,
            "battle.ponychart.label-contract-invalid",
        )
        driver.page.evaluate.assert_awaited_once()

    async def test_pre_submit_countdown_stop_has_a_specific_diagnostic(
        self,
    ) -> None:
        cases = {
            "challenge-expiring": ("battle.ponychart.challenge-expiring-before-submit"),
            "countdown-unverified": (
                "battle.ponychart.countdown-unverified-before-submit"
            ),
        }
        for status, diagnostic_code in cases.items():
            with self.subTest(status=status):
                driver = Mock()
                driver.page = Mock()
                driver.page.evaluate = AsyncMock(
                    return_value=_submit_acknowledgement(status)
                )
                challenge = PonyChart(driver)

                with self.assertRaises(BattleInterruptedError) as raised:
                    await challenge._select_and_submit_answer(
                        ("Twilight Sparkle",),
                        monitor_id="monitor",
                        deadline=SemanticDeadline.after(15.0),
                    )

                self.assertEqual(
                    raised.exception.diagnostic_code,
                    diagnostic_code,
                )
                driver.page.evaluate.assert_awaited_once()

    async def test_submission_timeout_is_not_replayed(self) -> None:
        driver = Mock()
        driver.page = Mock()
        timeout = ZendriverOperationTimeout(timeout_seconds=5.0)
        driver.page.evaluate = AsyncMock(side_effect=timeout)
        challenge = PonyChart(driver)

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await challenge._select_and_submit_answer(
                ("Twilight Sparkle",),
                monitor_id="monitor",
                deadline=SemanticDeadline.after(15.0),
            )

        self.assertIs(raised.exception, timeout)
        driver.page.evaluate.assert_awaited_once()

    async def test_retired_generation_timeout_is_not_reconciled_or_replayed(
        self,
    ) -> None:
        image = b"challenge"
        timeout = ZendriverOperationTimeout(timeout_seconds=5.0)
        driver = Mock(headless=True)
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)
        challenge._arm_challenge_receipt_monitor = AsyncMock(
            return_value=_receipt_context()
        )
        challenge._capture_pony_chart_image = AsyncMock(return_value=image)
        challenge._predict_labels = AsyncMock(return_value=("Applejack",))
        challenge._select_and_submit_answer = AsyncMock(side_effect=timeout)
        challenge._reconcile_natural_expiration = AsyncMock(return_value=False)
        challenge._wait_for_challenge_receipt = AsyncMock()

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await challenge.check()

        self.assertIs(raised.exception, timeout)
        challenge._select_and_submit_answer.assert_awaited_once()
        challenge._reconcile_natural_expiration.assert_not_awaited()
        challenge._wait_for_challenge_receipt.assert_not_awaited()

    async def test_capture_generation_timeout_skips_natural_expiry_probe(
        self,
    ) -> None:
        timeout = ZendriverOperationTimeout(timeout_seconds=5.0)
        driver = Mock(headless=True)
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)
        challenge._arm_challenge_receipt_monitor = AsyncMock(
            return_value=_receipt_context()
        )
        challenge._capture_pony_chart_image = AsyncMock(side_effect=timeout)
        challenge._predict_labels = AsyncMock()
        challenge._reconcile_natural_expiration = AsyncMock()

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await challenge.check()

        self.assertIs(raised.exception, timeout)
        challenge._capture_pony_chart_image.assert_awaited_once()
        challenge._predict_labels.assert_not_awaited()
        challenge._reconcile_natural_expiration.assert_not_awaited()

    async def test_inference_generation_timeout_skips_natural_expiry_probe(
        self,
    ) -> None:
        timeout = ZendriverOperationTimeout(timeout_seconds=5.0)
        driver = Mock(headless=True)
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)
        challenge._arm_challenge_receipt_monitor = AsyncMock(
            return_value=_receipt_context()
        )
        challenge._capture_pony_chart_image = AsyncMock(return_value=b"challenge")
        challenge._predict_labels = AsyncMock(side_effect=timeout)
        challenge._reconcile_natural_expiration = AsyncMock()

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await challenge.check()

        self.assertIs(raised.exception, timeout)
        challenge._predict_labels.assert_awaited_once()
        challenge._reconcile_natural_expiration.assert_not_awaited()

    async def test_natural_expiry_probe_generation_timeout_is_not_polled(
        self,
    ) -> None:
        timeout = ZendriverOperationTimeout(timeout_seconds=5.0)
        driver = Mock()
        challenge = PonyChart(driver)
        challenge._observe_challenge_receipt = AsyncMock(side_effect=timeout)

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await challenge._reconcile_natural_expiration(
                _receipt_context(),
                deadline=SemanticDeadline.after(15.0),
            )

        self.assertIs(raised.exception, timeout)
        challenge._observe_challenge_receipt.assert_awaited_once()

    async def test_post_submit_receipt_generation_timeout_is_not_polled(
        self,
    ) -> None:
        timeout = ZendriverOperationTimeout(timeout_seconds=5.0)
        driver = Mock()
        challenge = PonyChart(driver)
        challenge._read_challenge_receipt = AsyncMock(side_effect=timeout)

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await challenge._wait_for_challenge_receipt(
                _receipt_context(),
                deadline=SemanticDeadline.after(15.0),
            )

        self.assertIs(raised.exception, timeout)
        challenge._read_challenge_receipt.assert_awaited_once()

    async def test_pre_submit_countdown_stop_waits_for_natural_expiration(
        self,
    ) -> None:
        for diagnostic_code in (
            "battle.ponychart.challenge-expiring-before-submit",
            "battle.ponychart.countdown-unverified-before-submit",
        ):
            with self.subTest(diagnostic_code=diagnostic_code):
                driver = Mock(headless=True)
                challenge = PonyChart(driver)
                challenge._check = AsyncMock(return_value=True)
                challenge._arm_challenge_receipt_monitor = AsyncMock(
                    return_value=_receipt_context()
                )
                challenge._capture_pony_chart_image = AsyncMock(
                    return_value=b"challenge"
                )
                challenge._predict_labels = AsyncMock(return_value=("Applejack",))
                challenge._select_and_submit_answer = AsyncMock(
                    side_effect=BattleInterruptedError(
                        "PonyChart stopped before submit",
                        diagnostic_code=diagnostic_code,
                    )
                )
                challenge._reconcile_natural_expiration = AsyncMock(return_value=True)

                outcome = await challenge.check()

                self.assertIs(
                    outcome,
                    PonyChartResolutionOutcome.EXPIRED_WITHOUT_SUBMISSION,
                )
                challenge._select_and_submit_answer.assert_awaited_once()
                challenge._reconcile_natural_expiration.assert_awaited_once()

    async def test_receipt_requires_submission_bound_document_transition(
        self,
    ) -> None:
        cases = {
            "same-document-authoritative": (
                _receipt_observation(),
                True,
            ),
            "absence-without-submission": (
                _receipt_observation(
                    selectionApplied=False,
                    diagnostic_overrides={
                        "selectedCount": 0,
                        "selectionElapsedMs": None,
                        "submitCommandElapsedMs": None,
                        "clickEventElapsedMs": None,
                        "formSubmitEventElapsedMs": None,
                        "submitInvocationCount": 0,
                        "commandClickEventCount": 0,
                        "commandFormSubmitEventCount": 0,
                    },
                ),
                False,
            ),
            "login-navigation": (
                _receipt_observation(
                    monitorFound=False,
                    storageFound=True,
                    documentUrl="https://hentaiverse.org/login",
                    battlePresent=False,
                    disappeared=False,
                ),
                False,
            ),
            "wrong-origin-battle-lookalike": (
                _receipt_observation(
                    monitorFound=False,
                    storageFound=True,
                    documentUrl="https://example.invalid/battle",
                    origin="https://example.invalid",
                    disappeared=False,
                ),
                False,
            ),
            "same-realm-new-battle-document": (
                _receipt_observation(
                    monitorFound=False,
                    storageFound=True,
                    documentUrl="https://hentaiverse.org/battle?next=1",
                ),
                True,
            ),
        }
        for name, (observation, expected) in cases.items():
            with self.subTest(name=name):
                driver = Mock()
                driver.page = Mock()
                driver.page.evaluate = AsyncMock(return_value=observation)
                challenge = PonyChart(driver)

                accepted = await challenge._read_challenge_receipt(
                    _receipt_context(),
                    deadline=SemanticDeadline.after(1.0),
                )

                self.assertIs(accepted, expected)

    async def test_receipt_timing_oracle_fails_closed(self) -> None:
        cases = {
            "zero-at-submit": {
                "countdownAtSubmitSeconds": 0,
            },
            "late-natural-transition": {
                "countdownAtSubmitSeconds": 2,
                "transitionElapsedMs": 1_500,
            },
            "counter-unverified": {
                "initialCountdownSeconds": None,
                "initialCountdownSource": "none",
                "initialCountdownCandidateCount": 1,
                "countdownAtSubmitSeconds": None,
                "countdownAtSubmitSource": "none",
                "countdownAtSubmitCandidateCount": 1,
            },
        }
        for name, diagnostic_overrides in cases.items():
            with self.subTest(name=name):
                driver = Mock()
                driver.page = Mock()
                driver.page.evaluate = AsyncMock(
                    return_value=_receipt_observation(
                        diagnostic_overrides=diagnostic_overrides
                    )
                )
                challenge = PonyChart(driver)

                with self.assertRaises(BattleInterruptedError) as raised:
                    await challenge._read_challenge_receipt(
                        _receipt_context(),
                        deadline=SemanticDeadline.after(1.0),
                    )

                self.assertEqual(
                    raised.exception.diagnostic_code,
                    "battle.ponychart.receipt-timing-inconclusive",
                )

    async def test_countdown_source_decoder_accepts_only_bounded_safe_metadata(
        self,
    ) -> None:
        decoded = ponychart_module._decode_page_diagnostic(
            _page_diagnostic(
                countdownSource="id:status",
                initialCountdownSource="class:countdown-primary",
                countdownAtSubmitSource="id:remaining",
            )
        )

        self.assertEqual(decoded.countdown_source, "id:status")
        self.assertEqual(
            decoded.initial_countdown_source,
            "class:countdown-primary",
        )
        with self.assertRaisesRegex(ValueError, "countdown source"):
            ponychart_module._decode_page_diagnostic(
                _page_diagnostic(countdownSource="id:<unsafe>")
            )

    async def test_slow_six_second_receipt_is_accepted(self) -> None:
        now = 0.0
        driver = Mock()
        challenge = PonyChart(driver)

        async def read_receipt(
            _monitor_id: str,
            *,
            deadline: SemanticDeadline,
        ) -> bool:
            self.assertGreater(deadline.remaining(), 0)
            return now >= 6.0

        async def advance(delay: float) -> None:
            nonlocal now
            now += delay

        challenge._read_challenge_receipt = AsyncMock(side_effect=read_receipt)
        deadline = SemanticDeadline.after(15.0, clock=lambda: now)

        with patch.object(ponychart_module.asyncio, "sleep", side_effect=advance):
            await challenge._wait_for_challenge_receipt(
                _receipt_context(),
                deadline=deadline,
                check_interval=1.0,
            )

        self.assertEqual(now, 6.0)
        self.assertEqual(challenge._read_challenge_receipt.await_count, 7)

    async def test_receipt_total_deadline_does_not_stack_per_probe(self) -> None:
        now = 0.0
        starts: list[float] = []
        driver = Mock()
        challenge = PonyChart(driver)

        async def consume_probe(
            _monitor_id: str,
            *,
            deadline: SemanticDeadline,
        ) -> bool:
            nonlocal now
            starts.append(now)
            now += min(5.0, deadline.remaining())
            return False

        challenge._read_challenge_receipt = AsyncMock(side_effect=consume_probe)
        deadline = SemanticDeadline.after(15.0, clock=lambda: now)

        with self.assertRaises(PonyChartResolutionError):
            await challenge._wait_for_challenge_receipt(
                _receipt_context(),
                deadline=deadline,
                check_interval=1.0,
            )

        self.assertEqual(now, 15.0)
        self.assertEqual(starts, [0.0, 5.0, 10.0])

    async def test_final_receipt_probe_cannot_accept_after_deadline(self) -> None:
        now = 14.0
        observed_timeouts: list[float] = []
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(return_value={})
        challenge = PonyChart(driver)
        deadline = SemanticDeadline(
            expires_at=15.0,
            _clock=lambda: now,
        )

        async def finish_late(
            awaitable: object,
            *,
            timeout: float,
            owner: object,
        ) -> object:
            nonlocal now
            del owner
            observed_timeouts.append(timeout)
            now = 15.1
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            return {"status": "observed", "present": False}

        with (
            patch.object(
                ponychart_module,
                "wait_for_zendriver",
                side_effect=finish_late,
            ),
            self.assertRaisesRegex(TimeoutError, "final state probe"),
        ):
            await challenge._read_challenge_receipt(
                _receipt_context(),
                deadline=deadline,
            )

        self.assertEqual(observed_timeouts, [1.0])


if __name__ == "__main__":
    unittest.main()

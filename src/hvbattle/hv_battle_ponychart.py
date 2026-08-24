import asyncio
import base64
import binascii
import json
import math
import struct
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from hvbrowser import HVDriver
from hvbrowser.runtime import (
    ZendriverOperationTimeout,
    is_browser_generation_error,
    notify,
    setup_logger,
    wait_for_zendriver,
)
from zendriver import cdp

from ._ponychart_store_process import PonyChartStoreProcessOwner
from ._ponychart_workers import (
    PonyChartGenerationDescriptor,
    PonyChartInferenceOwner,
    PonyChartRetentionOwner,
    PonyChartWorkerOwnershipError,
)
from ._timing import PROTOCOL_TIMEOUT_SECONDS, SemanticDeadline, protocol_timeout
from .contracts import BattleInterruptedError, PonyChartResolutionOutcome
from .ponychart_model_store import (
    LoadedPonyChartGeneration,
    PonyChartRefreshOutcome,
)

logger = setup_logger(__name__)

_PONYCHART_DOM_CAPTURE_TIMEOUT_SECONDS = 4.0
_PONYCHART_SCREENSHOT_TIMEOUT_SECONDS = PROTOCOL_TIMEOUT_SECONDS
_PONYCHART_MUTATION_TIMEOUT_SECONDS = PROTOCOL_TIMEOUT_SECONDS
_PONYCHART_INFERENCE_DEADLINE_SECONDS = 5.0
_PONYCHART_WORKER_PRELOAD_DEADLINE_SECONDS = 15.0
_PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS = 5.0
_PONYCHART_UNVERIFIED_TOTAL_DEADLINE_SECONDS = 15.0
_PONYCHART_MINIMUM_IMAGE_DIMENSION = 50
_PONYCHART_IMAGE_RETRY_INTERVAL_SECONDS = 0.1
_PONYCHART_SUBMIT_RETRY_INTERVAL_SECONDS = 0.1
_PONYCHART_PRE_EXPIRY_RESERVE_SECONDS = 1.0
_PONYCHART_COUNTDOWN_RESOLUTION_SECONDS = 1.0
_PONYCHART_EXPIRY_SAFETY_MARGIN_MILLISECONDS = (
    _PONYCHART_PRE_EXPIRY_RESERVE_SECONDS * 1_000
)
_PONYCHART_LABEL_NAMES = (
    "Twilight Sparkle",
    "Rarity",
    "Fluttershy",
    "Rainbow Dash",
    "Pinkie Pie",
    "Applejack",
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PONYCHART_IMAGE_BINDING = "__hvbattle_ponychart_image_changed__"
_PONYCHART_IMAGE_BINDING_PAGE_ATTRIBUTE = "_hvbattle_ponychart_image_binding"
_ARM_PONYCHART_IMAGE_READY_JS = r"""
(() => {
    const token = __TOKEN__;
    const wakeBinding = window["__hvbattle_ponychart_image_changed__"];
    const listenerKey = "__hvbattlePonyChartImageListener";
    const previous = window[listenerKey];
    const detach = (entry) => {
        if (!entry) return;
        clearTimeout(entry.timer);
        if (entry.observer) entry.observer.disconnect();
        if (entry.image) {
            entry.image.removeEventListener("load", entry.wake);
            entry.image.removeEventListener("error", entry.wake);
        }
    };
    detach(previous);
    window[listenerKey] = null;

    const container = document.getElementById("riddleimage");
    const image = container && container.querySelector("img");
    const source = image ? (image.currentSrc || image.src || "") : "";
    const width = image ? image.naturalWidth : 0;
    const height = image ? image.naturalHeight : 0;
    const rect = image && typeof image.getBoundingClientRect === "function"
        ? image.getBoundingClientRect() : {width: 0, height: 0};
    const renderedWidth = rect.width;
    const renderedHeight = rect.height;
    const ready = Boolean(
        image
        && image.complete
        && source
        && Number.isFinite(width)
        && Number.isFinite(height)
        && width >= __MINIMUM_DIMENSION__
        && height >= __MINIMUM_DIMENSION__
    );
    const result = {
        ready,
        source,
        width,
        height,
        renderedWidth,
        renderedHeight,
    };
    if (ready || !document.documentElement
            || typeof wakeBinding !== "function") {
        return result;
    }

    let entry = null;
    let fired = false;
    const wake = () => {
        if (fired) return;
        fired = true;
        detach(entry);
        window[listenerKey] = null;
        wakeBinding(token);
    };
    const observer = new MutationObserver(wake);
    observer.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["src", "srcset"],
        childList: true,
        subtree: true,
    });
    if (image) {
        image.addEventListener("load", wake);
        image.addEventListener("error", wake);
    }
    const timer = setTimeout(() => {
        if (window[listenerKey] === entry) {
            detach(entry);
            window[listenerKey] = null;
        }
    }, __CLEANUP_MILLISECONDS__);
    entry = {image, wake, observer, timer};
    window[listenerKey] = entry;
    return result;
})()
"""
_CANVAS_CAPTURE_JS = r"""
(() => {
    const expectedSource = __EXPECTED_SOURCE__;
    const expectedWidth = __EXPECTED_WIDTH__;
    const expectedHeight = __EXPECTED_HEIGHT__;
    const container = document.getElementById("riddleimage");
    const image = container && container.querySelector("img");
    if (!image || !image.complete) {
        return {status: "stale", errorName: "ImageNotReady"};
    }
    const source = image.currentSrc || image.src || "";
    const width = image.naturalWidth;
    const height = image.naturalHeight;
    if (source !== expectedSource
            || width !== expectedWidth
            || height !== expectedHeight
            || !Number.isFinite(width)
            || !Number.isFinite(height)
            || width < __MINIMUM_DIMENSION__
            || height < __MINIMUM_DIMENSION__) {
        return {status: "stale", errorName: "ImageReceiptChanged"};
    }
    try {
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d");
        if (!context) {
            return {status: "error", errorName: "CanvasContextUnavailable"};
        }
        context.drawImage(image, 0, 0, width, height);
        return {
            status: "ok",
            width: canvas.width,
            height: canvas.height,
            dataUrl: canvas.toDataURL("image/png"),
        };
    } catch (error) {
        const name = error && typeof error.name === "string"
            ? error.name : "CanvasError";
        const message = error && typeof error.message === "string"
            ? error.message : "";
        const isSecurityError = name === "SecurityError"
            || /taint|cross-origin|cross origin|insecure/i.test(message);
        return {
            status: isSecurityError ? "security-error" : "error",
            errorName: name,
        };
    }
})()
"""
_PONYCHART_IMAGE_RECT_JS = r"""
(() => {
    const monitorId = __MONITOR_ID__;
    const expectedSource = __EXPECTED_SOURCE__;
    const expectedWidth = __EXPECTED_WIDTH__;
    const expectedHeight = __EXPECTED_HEIGHT__;
    const monitorKey = "__hvbattlePonyChartScreenshotMonitor";
    const previous = window[monitorKey];
    if (previous && previous.observer) previous.observer.disconnect();
    const container = document.getElementById("riddleimage");
    const image = container && container.querySelector("img");
    if (!image || !image.complete) {
        return {status: "stale", errorName: "ImageNotReady"};
    }
    const source = image.currentSrc || image.src || "";
    const naturalWidth = image.naturalWidth;
    const naturalHeight = image.naturalHeight;
    if (source !== expectedSource
            || naturalWidth !== expectedWidth
            || naturalHeight !== expectedHeight
            || naturalWidth < __MINIMUM_DIMENSION__
            || naturalHeight < __MINIMUM_DIMENSION__) {
        return {status: "stale", errorName: "ImageReceiptChanged"};
    }
    const rect = image.getBoundingClientRect();
    const monitor = {
        id: monitorId,
        image,
        source,
        naturalWidth,
        naturalHeight,
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
        changed: false,
        observer: null,
    };
    const observer = new MutationObserver(() => { monitor.changed = true; });
    observer.observe(container, {
        attributes: true,
        attributeFilter: ["src", "srcset"],
        childList: true,
        subtree: true,
    });
    monitor.observer = observer;
    window[monitorKey] = monitor;
    return {
        status: "ok",
        x: rect.left + window.scrollX,
        y: rect.top + window.scrollY,
        width: rect.width,
        height: rect.height,
    };
})()
"""
_VERIFY_PONYCHART_SCREENSHOT_JS = r"""
(() => {
    const monitorId = __MONITOR_ID__;
    const monitorKey = "__hvbattlePonyChartScreenshotMonitor";
    const monitor = window[monitorKey];
    window[monitorKey] = null;
    if (!monitor || monitor.id !== monitorId) return {status: "stale"};
    if (monitor.observer) monitor.observer.disconnect();
    const container = document.getElementById("riddleimage");
    const image = container && container.querySelector("img");
    if (!image || image !== monitor.image || !image.complete || monitor.changed) {
        return {status: "stale"};
    }
    const rect = image.getBoundingClientRect();
    const source = image.currentSrc || image.src || "";
    const stable = source === monitor.source
        && image.naturalWidth === monitor.naturalWidth
        && image.naturalHeight === monitor.naturalHeight
        && rect.left === monitor.left
        && rect.top === monitor.top
        && rect.width === monitor.width
        && rect.height === monitor.height;
    return {status: stable ? "stable" : "stale"};
})()
"""
_PONYCHART_PAGE_HELPERS_JS = r"""
    const receiptKey = "__hvbattlePonyChartReceiptMonitor";
    const storagePrefix = "__hvbattlePonyChartReceipt:";
    const canonicalLabelNames = __EXPECTED_LABELS__;
    const canonicalLabelByNormalizedName = new Map(
        canonicalLabelNames.map((name) => [
            String(name || "").trim().toLowerCase(),
            String(name || "").trim().toLowerCase(),
        ])
    );
    const normalize = (value) => String(value || "").trim().toLowerCase();
    const elapsed = (state) => Math.max(0, Date.now() - state.armedAtEpochMs);
    const optionalElapsed = (value) => Number.isFinite(value)
        ? Math.max(0, value) : null;
    const safeReadyState = () => {
        const value = String(document.readyState || "unknown").toLowerCase();
        return ["loading", "interactive", "complete"].includes(value)
            ? value : "unknown";
    };
    const safeSubmitTag = (submit) => {
        if (!submit) return "missing";
        const value = String(submit.tagName || "").toLowerCase();
        return ["button", "input"].includes(value) ? value : "other";
    };
    const safeSubmitType = (submit) => {
        if (!submit) return "missing";
        const value = String(submit.type || "").toLowerCase();
        return ["submit", "button", "reset"].includes(value)
            ? value : "other";
    };
    const parseCountdownText = (raw, allowEmbeddedNumber) => {
        const text = String(raw || "").trim();
        if (!text) return null;
        const clockMatch = text.match(/^(\d{1,2}):(\d{1,2})$/);
        if (clockMatch) {
            const clockSeconds = Number(clockMatch[1]) * 60
                + Number(clockMatch[2]);
            return Number.isFinite(clockSeconds) && clockSeconds <= 600
                ? clockSeconds : null;
        }
        const numericTokens = text.match(/\d+(?:\.\d+)?/g) || [];
        if (numericTokens.length !== 1) return null;
        if (!allowEmbeddedNumber
                && !/(?:seconds?|secs?|s)(?:\D|$)/i.test(text)) {
            return null;
        }
        const seconds = Number(numericTokens[0]);
        return Number.isFinite(seconds) && seconds >= 0 && seconds <= 600
            ? seconds : null;
    };
    const readCountdown = () => {
        const missing = {seconds: null, source: "none", candidateCount: 0};
        if (typeof document.querySelectorAll !== "function") return missing;
        const exactCounter = document.getElementById("riddlecounter");
        let candidates = exactCounter ? [exactCounter] : [];
        try {
            candidates.push(...Array.from(document.querySelectorAll(
                '[id*="countdown"], [class*="countdown"], '
                + '[id*="timer"], [class*="timer"]'
            )).filter((candidate) => candidate !== exactCounter).slice(0, 19));
        } catch (_error) {
            candidates = exactCounter ? [exactCounter] : [];
        }
        const parseExactSpriteCounter = (counter) => {
            const wrappers = counter && counter.children
                ? Array.from(counter.children) : [];
            if (wrappers.length !== 1 || !wrappers[0].children) return null;
            const glyphs = Array.from(wrappers[0].children);
            if (glyphs.length < 1 || glyphs.length > 3) return null;
            const classDigits = [];
            for (const glyph of glyphs) {
                const tokens = String(glyph.className || "")
                    .trim().split(/\s+/).filter(Boolean);
                const digitTokens = tokens.filter((token) => /^c4[0-9]$/.test(token));
                if (digitTokens.length !== 1) {
                    classDigits.length = 0;
                    break;
                }
                classDigits.push(digitTokens[0].slice(2));
            }
            if (classDigits.length === glyphs.length) {
                const seconds = Number(classDigits.join(""));
                return Number.isFinite(seconds) && seconds >= 0 && seconds <= 600
                    ? {seconds, source: "riddlecounter-class-sprite"} : null;
            }
            const inlineDigits = [];
            for (const glyph of glyphs) {
                const style = glyph && glyph.style;
                if (!style || !String(style.background || "").trim()) return null;
                let yPosition = String(style.backgroundPositionY || "").trim();
                if (!yPosition) {
                    const positionTokens = String(
                        style.backgroundPosition || ""
                    ).trim().split(/\s+/).filter(Boolean);
                    if (positionTokens.length !== 2
                            || !/^0(?:px)?$/.test(positionTokens[0])) return null;
                    yPosition = positionTokens[1];
                }
                if (!/^-?\d+(?:\.\d+)?px$/.test(yPosition)) return null;
                const yOffset = Number(yPosition.slice(0, -2));
                const digit = Math.abs(yOffset) / 12;
                if (!Number.isFinite(yOffset)
                        || yOffset > 0 || yOffset < -108
                        || !Number.isInteger(digit)) return null;
                inlineDigits.push(String(digit));
            }
            const seconds = Number(inlineDigits.reverse().join(""));
            return Number.isFinite(seconds) && seconds >= 0 && seconds <= 600
                ? {seconds, source: "riddlecounter-inline-sprite"} : null;
        };
        const candidateSource = (candidate) => {
            if (candidate === exactCounter) return "riddlecounter";
            const safeToken = (value) => {
                const normalized = String(value || "").trim().toLowerCase();
                return /^[a-z0-9_-]{1,32}$/.test(normalized)
                    ? normalized : null;
            };
            const safeId = safeToken(candidate.id);
            if (safeId) return `id:${safeId}`;
            const safeClass = String(candidate.className || "")
                .split(/\s+/)
                .map(safeToken)
                .find((token) => token
                    && (token.includes("countdown") || token.includes("timer")));
            return safeClass ? `class:${safeClass}` : "timer-candidate";
        };
        for (const candidate of candidates) {
            const primaryText = String(candidate.textContent || "").trim()
                || String(candidate.value || "").trim()
                || String(candidate.getAttribute
                    ? candidate.getAttribute("aria-label") : "").trim();
            let seconds = parseCountdownText(
                primaryText,
                true,
            );
            let source = candidateSource(candidate);
            if (seconds === null && candidate === exactCounter) {
                const sprite = parseExactSpriteCounter(candidate);
                if (sprite !== null) {
                    seconds = sprite.seconds;
                    source = sprite.source;
                }
            }
            if (seconds !== null) {
                return {
                    seconds,
                    source,
                    candidateCount: candidates.length,
                };
            }
        }
        return {...missing, candidateCount: candidates.length};
    };
    const resolveSubmit = () => {
        const byId = document.getElementById("riddlesubmit");
        if (byId) return {element: byId, source: "riddlesubmit"};
        if (typeof document.querySelectorAll !== "function") {
            return {element: null, source: "none"};
        }
        let matches = [];
        try {
            matches = Array.from(document.querySelectorAll(
                'input[type="submit"], button[type="submit"]'
            )).filter((candidate) => normalize(
                candidate.value || candidate.textContent
            ) === "submit answer");
        } catch (_error) {
            return {element: null, source: "none"};
        }
        if (matches.length === 1) {
            return {element: matches[0], source: "caption-fallback"};
        }
        return {
            element: null,
            source: matches.length > 1 ? "ambiguous" : "none",
        };
    };
    const inspectLabelScope = () => {
        const master = document.getElementById("riddlemaster");
        const options = document.getElementById("riddler1");
        const container = options || master;
        let labels = [];
        let globalLabelCount = 0;
        if (typeof document.querySelectorAll === "function") {
            try {
                globalLabelCount = Array.from(
                    document.querySelectorAll("label.lc")
                ).length;
            } catch (_error) {
                globalLabelCount = 0;
            }
        }
        if (container && typeof container.querySelectorAll === "function") {
            try {
                labels = Array.from(container.querySelectorAll("label.lc"));
            } catch (_error) {
                labels = [];
            }
        } else if (typeof document.querySelectorAll === "function") {
            try {
                labels = Array.from(document.querySelectorAll("label.lc"));
            } catch (_error) {
                labels = [];
            }
        }
        return {
            container,
            labels,
            source: options ? "riddler1"
                : master ? "riddlemaster"
                : labels.length > 0 ? "global-diagnostic" : "none",
            masterPresent: Boolean(master),
            optionsPresent: Boolean(options),
            globalLabelCount,
        };
    };
    const resolveControlDescriptor = (label) => {
        if (label.control) {
            return {control: label.control, source: "label-control"};
        }
        if (label.htmlFor) {
            const referenced = document.getElementById(label.htmlFor);
            if (referenced) return {control: referenced, source: "for"};
        }
        if (typeof label.querySelector === "function") {
            const nested = label.querySelector('input[type="checkbox"]');
            if (nested) return {control: nested, source: "nested"};
        }
        return {control: null, source: "none"};
    };
    const resolveControl = (label) => resolveControlDescriptor(label).control;
    const safeControlType = (control) => {
        if (!control) return "missing";
        const value = normalize(control.type);
        return ["checkbox", "radio"].includes(value) ? value : "other";
    };
    const inspectPage = () => {
        const submitResolution = resolveSubmit();
        const submit = submitResolution.element;
        const labelScope = inspectLabelScope();
        const labels = labelScope.labels;
        const controls = labels.map(resolveControl).filter(Boolean);
        const labelDescriptors = labels.slice(0, 12).map((label) => {
            const descriptor = resolveControlDescriptor(label);
            const normalizedName = normalize(label.innerText || label.textContent);
            return {
                name: canonicalLabelByNormalizedName.get(normalizedName)
                    || "unknown",
                controlSource: descriptor.source,
                controlType: safeControlType(descriptor.control),
                checked: descriptor.control
                    && typeof descriptor.control.checked === "boolean"
                    ? descriptor.control.checked : null,
                disabled: descriptor.control
                    && typeof descriptor.control.disabled === "boolean"
                    ? descriptor.control.disabled : null,
                sameForm: descriptor.control && submit
                    ? descriptor.control.form === submit.form : null,
            };
        });
        const countdown = readCountdown();
        return {
            readyState: safeReadyState(),
            labelCount: labels.length,
            controlCount: controls.length,
            checkedCount: controls.filter(
                (control) => control.checked === true
            ).length,
            submitTag: safeSubmitTag(submit),
            submitType: safeSubmitType(submit),
            submitSource: submitResolution.source,
            submitConnected: submit
                ? submit.isConnected === true : null,
            submitCaptionMatches: submit
                ? normalize(submit.value || submit.textContent) === "submit answer"
                : null,
            submitDisabled: submit ? submit.disabled === true : null,
            submitAriaDisabled: submit
                ? normalize(submit.getAttribute
                    ? submit.getAttribute("aria-disabled") : "") === "true"
                : null,
            formAssociated: Boolean(submit && submit.form),
            labelScope: labelScope.source,
            riddleMasterPresent: labelScope.masterPresent,
            riddleOptionsPresent: labelScope.optionsPresent,
            globalLabelCount: labelScope.globalLabelCount,
            labelDescriptors,
            countdownSeconds: countdown.seconds,
            countdownSource: countdown.source,
            countdownCandidateCount: countdown.candidateCount,
        };
    };
    const pageDiagnostic = (state, storageAvailable) => {
        const live = inspectPage();
        return {
            ...live,
            storageAvailable: storageAvailable === true,
            initialSubmitDisabled: state
                ? state.initialSubmitDisabled : null,
            initialCountdownSeconds: state
                ? state.initialCountdownSeconds : null,
            initialCountdownSource: state
                ? state.initialCountdownSource : "none",
            initialCountdownCandidateCount: state
                ? state.initialCountdownCandidateCount : 0,
            countdownAtSubmitSeconds: state
                ? state.countdownAtSubmitSeconds : null,
            countdownAtSubmitSource: state
                ? state.countdownAtSubmitSource : "none",
            countdownAtSubmitCandidateCount: state
                ? state.countdownAtSubmitCandidateCount : 0,
            elapsedMs: state ? elapsed(state) : 0,
            submitEnabledElapsedMs: state
                ? optionalElapsed(state.submitEnabledElapsedMs) : null,
            selectionElapsedMs: state
                ? optionalElapsed(state.selectionElapsedMs) : null,
            submitCommandElapsedMs: state
                ? optionalElapsed(state.submitCommandElapsedMs) : null,
            clickEventElapsedMs: state
                ? optionalElapsed(state.clickEventElapsedMs) : null,
            formSubmitEventElapsedMs: state
                ? optionalElapsed(state.formSubmitEventElapsedMs) : null,
            transitionElapsedMs: state ? (() => {
                const candidates = [
                    state.challengeDisappearedElapsedMs,
                    state.pageHideElapsedMs,
                ].filter(Number.isFinite).map((value) => Math.max(0, value));
                return candidates.length > 0 ? Math.min(...candidates) : null;
            })() : null,
            mutationCount: state ? state.mutationCount : 0,
            selectedCount: state ? state.selectedCount : 0,
            submitInvocationCount: state
                ? state.submitInvocationCount : 0,
            commandClickEventCount: state
                ? state.commandClickEventCount : 0,
            commandFormSubmitEventCount: state
                ? state.commandFormSubmitEventCount : 0,
            commandSubmitterMatchCount: state
                ? state.commandSubmitterMatchCount : 0,
            commandFormSubmitPreventedCount: state
                ? state.commandFormSubmitPreventedCount : 0,
        };
    };
    const readStoredState = (monitorId) => {
        try {
            const raw = window.sessionStorage.getItem(storagePrefix + monitorId);
            if (!raw) return null;
            const state = JSON.parse(raw);
            return state && state.version === 1 && state.id === monitorId
                ? state : null;
        } catch (_error) {
            return null;
        }
    };
"""
_ARM_PONYCHART_RECEIPT_JS = "(() => {\n" + _PONYCHART_PAGE_HELPERS_JS + r"""
    const monitorId = __MONITOR_ID__;
    const previous = window[receiptKey];
    if (previous && typeof previous.detach === "function") previous.detach();
    const initial = inspectPage();
    const present = Boolean(resolveSubmit().element);
    const state = {
        version: 1,
        id: monitorId,
        armedAtEpochMs: Date.now(),
        initialPresent: present,
        present,
        disappeared: !present,
        mutationCount: 0,
        selectedCount: 0,
        selectionApplied: false,
        initialSubmitDisabled: initial.submitDisabled,
        initialCountdownSeconds: initial.countdownSeconds,
        initialCountdownSource: initial.countdownSource,
        initialCountdownCandidateCount: initial.countdownCandidateCount,
        countdownAtSubmitSeconds: null,
        countdownAtSubmitSource: "none",
        countdownAtSubmitCandidateCount: 0,
        submitEnabledElapsedMs: initial.submitDisabled === false ? 0 : null,
        selectionElapsedMs: null,
        submitCommandElapsedMs: null,
        clickEventElapsedMs: null,
        formSubmitEventElapsedMs: null,
        challengeDisappearedElapsedMs: !present ? 0 : null,
        pageHideElapsedMs: null,
        submitInvocationCount: 0,
        commandClickEventCount: 0,
        commandFormSubmitEventCount: 0,
        commandSubmitterMatchCount: 0,
        commandFormSubmitPreventedCount: 0,
        commandActive: false,
        storageAvailable: true,
    };
    const storageKey = storagePrefix + monitorId;
    const persist = () => {
        try {
            window.sessionStorage.setItem(storageKey, JSON.stringify(state));
            state.storageAvailable = true;
        } catch (_error) {
            state.storageAvailable = false;
        }
    };
    const refresh = (countMutation = false) => {
        if (countMutation) state.mutationCount += 1;
        state.present = Boolean(resolveSubmit().element);
        const live = inspectPage();
        if (state.submitEnabledElapsedMs === null
                && live.submitDisabled === false) {
            state.submitEnabledElapsedMs = elapsed(state);
        }
        if (!state.present && !state.disappeared) {
            state.disappeared = true;
            state.challengeDisappearedElapsedMs = elapsed(state);
        }
        persist();
    };
    const onClick = (event) => {
        const activeSubmit = resolveSubmit().element;
        if (!state.commandActive || event.target !== activeSubmit) return;
        state.commandClickEventCount += 1;
        if (state.clickEventElapsedMs === null) {
            state.clickEventElapsedMs = elapsed(state);
        }
        persist();
    };
    const onSubmit = (event) => {
        const activeSubmit = resolveSubmit().element;
        if (!state.commandActive || !activeSubmit
                || event.target !== activeSubmit.form) return;
        state.commandFormSubmitEventCount += 1;
        if (event.submitter === activeSubmit) {
            state.commandSubmitterMatchCount += 1;
        }
        if (state.formSubmitEventElapsedMs === null) {
            state.formSubmitEventElapsedMs = elapsed(state);
        }
        monitor.lastSubmitEvent = event;
        persist();
    };
    const onPageHide = () => {
        if (state.pageHideElapsedMs === null) {
            state.pageHideElapsedMs = elapsed(state);
        }
        persist();
    };
    const observer = present && document.documentElement
        ? new MutationObserver(() => refresh(true)) : null;
    if (observer) {
        observer.observe(document.documentElement, {
            childList: true,
            subtree: true,
            attributes: true,
        });
    }
    document.addEventListener("click", onClick, true);
    document.addEventListener("submit", onSubmit, true);
    window.addEventListener("pagehide", onPageHide, false);
    const monitor = {
        state,
        observer,
        refresh,
        persist,
        lastSubmitEvent: null,
        diagnostic: () => pageDiagnostic(state, state.storageAvailable),
        detach: () => {
            if (observer) observer.disconnect();
            document.removeEventListener("click", onClick, true);
            document.removeEventListener("submit", onSubmit, true);
            window.removeEventListener("pagehide", onPageHide, false);
        },
    };
    window[receiptKey] = monitor;
    persist();
    return {
        status: "armed",
        present,
        documentUrl: window.location.href,
        origin: window.location.origin,
        diagnostic: monitor.diagnostic(),
    };
})()
"""
_READ_PONYCHART_RECEIPT_JS = "(() => {\n" + _PONYCHART_PAGE_HELPERS_JS + r"""
    const monitorId = __MONITOR_ID__;
    const present = Boolean(resolveSubmit().element);
    const battlePresent = Boolean(document.getElementById("battle_main"));
    const monitor = window[receiptKey];
    const monitorFound = Boolean(
        monitor && monitor.state && monitor.state.id === monitorId
    );
    const state = monitorFound ? monitor.state : readStoredState(monitorId);
    const storageFound = !monitorFound && Boolean(state);
    if (monitorFound) monitor.refresh();
    if (state && !present && !state.disappeared) {
        state.disappeared = true;
        state.challengeDisappearedElapsedMs = elapsed(state);
        if (monitorFound) monitor.persist();
    }
    return {
        status: "observed",
        monitorFound,
        storageFound,
        present,
        battlePresent,
        documentUrl: window.location.href,
        origin: window.location.origin,
        disappeared: Boolean(state && state.disappeared),
        selectionApplied: Boolean(state && state.selectionApplied),
        diagnostic: pageDiagnostic(
            state,
            Boolean(state && state.storageAvailable)
        ),
    };
})()
"""
_SELECT_AND_SUBMIT_PONYCHART_JS = "(() => {\n" + _PONYCHART_PAGE_HELPERS_JS + r"""
    const monitorId = __MONITOR_ID__;
    const predictedLabels = __PREDICTED_LABELS__;
    const expectedLabels = __EXPECTED_LABELS__;
    const monitor = window[receiptKey];
    const result = (status, extra = {}) => ({
        status,
        ...extra,
        diagnostic: monitor && monitor.state
            ? monitor.diagnostic()
            : pageDiagnostic(readStoredState(monitorId), false),
    });
    const submit = resolveSubmit().element;
    if (!monitor || !monitor.state || monitor.state.id !== monitorId) {
        return result("monitor-missing");
    }
    monitor.refresh();
    if (!submit) return result("challenge-absent");
    if (monitor.state.submitInvocationCount !== 0) {
        return result("already-submitted");
    }

    const requested = predictedLabels.map(normalize);
    const requestedSet = new Set(requested);
    const expected = expectedLabels.map(normalize);
    const expectedSet = new Set(expected);
    if (requested.length === 0 || requestedSet.size !== requested.length
            || requested.some((name) => !expectedSet.has(name))) {
        return result("prediction-invalid");
    }
    if (safeReadyState() === "loading") {
        return result("document-not-ready");
    }

    const labelScope = inspectLabelScope();
    if (!labelScope.container) return result("labels-not-ready");
    const rows = [];
    const seen = new Set();
    const seenControls = new Set();
    for (const label of labelScope.labels) {
        const name = normalize(label.innerText || label.textContent);
        if (!name || seen.has(name)) {
            return result("label-contract-invalid");
        }
        seen.add(name);
        const control = resolveControl(label);
        if (!control) return result("label-controls-not-ready");
        if (normalize(control.type) !== "checkbox"
                || typeof control.checked !== "boolean"
                || seenControls.has(control)
                || !submit.form
                || control.form !== submit.form) {
            return result("label-contract-invalid");
        }
        if (control.disabled) return result("label-controls-not-ready");
        seenControls.add(control);
        rows.push({label, control, name});
    }
    if (rows.length < expected.length) return result("labels-not-ready");
    if (rows.length !== expected.length
            || seen.size !== expectedSet.size
            || expected.some((name) => !seen.has(name))) {
        return result("label-contract-invalid");
    }
    let selectionStarted = false;
    try {
        for (const row of rows) {
            const desired = requestedSet.has(row.name);
            if (row.control.checked !== desired) {
                selectionStarted = true;
                row.label.click();
            }
            if (row.control.checked !== desired) {
                return result("selection-unconfirmed", {selectionStarted});
            }
        }
        monitor.state.selectionApplied = true;
        monitor.state.selectedCount = requested.length;
        if (monitor.state.selectionElapsedMs === null) {
            monitor.state.selectionElapsedMs = elapsed(monitor.state);
        }
        monitor.refresh();
        const activeSubmit = resolveSubmit().element;
        if (!activeSubmit) return result("challenge-absent", {selectionStarted});
        const submitTag = safeSubmitTag(activeSubmit);
        const submitType = safeSubmitType(activeSubmit);
        const submitCaptionMatches = normalize(
            activeSubmit.value || activeSubmit.textContent
        ) === "submit answer";
        if (!["button", "input"].includes(submitTag)
                || submitType !== "submit"
                || !submitCaptionMatches
                || activeSubmit.isConnected !== true
                || !activeSubmit.form
                || rows.some((row) => row.control.form !== activeSubmit.form)
                || typeof activeSubmit.click !== "function") {
            return result("submit-contract-invalid", {selectionStarted});
        }
        if (activeSubmit.disabled
                || normalize(activeSubmit.getAttribute
                    ? activeSubmit.getAttribute("aria-disabled") : "") === "true") {
            return result("submit-not-ready", {selectionStarted});
        }
        const submitCountdown = readCountdown();
        const commandElapsedMs = elapsed(monitor.state);
        const initialRemaining = Number.isFinite(
            monitor.state.initialCountdownSeconds
        ) ? monitor.state.initialCountdownSeconds - (commandElapsedMs / 1000)
            : null;
        const remainingCandidates = [];
        if (Number.isFinite(submitCountdown.seconds)) {
            remainingCandidates.push({
                seconds: submitCountdown.seconds,
                source: submitCountdown.source,
                candidateCount: submitCountdown.candidateCount,
            });
        }
        if (Number.isFinite(initialRemaining)) {
            remainingCandidates.push({
                seconds: initialRemaining,
                source: "armed-elapsed",
                candidateCount: monitor.state.initialCountdownCandidateCount,
            });
        }
        if (remainingCandidates.length === 0) {
            monitor.state.countdownAtSubmitSeconds = null;
            monitor.state.countdownAtSubmitSource = "none";
            monitor.state.countdownAtSubmitCandidateCount =
                submitCountdown.candidateCount;
            monitor.persist();
            return result("countdown-unverified", {selectionStarted});
        }
        const effectiveRemaining = remainingCandidates.reduce(
            (best, candidate) => candidate.seconds < best.seconds
                ? candidate : best
        );
        monitor.state.countdownAtSubmitSeconds = Math.max(
            0,
            effectiveRemaining.seconds,
        );
        monitor.state.countdownAtSubmitSource = effectiveRemaining.source;
        monitor.state.countdownAtSubmitCandidateCount =
            effectiveRemaining.candidateCount;
        if (effectiveRemaining.seconds <= __PRE_EXPIRY_RESERVE_SECONDS__) {
            monitor.persist();
            return result("challenge-expiring", {selectionStarted});
        }
        monitor.state.submitInvocationCount = 1;
        monitor.state.submitCommandElapsedMs = commandElapsedMs;
        monitor.state.commandActive = true;
        monitor.persist();
        try {
            activeSubmit.click();
        } finally {
            monitor.state.commandActive = false;
            const event = monitor.lastSubmitEvent;
            if (event && event.defaultPrevented === true) {
                monitor.state.commandFormSubmitPreventedCount = 1;
            }
            monitor.persist();
        }
        if (monitor.state.commandClickEventCount !== 1
                || monitor.state.commandFormSubmitEventCount !== 1
                || monitor.state.commandSubmitterMatchCount !== 1
                || monitor.state.commandFormSubmitPreventedCount !== 0) {
            return result("submit-evidence-missing", {selectionStarted});
        }
        return result("submitted", {selectedCount: requested.length});
    } catch (_error) {
        return result("mutation-error", {selectionStarted});
    }
})()
"""


def _render_ponychart_page_script(
    template: str,
    *,
    monitor_id: str,
    predicted_labels: tuple[str, ...] | None = None,
) -> str:
    """Inject the shared DOM contract into every receipt lifecycle script."""
    rendered = (
        template.replace("__MONITOR_ID__", json.dumps(monitor_id))
        .replace("__EXPECTED_LABELS__", json.dumps(_PONYCHART_LABEL_NAMES))
        .replace(
            "__PRE_EXPIRY_RESERVE_SECONDS__",
            json.dumps(_PONYCHART_PRE_EXPIRY_RESERVE_SECONDS),
        )
    )
    if predicted_labels is not None:
        rendered = rendered.replace(
            "__PREDICTED_LABELS__", json.dumps(predicted_labels)
        )
    if "__MONITOR_ID__" in rendered or "__EXPECTED_LABELS__" in rendered:
        raise ValueError(
            "PonyChart page script has an unresolved lifecycle placeholder"
        )
    if "__PREDICTED_LABELS__" in rendered:
        raise ValueError(
            "PonyChart page script has an unresolved prediction placeholder"
        )
    if "__PRE_EXPIRY_RESERVE_SECONDS__" in rendered:
        raise ValueError("PonyChart page script has an unresolved expiry placeholder")
    return rendered


class PonyChartResolutionError(RuntimeError):
    """Raised when a detected timed challenge remains on screen."""


@dataclass(frozen=True, slots=True)
class _PonyChartImageState:
    ready: bool
    source: str
    width: float
    height: float
    rendered_width: float
    rendered_height: float


@dataclass(frozen=True, slots=True)
class _PonyChartReceiptContext:
    monitor_id: str
    document_url: str
    origin: str
    deadline: SemanticDeadline
    expiration_classification_deadline: SemanticDeadline


class _PonyChartSubmitStatus(StrEnum):
    ALREADY_SUBMITTED = "already-submitted"
    CHALLENGE_EXPIRING = "challenge-expiring"
    CHALLENGE_ABSENT = "challenge-absent"
    COUNTDOWN_UNVERIFIED = "countdown-unverified"
    DOCUMENT_NOT_READY = "document-not-ready"
    LABEL_CONTRACT_INVALID = "label-contract-invalid"
    LABEL_CONTROLS_NOT_READY = "label-controls-not-ready"
    LABELS_NOT_READY = "labels-not-ready"
    MONITOR_MISSING = "monitor-missing"
    MUTATION_ERROR = "mutation-error"
    PREDICTION_INVALID = "prediction-invalid"
    SELECTION_UNCONFIRMED = "selection-unconfirmed"
    SUBMIT_CONTRACT_INVALID = "submit-contract-invalid"
    SUBMIT_EVIDENCE_MISSING = "submit-evidence-missing"
    SUBMIT_NOT_READY = "submit-not-ready"
    SUBMITTED = "submitted"


_PONYCHART_RETRIABLE_SUBMIT_STATUSES = frozenset(
    {
        _PonyChartSubmitStatus.DOCUMENT_NOT_READY,
        _PonyChartSubmitStatus.LABEL_CONTROLS_NOT_READY,
        _PonyChartSubmitStatus.LABELS_NOT_READY,
        _PonyChartSubmitStatus.SUBMIT_NOT_READY,
    }
)
_PONYCHART_SAFE_PRE_SUBMIT_STOP_STATUSES = frozenset(
    {
        _PonyChartSubmitStatus.CHALLENGE_EXPIRING,
        _PonyChartSubmitStatus.COUNTDOWN_UNVERIFIED,
    }
)
_PONYCHART_SUBMIT_DIAGNOSTIC_CODES = {
    _PonyChartSubmitStatus.ALREADY_SUBMITTED: (
        "battle.ponychart.duplicate-submit-prevented"
    ),
    _PonyChartSubmitStatus.CHALLENGE_EXPIRING: (
        "battle.ponychart.challenge-expiring-before-submit"
    ),
    _PonyChartSubmitStatus.COUNTDOWN_UNVERIFIED: (
        "battle.ponychart.countdown-unverified-before-submit"
    ),
    _PonyChartSubmitStatus.DOCUMENT_NOT_READY: ("battle.ponychart.document-not-ready"),
    _PonyChartSubmitStatus.LABEL_CONTRACT_INVALID: (
        "battle.ponychart.label-contract-invalid"
    ),
    _PonyChartSubmitStatus.LABEL_CONTROLS_NOT_READY: (
        "battle.ponychart.label-controls-not-ready"
    ),
    _PonyChartSubmitStatus.LABELS_NOT_READY: "battle.ponychart.labels-not-ready",
    _PonyChartSubmitStatus.MONITOR_MISSING: "battle.ponychart.monitor-missing",
    _PonyChartSubmitStatus.MUTATION_ERROR: (
        "battle.ponychart.label-selection-outcome-unknown"
    ),
    _PonyChartSubmitStatus.PREDICTION_INVALID: ("battle.ponychart.prediction-invalid"),
    _PonyChartSubmitStatus.SELECTION_UNCONFIRMED: (
        "battle.ponychart.selection-unconfirmed"
    ),
    _PonyChartSubmitStatus.SUBMIT_CONTRACT_INVALID: (
        "battle.ponychart.submit-contract-invalid"
    ),
    _PonyChartSubmitStatus.SUBMIT_EVIDENCE_MISSING: (
        "battle.ponychart.submit-evidence-missing"
    ),
    _PonyChartSubmitStatus.SUBMIT_NOT_READY: ("battle.ponychart.submit-not-ready"),
}


@dataclass(frozen=True, slots=True)
class _PonyChartLabelDiagnostic:
    name: str
    control_source: str
    control_type: str
    checked: bool | None
    disabled: bool | None
    same_form: bool | None


@dataclass(frozen=True, slots=True)
class _PonyChartPageDiagnostic:
    ready_state: str
    label_count: int
    control_count: int
    checked_count: int
    submit_tag: str
    submit_type: str
    submit_source: str
    submit_connected: bool | None
    submit_caption_matches: bool | None
    submit_disabled: bool | None
    submit_aria_disabled: bool | None
    form_associated: bool
    label_scope: str
    riddle_master_present: bool
    riddle_options_present: bool
    global_label_count: int
    label_descriptors: tuple[_PonyChartLabelDiagnostic, ...]
    storage_available: bool
    initial_submit_disabled: bool | None
    countdown_seconds: float | None
    countdown_source: str
    countdown_candidate_count: int
    initial_countdown_seconds: float | None
    initial_countdown_source: str
    initial_countdown_candidate_count: int
    countdown_at_submit_seconds: float | None
    countdown_at_submit_source: str
    countdown_at_submit_candidate_count: int
    elapsed_ms: float
    submit_enabled_elapsed_ms: float | None
    selection_elapsed_ms: float | None
    submit_command_elapsed_ms: float | None
    click_event_elapsed_ms: float | None
    form_submit_event_elapsed_ms: float | None
    transition_elapsed_ms: float | None
    mutation_count: int
    selected_count: int
    submit_invocation_count: int
    command_click_event_count: int
    command_form_submit_event_count: int
    command_submitter_match_count: int
    command_form_submit_prevented_count: int

    @property
    def has_exact_submit_evidence(self) -> bool:
        return (
            self.submit_invocation_count == 1
            and self.selected_count > 0
            and self.command_click_event_count == 1
            and self.command_form_submit_event_count == 1
            and self.command_submitter_match_count == 1
            and self.command_form_submit_prevented_count == 0
            and self.selection_elapsed_ms is not None
            and self.submit_command_elapsed_ms is not None
            and self.click_event_elapsed_ms is not None
            and self.form_submit_event_elapsed_ms is not None
            and self.selection_elapsed_ms <= self.submit_command_elapsed_ms
            and self.submit_command_elapsed_ms <= self.click_event_elapsed_ms
            and self.click_event_elapsed_ms <= self.form_submit_event_elapsed_ms
        )

    @property
    def transition_follows_submit(self) -> bool:
        transition = self.transition_elapsed_ms
        form_submit = self.form_submit_event_elapsed_ms
        command = self.submit_command_elapsed_ms
        if transition is None or form_submit is None or command is None:
            return False
        transition_latency = transition - form_submit
        if transition_latency < 0:
            return False
        remaining = self.countdown_at_submit_seconds
        if remaining is None and self.initial_countdown_seconds is not None:
            remaining = self.initial_countdown_seconds - (command / 1_000)
        if remaining is None or remaining <= 0:
            return False
        expected_expiry = command + (remaining * 1_000)
        return transition <= (
            expected_expiry - _PONYCHART_EXPIRY_SAFETY_MARGIN_MILLISECONDS
        )


@dataclass(frozen=True, slots=True)
class _PonyChartReceiptObservation:
    monitor_found: bool
    storage_found: bool
    present: bool
    battle_present: bool
    document_url: str
    origin: str
    disappeared: bool
    selection_applied: bool
    diagnostic: _PonyChartPageDiagnostic

    def confirms_submission(self, context: _PonyChartReceiptContext) -> bool:
        return (
            self.origin == context.origin
            and (self.monitor_found or self.storage_found)
            and not self.present
            and self.battle_present
            and self.disappeared
            and self.selection_applied
            and self.diagnostic.has_exact_submit_evidence
            and self.diagnostic.transition_follows_submit
        )

    def confirms_natural_expiration(self, context: _PonyChartReceiptContext) -> bool:
        return (
            self.origin == context.origin
            and (self.monitor_found or self.storage_found)
            and not self.present
            and self.battle_present
            and self.disappeared
            and self.diagnostic.submit_invocation_count == 0
            and self.diagnostic.command_click_event_count == 0
            and self.diagnostic.command_form_submit_event_count == 0
        )


_PONYCHART_DIAGNOSTIC_FIELDS = {
    "readyState",
    "labelCount",
    "controlCount",
    "checkedCount",
    "submitTag",
    "submitType",
    "submitSource",
    "submitConnected",
    "submitCaptionMatches",
    "submitDisabled",
    "submitAriaDisabled",
    "formAssociated",
    "labelScope",
    "riddleMasterPresent",
    "riddleOptionsPresent",
    "globalLabelCount",
    "labelDescriptors",
    "countdownSeconds",
    "countdownSource",
    "countdownCandidateCount",
    "storageAvailable",
    "initialSubmitDisabled",
    "initialCountdownSeconds",
    "initialCountdownSource",
    "initialCountdownCandidateCount",
    "countdownAtSubmitSeconds",
    "countdownAtSubmitSource",
    "countdownAtSubmitCandidateCount",
    "elapsedMs",
    "submitEnabledElapsedMs",
    "selectionElapsedMs",
    "submitCommandElapsedMs",
    "clickEventElapsedMs",
    "formSubmitEventElapsedMs",
    "transitionElapsedMs",
    "mutationCount",
    "selectedCount",
    "submitInvocationCount",
    "commandClickEventCount",
    "commandFormSubmitEventCount",
    "commandSubmitterMatchCount",
    "commandFormSubmitPreventedCount",
}


def _bounded_nonnegative_count(
    value: object,
    *,
    field: str,
    maximum: int = 1_000_000,
) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"PonyChart diagnostic returned invalid {field}")
    return value


def _optional_boolean(value: object, *, field: str) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise ValueError(f"PonyChart diagnostic returned invalid {field}")


def _optional_nonnegative_number(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    number = _finite_number(value)
    if not 0 <= number <= 3_600_000:
        raise ValueError(f"PonyChart diagnostic returned invalid {field}")
    return number


def _diagnostic_enum(
    value: object,
    *,
    field: str,
    allowed: frozenset[str],
) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"PonyChart diagnostic returned invalid {field}")
    return value


def _decode_label_descriptors(
    value: object,
) -> tuple[_PonyChartLabelDiagnostic, ...]:
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("PonyChart diagnostic returned invalid label descriptors")
    allowed_names = frozenset(
        {"unknown", *(name.casefold() for name in _PONYCHART_LABEL_NAMES)}
    )
    expected_fields = {
        "name",
        "controlSource",
        "controlType",
        "checked",
        "disabled",
        "sameForm",
    }
    decoded: list[_PonyChartLabelDiagnostic] = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise ValueError(
                "PonyChart diagnostic returned invalid label descriptor payload"
            )
        decoded.append(
            _PonyChartLabelDiagnostic(
                name=_diagnostic_enum(
                    raw["name"],
                    field="label name",
                    allowed=allowed_names,
                ),
                control_source=_diagnostic_enum(
                    raw["controlSource"],
                    field="label control source",
                    allowed=frozenset({"label-control", "for", "nested", "none"}),
                ),
                control_type=_diagnostic_enum(
                    raw["controlType"],
                    field="label control type",
                    allowed=frozenset({"checkbox", "radio", "missing", "other"}),
                ),
                checked=_optional_boolean(raw["checked"], field="label checked state"),
                disabled=_optional_boolean(
                    raw["disabled"], field="label disabled state"
                ),
                same_form=_optional_boolean(
                    raw["sameForm"], field="label form association"
                ),
            )
        )
    return tuple(decoded)


def _decode_countdown_source(value: object, *, field: str) -> str:
    if value in {
        "armed-elapsed",
        "none",
        "riddlecounter",
        "riddlecounter-class-sprite",
        "riddlecounter-inline-sprite",
        "timer-candidate",
    }:
        assert isinstance(value, str)
        return value
    if not isinstance(value, str):
        raise ValueError(f"PonyChart diagnostic returned invalid {field}")
    source_kind, separator, token = value.partition(":")
    if (
        separator != ":"
        or source_kind not in {"id", "class"}
        or not 1 <= len(token) <= 32
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in token
        )
        or (source_kind == "class" and not ("timer" in token or "countdown" in token))
    ):
        raise ValueError(f"PonyChart diagnostic returned invalid {field}")
    return value


def _decode_page_diagnostic(raw: object) -> _PonyChartPageDiagnostic:
    if not isinstance(raw, dict) or set(raw) != _PONYCHART_DIAGNOSTIC_FIELDS:
        raise ValueError("PonyChart page diagnostic returned an invalid payload")
    ready_state = raw["readyState"]
    submit_tag = raw["submitTag"]
    submit_type = raw["submitType"]
    submit_source = raw["submitSource"]
    label_scope = raw["labelScope"]
    countdown_source = raw["countdownSource"]
    initial_countdown_source = raw["initialCountdownSource"]
    countdown_at_submit_source = raw["countdownAtSubmitSource"]
    if not isinstance(ready_state, str) or ready_state not in {
        "loading",
        "interactive",
        "complete",
        "unknown",
    }:
        raise ValueError("PonyChart diagnostic returned invalid document readiness")
    if not isinstance(submit_tag, str) or submit_tag not in {
        "button",
        "input",
        "missing",
        "other",
    }:
        raise ValueError("PonyChart diagnostic returned invalid submit tag")
    if not isinstance(submit_type, str) or submit_type not in {
        "submit",
        "button",
        "reset",
        "missing",
        "other",
    }:
        raise ValueError("PonyChart diagnostic returned invalid submit type")
    submit_source = _diagnostic_enum(
        submit_source,
        field="submit source",
        allowed=frozenset({"riddlesubmit", "caption-fallback", "ambiguous", "none"}),
    )
    label_scope = _diagnostic_enum(
        label_scope,
        field="label scope",
        allowed=frozenset({"riddler1", "riddlemaster", "global-diagnostic", "none"}),
    )
    countdown_source = _decode_countdown_source(
        countdown_source,
        field="countdown source",
    )
    initial_countdown_source = _decode_countdown_source(
        initial_countdown_source,
        field="initial countdown source",
    )
    countdown_at_submit_source = _decode_countdown_source(
        countdown_at_submit_source,
        field="submit countdown source",
    )
    form_associated = raw["formAssociated"]
    storage_available = raw["storageAvailable"]
    riddle_master_present = raw["riddleMasterPresent"]
    riddle_options_present = raw["riddleOptionsPresent"]
    if any(
        type(value) is not bool
        for value in (
            form_associated,
            storage_available,
            riddle_master_present,
            riddle_options_present,
        )
    ):
        raise ValueError("PonyChart diagnostic returned invalid capability flags")
    elapsed_ms = _finite_number(raw["elapsedMs"])
    if not 0 <= elapsed_ms <= 3_600_000:
        raise ValueError("PonyChart diagnostic returned invalid elapsed time")
    countdown_seconds = _optional_nonnegative_number(
        raw["countdownSeconds"], field="countdown"
    )
    initial_countdown_seconds = _optional_nonnegative_number(
        raw["initialCountdownSeconds"], field="initial countdown"
    )
    countdown_at_submit_seconds = _optional_nonnegative_number(
        raw["countdownAtSubmitSeconds"], field="submit countdown"
    )
    for source, seconds, field in (
        (countdown_source, countdown_seconds, "countdown"),
        (initial_countdown_source, initial_countdown_seconds, "initial countdown"),
        (
            countdown_at_submit_source,
            countdown_at_submit_seconds,
            "submit countdown",
        ),
    ):
        if (source == "none") != (seconds is None):
            raise ValueError(f"PonyChart diagnostic returned incoherent {field}")
    return _PonyChartPageDiagnostic(
        ready_state=ready_state,
        label_count=_bounded_nonnegative_count(raw["labelCount"], field="label count"),
        control_count=_bounded_nonnegative_count(
            raw["controlCount"], field="control count"
        ),
        checked_count=_bounded_nonnegative_count(
            raw["checkedCount"], field="checked count"
        ),
        submit_tag=submit_tag,
        submit_type=submit_type,
        submit_source=submit_source,
        submit_connected=_optional_boolean(
            raw["submitConnected"], field="submit connection state"
        ),
        submit_caption_matches=_optional_boolean(
            raw["submitCaptionMatches"], field="submit caption state"
        ),
        submit_disabled=_optional_boolean(
            raw["submitDisabled"], field="submit disabled state"
        ),
        submit_aria_disabled=_optional_boolean(
            raw["submitAriaDisabled"], field="submit ARIA state"
        ),
        form_associated=form_associated,
        label_scope=label_scope,
        riddle_master_present=riddle_master_present,
        riddle_options_present=riddle_options_present,
        global_label_count=_bounded_nonnegative_count(
            raw["globalLabelCount"], field="global label count"
        ),
        label_descriptors=_decode_label_descriptors(raw["labelDescriptors"]),
        storage_available=storage_available,
        initial_submit_disabled=_optional_boolean(
            raw["initialSubmitDisabled"], field="initial submit state"
        ),
        countdown_seconds=countdown_seconds,
        countdown_source=countdown_source,
        countdown_candidate_count=_bounded_nonnegative_count(
            raw["countdownCandidateCount"],
            field="countdown candidate count",
            maximum=20,
        ),
        initial_countdown_seconds=initial_countdown_seconds,
        initial_countdown_source=initial_countdown_source,
        initial_countdown_candidate_count=_bounded_nonnegative_count(
            raw["initialCountdownCandidateCount"],
            field="initial countdown candidate count",
            maximum=20,
        ),
        countdown_at_submit_seconds=countdown_at_submit_seconds,
        countdown_at_submit_source=countdown_at_submit_source,
        countdown_at_submit_candidate_count=_bounded_nonnegative_count(
            raw["countdownAtSubmitCandidateCount"],
            field="submit countdown candidate count",
            maximum=20,
        ),
        elapsed_ms=elapsed_ms,
        submit_enabled_elapsed_ms=_optional_nonnegative_number(
            raw["submitEnabledElapsedMs"], field="submit enabled time"
        ),
        selection_elapsed_ms=_optional_nonnegative_number(
            raw["selectionElapsedMs"], field="selection time"
        ),
        submit_command_elapsed_ms=_optional_nonnegative_number(
            raw["submitCommandElapsedMs"], field="submit command time"
        ),
        click_event_elapsed_ms=_optional_nonnegative_number(
            raw["clickEventElapsedMs"], field="click event time"
        ),
        form_submit_event_elapsed_ms=_optional_nonnegative_number(
            raw["formSubmitEventElapsedMs"], field="form submit event time"
        ),
        transition_elapsed_ms=_optional_nonnegative_number(
            raw["transitionElapsedMs"], field="transition time"
        ),
        mutation_count=_bounded_nonnegative_count(
            raw["mutationCount"], field="mutation count"
        ),
        selected_count=_bounded_nonnegative_count(
            raw["selectedCount"], field="selected count"
        ),
        submit_invocation_count=_bounded_nonnegative_count(
            raw["submitInvocationCount"], field="submit invocation count"
        ),
        command_click_event_count=_bounded_nonnegative_count(
            raw["commandClickEventCount"], field="click event count"
        ),
        command_form_submit_event_count=_bounded_nonnegative_count(
            raw["commandFormSubmitEventCount"], field="form submit event count"
        ),
        command_submitter_match_count=_bounded_nonnegative_count(
            raw["commandSubmitterMatchCount"], field="submitter match count"
        ),
        command_form_submit_prevented_count=_bounded_nonnegative_count(
            raw["commandFormSubmitPreventedCount"],
            field="prevented form submit count",
        ),
    )


def _log_page_diagnostic(
    *,
    phase: str,
    status: str,
    diagnostic: _PonyChartPageDiagnostic,
    warning: bool = False,
) -> None:
    label_descriptors = tuple(
        (
            descriptor.name,
            descriptor.control_source,
            descriptor.control_type,
            descriptor.checked,
            descriptor.disabled,
            descriptor.same_form,
        )
        for descriptor in diagnostic.label_descriptors
    )
    log = logger.warning if warning else logger.info
    log(
        "PonyChart page diagnostic phase=%s status=%s ready=%s "
        "label_scope=%s riddlemaster=%s riddler1=%s labels=%d "
        "global_labels=%d controls=%d checked=%d label_descriptors=%s "
        "submit=%s/%s source=%s connected=%s caption_match=%s disabled=%s "
        "aria_disabled=%s form=%s storage=%s countdown_initial=%s/%s/%d "
        "countdown_now=%s/%s/%d countdown_submit=%s/%s/%d elapsed_ms=%.0f "
        "submit_enabled_ms=%s selection_ms=%s command_ms=%s click_ms=%s "
        "form_submit_ms=%s transition_ms=%s mutations=%d selected=%d "
        "invocations=%d click_events=%d form_submit_events=%d "
        "submitter_matches=%d prevented=%d",
        phase,
        status,
        diagnostic.ready_state,
        diagnostic.label_scope,
        diagnostic.riddle_master_present,
        diagnostic.riddle_options_present,
        diagnostic.label_count,
        diagnostic.global_label_count,
        diagnostic.control_count,
        diagnostic.checked_count,
        label_descriptors,
        diagnostic.submit_tag,
        diagnostic.submit_type,
        diagnostic.submit_source,
        diagnostic.submit_connected,
        diagnostic.submit_caption_matches,
        diagnostic.submit_disabled,
        diagnostic.submit_aria_disabled,
        diagnostic.form_associated,
        diagnostic.storage_available,
        diagnostic.initial_countdown_seconds,
        diagnostic.initial_countdown_source,
        diagnostic.initial_countdown_candidate_count,
        diagnostic.countdown_seconds,
        diagnostic.countdown_source,
        diagnostic.countdown_candidate_count,
        diagnostic.countdown_at_submit_seconds,
        diagnostic.countdown_at_submit_source,
        diagnostic.countdown_at_submit_candidate_count,
        diagnostic.elapsed_ms,
        diagnostic.submit_enabled_elapsed_ms,
        diagnostic.selection_elapsed_ms,
        diagnostic.submit_command_elapsed_ms,
        diagnostic.click_event_elapsed_ms,
        diagnostic.form_submit_event_elapsed_ms,
        diagnostic.transition_elapsed_ms,
        diagnostic.mutation_count,
        diagnostic.selected_count,
        diagnostic.submit_invocation_count,
        diagnostic.command_click_event_count,
        diagnostic.command_form_submit_event_count,
        diagnostic.command_submitter_match_count,
        diagnostic.command_form_submit_prevented_count,
    )


def _decode_image_state(raw: object) -> _PonyChartImageState:
    if not isinstance(raw, dict) or set(raw) != {
        "ready",
        "source",
        "width",
        "height",
        "renderedWidth",
        "renderedHeight",
    }:
        raise ValueError("PonyChart image readiness returned an invalid payload")
    ready = raw["ready"]
    source = raw["source"]
    if type(ready) is not bool or not isinstance(source, str):
        raise ValueError("PonyChart image readiness returned invalid state fields")
    width = _finite_number(raw["width"])
    height = _finite_number(raw["height"])
    rendered_width = _finite_number(raw["renderedWidth"])
    rendered_height = _finite_number(raw["renderedHeight"])
    if min(width, height, rendered_width, rendered_height) < 0:
        raise ValueError("PonyChart image readiness returned negative geometry")
    if ready and (
        not source
        or width < _PONYCHART_MINIMUM_IMAGE_DIMENSION
        or height < _PONYCHART_MINIMUM_IMAGE_DIMENSION
    ):
        raise ValueError("PonyChart image readiness accepted placeholder geometry")
    return _PonyChartImageState(
        ready,
        source,
        width,
        height,
        rendered_width,
        rendered_height,
    )


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("PonyChart capture returned non-numeric geometry")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("PonyChart capture returned non-finite geometry")
    return number


def _positive_finite_number(value: object) -> float:
    number = _finite_number(value)
    if number <= 0:
        raise ValueError("PonyChart capture returned non-positive dimensions")
    return number


def _validate_png(image: bytes) -> tuple[int, int]:
    """Validate capture transport integrity without constraining source pixels."""

    if not image.startswith(_PNG_SIGNATURE):
        raise ValueError("PonyChart capture was not a PNG image")

    offset = len(_PNG_SIGNATURE)
    dimensions: tuple[int, int] | None = None
    saw_image_data = False
    while True:
        if offset + 12 > len(image):
            raise ValueError("PonyChart capture contained a truncated PNG chunk")

        chunk_length = struct.unpack_from(">I", image, offset)[0]
        chunk_type = image[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + chunk_length
        chunk_end = data_end + 4
        if chunk_end > len(image):
            raise ValueError("PonyChart capture contained a truncated PNG chunk")

        chunk_data = image[data_start:data_end]
        expected_crc = struct.unpack_from(">I", image, data_end)[0]
        actual_crc = binascii.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError("PonyChart capture contained an invalid PNG checksum")

        if dimensions is None:
            if chunk_type != b"IHDR" or chunk_length != 13:
                raise ValueError("PonyChart capture did not begin with PNG IHDR")
            width, height = struct.unpack_from(">II", chunk_data)
            if width <= 0 or height <= 0:
                raise ValueError("PonyChart PNG dimensions must be positive")
            dimensions = (width, height)
        elif chunk_type == b"IHDR":
            raise ValueError("PonyChart capture contained more than one PNG IHDR")

        if chunk_type == b"IDAT":
            saw_image_data = True
        elif chunk_type == b"IEND":
            if chunk_length != 0 or not saw_image_data:
                raise ValueError("PonyChart capture contained an incomplete PNG")
            if chunk_end != len(image):
                raise ValueError("PonyChart capture had data after PNG IEND")
            assert dimensions is not None
            return dimensions

        offset = chunk_end


def _decode_png_base64(payload: object) -> tuple[bytes, tuple[int, int]]:
    if not isinstance(payload, str):
        raise ValueError("PonyChart capture did not return base64 image data")
    try:
        image = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(
            "PonyChart capture returned invalid base64 image data"
        ) from error
    return image, _validate_png(image)


def _decode_png_data_url(payload: object) -> tuple[bytes, tuple[int, int]]:
    if not isinstance(payload, str):
        raise ValueError("PonyChart canvas did not return an image data URL")
    prefix, separator, encoded = payload.partition(",")
    if separator != "," or prefix.casefold() != "data:image/png;base64":
        raise ValueError("PonyChart canvas did not return a PNG data URL")
    return _decode_png_base64(encoded)


_generation_descriptor: PonyChartGenerationDescriptor | None = None
_publication_lock = threading.Lock()
_lifecycle_lock_guard = threading.Lock()
_lifecycle_lock: asyncio.Lock | None = None
_lifecycle_lock_loop: asyncio.AbstractEventLoop | None = None
_store_owner = PonyChartStoreProcessOwner.default()
_inference_owner = PonyChartInferenceOwner()
_retention_owner = PonyChartRetentionOwner()
_PONYCHART_STORE_OPERATION_DEADLINE_SECONDS = 120.0


def _published_descriptor() -> PonyChartGenerationDescriptor | None:
    with _publication_lock:
        return _generation_descriptor


def _lifecycle_lock_for_current_loop() -> asyncio.Lock:
    """Return a loop-bound lifecycle lock without binding it at import time."""

    global _lifecycle_lock, _lifecycle_lock_loop
    loop = asyncio.get_running_loop()
    with _lifecycle_lock_guard:
        if _lifecycle_lock is None or _lifecycle_lock_loop is not loop:
            if _lifecycle_lock is not None and _lifecycle_lock.locked():
                raise RuntimeError(
                    "PonyChart lifecycle is active on a different event loop"
                )
            _lifecycle_lock = asyncio.Lock()
            _lifecycle_lock_loop = loop
        return _lifecycle_lock


def _lifecycle_phase_deadline(
    expires_at: float,
    *,
    maximum: float,
    reserve: float = 0.0,
    operation: str,
) -> float:
    now = time.monotonic()
    available = expires_at - now - reserve
    if available <= 0:
        raise TimeoutError(f"PonyChart lifecycle deadline expired before {operation}")
    return min(expires_at - reserve, now + maximum)


def _publish(
    descriptor: PonyChartGenerationDescriptor,
) -> tuple[Any, ...]:
    """Commit a READY descriptor in O(1); cleanup is deliberately external."""

    global _generation_descriptor
    with _publication_lock:
        retired = _inference_owner.activate(descriptor)
        _generation_descriptor = descriptor
    return retired


async def _retire_published_workers(
    retired: tuple[Any, ...],
    *,
    expires_at: float,
) -> None:
    retirement_expires_at = _lifecycle_phase_deadline(
        expires_at,
        maximum=_PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS,
        operation="generation retirement",
    )
    if not retired:
        return
    await _inference_owner.retire_superseded_async(
        retired,
        expires_at=retirement_expires_at,
    )


def _descriptor_for_loaded(
    loaded: LoadedPonyChartGeneration,
) -> PonyChartGenerationDescriptor:
    return PonyChartGenerationDescriptor(
        generation=loaded.generation,
        model_path=loaded.model_path,
        thresholds_path=loaded.thresholds_path,
    )


async def preload_ponychart_classifier() -> None:
    """Preload artifact and inference children before browser work begins."""

    expires_at = time.monotonic() + _PONYCHART_STORE_OPERATION_DEADLINE_SECONDS
    lifecycle_lock = _lifecycle_lock_for_current_loop()
    try:
        async with asyncio.timeout_at(
            _lifecycle_phase_deadline(
                expires_at,
                maximum=_PONYCHART_STORE_OPERATION_DEADLINE_SECONDS,
                operation="lifecycle ownership",
            )
        ):
            await lifecycle_lock.acquire()
    except TimeoutError as error:
        raise TimeoutError(
            "PonyChart preload deadline expired waiting for lifecycle ownership"
        ) from error
    try:
        descriptor = _published_descriptor()
        if descriptor is None:
            loaded = await _store_owner.load_or_bootstrap(
                expires_at=_lifecycle_phase_deadline(
                    expires_at,
                    maximum=_PONYCHART_STORE_OPERATION_DEADLINE_SECONDS,
                    reserve=(
                        _PONYCHART_WORKER_PRELOAD_DEADLINE_SECONDS
                        + PROTOCOL_TIMEOUT_SECONDS
                        + _PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS
                    ),
                    operation="artifact bootstrap",
                )
            )
            descriptor = _descriptor_for_loaded(loaded)

        await _inference_owner.prepare_async(
            descriptor,
            expires_at=_lifecycle_phase_deadline(
                expires_at,
                maximum=_PONYCHART_WORKER_PRELOAD_DEADLINE_SECONDS,
                reserve=(
                    PROTOCOL_TIMEOUT_SECONDS + _PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS
                ),
                operation="inference preload",
            ),
        )
        await _retention_owner.prepare_async(
            expires_at=_lifecycle_phase_deadline(
                expires_at,
                maximum=PROTOCOL_TIMEOUT_SECONDS,
                reserve=_PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS,
                operation="retention preload",
            )
        )
        retired = _publish(descriptor)
        await _retire_published_workers(retired, expires_at=expires_at)
    finally:
        lifecycle_lock.release()


async def refresh_ponychart_classifier() -> PonyChartRefreshOutcome:
    """Refresh and atomically publish one immutable classifier generation.

    ``CURRENT`` is returned only after a successful remote metadata check (or
    a byte-identical generation commit). Transport, validation, and commit
    failures raise and leave the published predictor-generation pair intact.
    """

    expires_at = time.monotonic() + _PONYCHART_STORE_OPERATION_DEADLINE_SECONDS
    lifecycle_lock = _lifecycle_lock_for_current_loop()
    try:
        async with asyncio.timeout_at(
            _lifecycle_phase_deadline(
                expires_at,
                maximum=_PONYCHART_STORE_OPERATION_DEADLINE_SECONDS,
                operation="lifecycle ownership",
            )
        ):
            await lifecycle_lock.acquire()
    except TimeoutError as error:
        raise TimeoutError(
            "PonyChart refresh deadline expired waiting for lifecycle ownership"
        ) from error
    try:
        published = _published_descriptor()
        result = await _store_owner.refresh(
            published.generation if published is not None else None,
            expires_at=_lifecycle_phase_deadline(
                expires_at,
                maximum=_PONYCHART_STORE_OPERATION_DEADLINE_SECONDS,
                reserve=(
                    _PONYCHART_WORKER_PRELOAD_DEADLINE_SECONDS
                    + _PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS
                ),
                operation="artifact refresh",
            ),
        )
        if result.loaded is not None:
            descriptor = _descriptor_for_loaded(result.loaded)
            await _inference_owner.prepare_async(
                descriptor,
                expires_at=_lifecycle_phase_deadline(
                    expires_at,
                    maximum=_PONYCHART_WORKER_PRELOAD_DEADLINE_SECONDS,
                    reserve=_PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS,
                    operation="inference preload",
                ),
            )
            retired = _publish(descriptor)
            await _retire_published_workers(retired, expires_at=expires_at)
        return result.outcome
    finally:
        lifecycle_lock.release()


async def close_ponychart_workers(
    *,
    timeout: float | None = None,
    expires_at: float | None = None,
) -> None:
    """Reap all children under one lifecycle-serialized ownership deadline."""

    if timeout is None and expires_at is None:
        timeout = _PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS
    elif timeout is not None and expires_at is not None:
        raise TypeError(
            "PonyChart worker close requires either timeout or expires_at, not both"
        )
    if timeout is not None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= _PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS
        ):
            raise ValueError(
                "PonyChart worker close timeout must be finite and in (0, "
                f"{_PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS:g}]"
            )
        expires_at = time.monotonic() + float(timeout)
    else:
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int | float)
            or not math.isfinite(expires_at)
        ):
            raise ValueError("PonyChart worker close deadline must be finite")
        expires_at = float(expires_at)
        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart worker close deadline expired before cleanup"
            )
        if remaining > _PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS:
            raise ValueError(
                "PonyChart worker close deadline must not be more than 5 seconds away"
            )
    assert expires_at is not None
    lifecycle_lock = _lifecycle_lock_for_current_loop()

    async def close_owned() -> None:
        try:
            async with asyncio.timeout_at(
                _lifecycle_phase_deadline(
                    expires_at,
                    maximum=_PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS,
                    operation="worker close ownership",
                )
            ):
                await lifecycle_lock.acquire()
        except TimeoutError as error:
            raise PonyChartWorkerOwnershipError(
                "PonyChart close could not acquire lifecycle ownership"
            ) from error
        try:
            _lifecycle_phase_deadline(
                expires_at,
                maximum=_PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS,
                operation="worker close",
            )
            results = await asyncio.gather(
                _store_owner._close_at(expires_at=expires_at),
                _inference_owner._close_at(expires_at=expires_at),
                _retention_owner._close_at(expires_at=expires_at),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise PonyChartWorkerOwnershipError(
                    f"Failed to close {len(errors)} PonyChart background worker(s)"
                ) from errors[0]
            if time.monotonic() >= expires_at:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart workers closed after their shared deadline"
                )
        finally:
            lifecycle_lock.release()

    cleanup = asyncio.create_task(close_owned())
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


class PonyChart:
    def __init__(
        self,
        driver: HVDriver,
        *,
        image_directory: Path | None = None,
        inference_owner: PonyChartInferenceOwner | None = None,
        retention_owner: PonyChartRetentionOwner | None = None,
    ) -> None:
        self.hvdriver = driver
        self._image_directory = image_directory
        self._inference_owner = (
            inference_owner if inference_owner is not None else _inference_owner
        )
        self._retention_owner = (
            retention_owner if retention_owner is not None else _retention_owner
        )
        self._image_binding_lock = asyncio.Lock()

    @property
    def page(self) -> Any:
        return self.hvdriver.page

    async def _ensure_image_binding(self, deadline: SemanticDeadline) -> None:
        deadline.require_remaining(
            "PonyChart image readiness deadline expired before binding setup"
        )
        page = self.page
        if getattr(page, _PONYCHART_IMAGE_BINDING_PAGE_ATTRIBUTE, None) is True:
            return
        binding_lock = getattr(self, "_image_binding_lock", None)
        if not isinstance(binding_lock, asyncio.Lock):
            binding_lock = asyncio.Lock()
            self._image_binding_lock = binding_lock
        lock_remaining = deadline.require_remaining(
            "PonyChart image readiness deadline expired before binding setup"
        )
        try:
            async with asyncio.timeout(lock_remaining):
                await binding_lock.acquire()
        except TimeoutError as error:
            raise TimeoutError(
                "PonyChart image readiness deadline expired while waiting for "
                "binding setup ownership"
            ) from error
        try:
            deadline.require_remaining(
                "PonyChart image readiness deadline expired while waiting for "
                "binding setup ownership"
            )
            if getattr(page, _PONYCHART_IMAGE_BINDING_PAGE_ATTRIBUTE, None) is True:
                return
            page_enable_timeout = protocol_timeout(deadline.remaining())
            await wait_for_zendriver(
                page.send(cdp.page.enable()),
                timeout=page_enable_timeout,
                owner=page,
            )
            deadline.require_remaining(
                "PonyChart image readiness deadline expired during page binding setup"
            )
            binding_timeout = protocol_timeout(deadline.remaining())
            await wait_for_zendriver(
                page.send(cdp.runtime.add_binding(_PONYCHART_IMAGE_BINDING)),
                timeout=binding_timeout,
                owner=page,
            )
            deadline.require_remaining(
                "PonyChart image readiness deadline expired during binding setup"
            )
            setattr(page, _PONYCHART_IMAGE_BINDING_PAGE_ATTRIBUTE, True)
            deadline.require_remaining(
                "PonyChart image readiness deadline expired after binding setup"
            )
        finally:
            binding_lock.release()

    async def _wait_for_image_loaded(
        self,
        *,
        deadline: SemanticDeadline,
    ) -> _PonyChartImageState:
        """Wait for the real image's load receipt, never elapsed stability time.

        Historical PonyChart documents expose a complete 4x4 placeholder before
        replacing its source with the real challenge.  The original client used
        50 pixels as the lower bound.  A page binding observes both that source
        mutation and the image's authoritative ``load`` event; an already-loaded
        real image is accepted by the initial snapshot.
        """

        await self._ensure_image_binding(deadline)
        readiness_attempts = 0
        while True:
            remaining = deadline.require_remaining(
                "PonyChart image did not finish loading before its deadline"
            )
            token = uuid4().hex
            changed = asyncio.get_running_loop().create_future()

            async def binding_called(event: cdp.runtime.BindingCalled) -> None:
                if (
                    event.name == _PONYCHART_IMAGE_BINDING
                    and event.payload == token
                    and not changed.done()
                ):
                    changed.set_result(None)

            async def lifecycle_changed(_event: object) -> None:
                if not changed.done():
                    changed.set_result(None)

            page = self.page
            page.add_handler(cdp.runtime.BindingCalled, binding_called)
            page.add_handler(cdp.page.FrameNavigated, lifecycle_changed)
            page.add_handler(cdp.page.LoadEventFired, lifecycle_changed)
            cleanup_milliseconds = max(1, math.ceil(remaining * 1000))
            expression = (
                _ARM_PONYCHART_IMAGE_READY_JS.replace(
                    "__TOKEN__",
                    json.dumps(token),
                )
                .replace(
                    "__MINIMUM_DIMENSION__",
                    str(_PONYCHART_MINIMUM_IMAGE_DIMENSION),
                )
                .replace(
                    "__CLEANUP_MILLISECONDS__",
                    str(cleanup_milliseconds),
                )
            )
            try:
                state = _decode_image_state(
                    await wait_for_zendriver(
                        page.evaluate(expression),
                        timeout=protocol_timeout(deadline.remaining()),
                        owner=page,
                    )
                )
                readiness_attempts += 1
                logger.info(
                    "PonyChart image diagnostic phase=readiness attempt=%d "
                    "ready=%s source_present=%s natural=%gx%g rendered=%gx%g",
                    readiness_attempts,
                    state.ready,
                    bool(state.source),
                    state.width,
                    state.height,
                    state.rendered_width,
                    state.rendered_height,
                )
                deadline.require_remaining(
                    "PonyChart image readiness deadline expired during state probe"
                )
                if state.ready:
                    return state
                try:
                    async with asyncio.timeout(deadline.remaining()):
                        await changed
                except TimeoutError as error:
                    raise TimeoutError(
                        "PonyChart image did not finish loading before its deadline"
                    ) from error
            finally:
                page.remove_handlers(cdp.runtime.BindingCalled, binding_called)
                page.remove_handlers(cdp.page.FrameNavigated, lifecycle_changed)
                page.remove_handlers(cdp.page.LoadEventFired, lifecycle_changed)

    async def _capture_pony_chart_image(
        self,
        *,
        deadline: SemanticDeadline,
    ) -> bytes:
        """Capture and validate one challenge entirely in memory."""
        capture_attempts = 0
        while True:
            receipt = await self._wait_for_image_loaded(deadline=deadline)
            capture_attempts += 1
            canvas_script = (
                _CANVAS_CAPTURE_JS.replace(
                    "__EXPECTED_SOURCE__",
                    json.dumps(receipt.source),
                )
                .replace("__EXPECTED_WIDTH__", json.dumps(receipt.width))
                .replace("__EXPECTED_HEIGHT__", json.dumps(receipt.height))
                .replace(
                    "__MINIMUM_DIMENSION__",
                    str(_PONYCHART_MINIMUM_IMAGE_DIMENSION),
                )
            )
            canvas_timeout = protocol_timeout(
                min(_PONYCHART_DOM_CAPTURE_TIMEOUT_SECONDS, deadline.remaining())
            )
            canvas_result = await wait_for_zendriver(
                self.page.evaluate(canvas_script),
                timeout=canvas_timeout,
                owner=self.page,
            )
            deadline.require_remaining(
                "PonyChart image acquisition deadline expired during canvas capture"
            )
            if not isinstance(canvas_result, dict):
                raise ValueError("PonyChart canvas returned an invalid payload")

            status = canvas_result.get("status")
            if status == "stale":
                logger.info(
                    "PonyChart image diagnostic phase=capture attempt=%d "
                    "status=stale natural=%gx%g rendered=%gx%g",
                    capture_attempts,
                    receipt.width,
                    receipt.height,
                    receipt.rendered_width,
                    receipt.rendered_height,
                )
                continue
            if status == "ok":
                reported_width = _positive_finite_number(canvas_result.get("width"))
                reported_height = _positive_finite_number(canvas_result.get("height"))
                image, png_dimensions = _decode_png_data_url(
                    canvas_result.get("dataUrl")
                )
                if png_dimensions != (reported_width, reported_height) or (
                    reported_width,
                    reported_height,
                ) != (receipt.width, receipt.height):
                    raise ValueError(
                        "PonyChart canvas dimensions did not match its image receipt"
                    )
                capture_method = "canvas"
            elif status == "security-error":
                screenshot = await self._capture_pony_chart_screenshot(
                    receipt=receipt,
                    deadline=deadline,
                )
                if screenshot is None:
                    remaining = deadline.require_remaining(
                        "PonyChart screenshot geometry remained unavailable"
                    )
                    await asyncio.sleep(
                        min(_PONYCHART_IMAGE_RETRY_INTERVAL_SECONDS, remaining)
                    )
                    continue
                image = screenshot
                png_dimensions = _validate_png(image)
                capture_method = "screenshot"
            else:
                error_name = canvas_result.get("errorName")
                raise ValueError(f"PonyChart canvas capture failed: {error_name!r}")

            deadline.require_remaining(
                "PonyChart image acquisition deadline expired while validating capture"
            )
            if min(png_dimensions) < _PONYCHART_MINIMUM_IMAGE_DIMENSION:
                raise ValueError("PonyChart capture retained placeholder dimensions")
            logger.info(
                "PonyChart image diagnostic phase=capture attempt=%d status=ok "
                "method=%s natural=%gx%g rendered=%gx%g captured=%dx%d",
                capture_attempts,
                capture_method,
                receipt.width,
                receipt.height,
                receipt.rendered_width,
                receipt.rendered_height,
                png_dimensions[0],
                png_dimensions[1],
            )
            return image

    async def _capture_pony_chart_screenshot(
        self,
        *,
        receipt: _PonyChartImageState,
        deadline: SemanticDeadline,
    ) -> bytes | None:
        """Fallback for a canvas tainted synchronously by cross-origin pixels."""

        monitor_id = uuid4().hex
        rect_script = (
            _PONYCHART_IMAGE_RECT_JS.replace(
                "__MONITOR_ID__",
                json.dumps(monitor_id),
            )
            .replace("__EXPECTED_SOURCE__", json.dumps(receipt.source))
            .replace("__EXPECTED_WIDTH__", json.dumps(receipt.width))
            .replace("__EXPECTED_HEIGHT__", json.dumps(receipt.height))
            .replace(
                "__MINIMUM_DIMENSION__",
                str(_PONYCHART_MINIMUM_IMAGE_DIMENSION),
            )
        )
        rect_timeout = protocol_timeout(
            min(_PONYCHART_DOM_CAPTURE_TIMEOUT_SECONDS, deadline.remaining())
        )
        rect = await wait_for_zendriver(
            self.page.evaluate(rect_script),
            timeout=rect_timeout,
            owner=self.page,
        )
        deadline.require_remaining(
            "PonyChart image acquisition deadline expired before screenshot"
        )
        if isinstance(rect, dict) and rect.get("status") == "stale":
            return None
        if not isinstance(rect, dict) or rect.get("status") != "ok":
            raise ValueError("PonyChart image did not expose a capturable rectangle")
        x = _finite_number(rect.get("x"))
        y = _finite_number(rect.get("y"))
        width = _positive_finite_number(rect.get("width"))
        height = _positive_finite_number(rect.get("height"))
        if min(width, height) < _PONYCHART_MINIMUM_IMAGE_DIMENSION:
            logger.info(
                "PonyChart image diagnostic phase=screenshot status=rendered-placeholder "
                "natural=%gx%g rendered=%gx%g",
                receipt.width,
                receipt.height,
                width,
                height,
            )
            return None
        screenshot_timeout = protocol_timeout(
            min(_PONYCHART_SCREENSHOT_TIMEOUT_SECONDS, deadline.remaining())
        )
        encoded = await wait_for_zendriver(
            self.page.send(
                cdp.page.capture_screenshot(
                    format_="png",
                    clip=cdp.page.Viewport(
                        x=x,
                        y=y,
                        width=width,
                        height=height,
                        scale=1.0,
                    ),
                    from_surface=True,
                    capture_beyond_viewport=True,
                )
            ),
            timeout=screenshot_timeout,
            owner=self.page,
        )
        deadline.require_remaining(
            "PonyChart image acquisition deadline expired during screenshot"
        )
        image, _ = _decode_png_base64(encoded)
        verify_script = _VERIFY_PONYCHART_SCREENSHOT_JS.replace(
            "__MONITOR_ID__",
            json.dumps(monitor_id),
        )
        verify_timeout = protocol_timeout(deadline.remaining())
        verification = await wait_for_zendriver(
            self.page.evaluate(verify_script),
            timeout=verify_timeout,
            owner=self.page,
        )
        if not isinstance(verification, dict) or verification.get("status") not in {
            "stable",
            "stale",
        }:
            raise ValueError("PonyChart screenshot verification returned invalid state")
        if verification["status"] == "stale":
            return None
        deadline.require_remaining(
            "PonyChart image acquisition deadline expired while validating screenshot"
        )
        return image

    async def _retain_pony_chart_image(self, image: bytes) -> None:
        """Submit to the bounded writer queue without waiting for filesystem IO."""

        directory = self._image_directory
        if directory is None:
            return
        status = self._retention_owner.submit(image, directory)
        if status == "full":
            logger.warning(
                "PonyChart image retention queue is full; capture dropped "
                "image_bytes=%d",
                len(image),
            )
        elif status == "dead":
            logger.warning(
                "PonyChart image retention worker is unavailable; capture dropped "
                "image_bytes=%d",
                len(image),
            )

    async def _predict_labels(
        self,
        image: bytes,
        *,
        deadline: SemanticDeadline | None = None,
    ) -> tuple[str, ...]:
        """Run the local CPU inference phase before any page mutation.

        The preloaded child owns the ONNX runtime.  Its request-id response must
        arrive before one five-second semantic deadline.  Timeout or
        cancellation reaps that child before this method returns, so an
        abandoned inference can never race a later page mutation.
        """
        with _publication_lock:
            descriptor = _generation_descriptor
            if descriptor is None:
                raise RuntimeError(
                    "PonyChart classifier was not preloaded before battle startup"
                )
            lease = self._inference_owner.reserve(descriptor)
        inference_timeout = _PONYCHART_INFERENCE_DEADLINE_SECONDS
        if deadline is not None:
            inference_timeout = min(
                inference_timeout,
                deadline.require_remaining(
                    "PonyChart challenge deadline expired before inference"
                ),
            )
        labels = await self._inference_owner.predict_reserved(
            lease,
            image,
            timeout=inference_timeout,
        )
        if deadline is not None:
            deadline.require_remaining(
                "PonyChart challenge deadline expired during inference"
            )
        if not isinstance(labels, tuple) or any(
            not isinstance(label, str) or not label.strip() for label in labels
        ):
            raise ValueError("PonyChart classifier returned invalid labels")
        ordered_labels = tuple(
            sorted(labels, key=lambda label: (label.casefold(), label))
        )
        if not ordered_labels:
            raise ValueError("PonyChart classifier returned no labels")
        logger.debug("PonyChart prediction labels=%s", ordered_labels)
        return ordered_labels

    async def _arm_challenge_receipt_monitor(
        self,
        monitor_id: str,
    ) -> _PonyChartReceiptContext | None:
        active_clock = asyncio.get_running_loop().time
        arm_started_at = active_clock()
        script = _render_ponychart_page_script(
            _ARM_PONYCHART_RECEIPT_JS,
            monitor_id=monitor_id,
        )
        raw = await wait_for_zendriver(
            self.page.evaluate(script),
            timeout=PROTOCOL_TIMEOUT_SECONDS,
            owner=self.page,
        )
        arm_returned_at = active_clock()
        if (
            not isinstance(raw, dict)
            or set(raw) != {"status", "present", "documentUrl", "origin", "diagnostic"}
            or raw.get("status") != "armed"
            or type(raw.get("present")) is not bool
            or not isinstance(raw.get("documentUrl"), str)
            or not isinstance(raw.get("origin"), str)
        ):
            raise ValueError("PonyChart receipt monitor returned invalid state")
        diagnostic = _decode_page_diagnostic(raw["diagnostic"])
        _log_page_diagnostic(
            phase="arm",
            status="armed" if raw["present"] else "challenge-absent",
            diagnostic=diagnostic,
        )
        if raw["present"] is not True:
            return None
        document_url = raw["documentUrl"]
        origin = raw["origin"]
        if not document_url or not origin:
            raise ValueError("PonyChart receipt monitor returned blank identity")
        countdown = diagnostic.initial_countdown_seconds
        if countdown is None:
            challenge_budget = _PONYCHART_UNVERIFIED_TOTAL_DEADLINE_SECONDS
            expiration_budget = challenge_budget
        else:
            challenge_budget = max(
                0.001,
                countdown - _PONYCHART_PRE_EXPIRY_RESERVE_SECONDS,
            )
            expiration_budget = max(
                0.001,
                countdown + _PONYCHART_COUNTDOWN_RESOLUTION_SECONDS,
            )
        return _PonyChartReceiptContext(
            monitor_id,
            document_url,
            origin,
            SemanticDeadline(arm_started_at + challenge_budget, active_clock),
            SemanticDeadline(arm_returned_at + expiration_budget, active_clock),
        )

    async def _select_and_submit_answer(
        self,
        labels: tuple[str, ...],
        *,
        monitor_id: str,
        deadline: SemanticDeadline,
    ) -> bool:
        """Wait for a verified DOM contract, then select and submit exactly once.

        Retriable acknowledgements are guaranteed to precede submit.  Selection
        is applied conditionally, so a caller may resume an acknowledged
        pre-submit state without toggling a label off.  Once the script records a
        submit invocation, this method never evaluates it again.
        """
        script = _render_ponychart_page_script(
            _SELECT_AND_SUBMIT_PONYCHART_JS,
            monitor_id=monitor_id,
            predicted_labels=labels,
        )
        last_status: _PonyChartSubmitStatus | None = None
        while deadline.remaining() > 0:
            try:
                deadline.require_remaining(
                    "PonyChart challenge deadline expired before page mutation"
                )
                operation_timeout = protocol_timeout(
                    _PONYCHART_MUTATION_TIMEOUT_SECONDS
                )
                raw = await wait_for_zendriver(
                    self.page.evaluate(script),
                    timeout=operation_timeout,
                    owner=self.page,
                )
            except ZendriverOperationTimeout:
                # The atomic script may have reached the mutation boundary, so
                # the outcome is unknown and hbrowser has retired this browser
                # generation.  Propagate for a clean rebuild; never replay it.
                raise
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                raise BattleInterruptedError(
                    "PonyChart answer submission outcome is unknown",
                    diagnostic_code="battle.ponychart.submit-outcome-unknown",
                ) from error

            if not isinstance(raw, dict) or not isinstance(raw.get("status"), str):
                raise BattleInterruptedError(
                    "PonyChart answer submission returned an invalid acknowledgement",
                    diagnostic_code="battle.ponychart.submit-outcome-unknown",
                )
            try:
                status = _PonyChartSubmitStatus(raw["status"])
                diagnostic = _decode_page_diagnostic(raw.get("diagnostic"))
            except (TypeError, ValueError) as error:
                raise BattleInterruptedError(
                    "PonyChart answer submission returned an invalid acknowledgement",
                    diagnostic_code="battle.ponychart.submit-outcome-unknown",
                ) from error
            if status is not last_status or status in {
                _PonyChartSubmitStatus.SUBMITTED,
                _PonyChartSubmitStatus.CHALLENGE_ABSENT,
            }:
                _log_page_diagnostic(
                    phase="submit",
                    status=status.value,
                    diagnostic=diagnostic,
                )
                last_status = status

            if status is _PonyChartSubmitStatus.CHALLENGE_ABSENT:
                return False
            if status is _PonyChartSubmitStatus.SUBMITTED:
                if (
                    raw.get("selectedCount") == len(labels)
                    and diagnostic.selected_count == len(labels)
                    and diagnostic.has_exact_submit_evidence
                ):
                    return True
                status = _PonyChartSubmitStatus.SUBMIT_EVIDENCE_MISSING
            if status in _PONYCHART_RETRIABLE_SUBMIT_STATUSES:
                remaining = deadline.remaining()
                if remaining <= 0:
                    break
                await asyncio.sleep(
                    min(_PONYCHART_SUBMIT_RETRY_INTERVAL_SECONDS, remaining)
                )
                continue

            diagnostic_code = _PONYCHART_SUBMIT_DIAGNOSTIC_CODES.get(status)
            if diagnostic_code is None:
                diagnostic_code = "battle.ponychart.submit-outcome-unknown"
            raise BattleInterruptedError(
                f"PonyChart submission stopped at {status.value}",
                diagnostic_code=diagnostic_code,
            )

        timeout_status = last_status or _PonyChartSubmitStatus.DOCUMENT_NOT_READY
        diagnostic_code = _PONYCHART_SUBMIT_DIAGNOSTIC_CODES.get(
            timeout_status,
            "battle.ponychart.submit-outcome-unknown",
        )
        raise BattleInterruptedError(
            f"PonyChart did not become submit-ready: {timeout_status.value}",
            diagnostic_code=diagnostic_code,
        )

    async def _observe_challenge_receipt(
        self,
        context: _PonyChartReceiptContext,
        *,
        deadline: SemanticDeadline,
    ) -> _PonyChartReceiptObservation:
        script = _render_ponychart_page_script(
            _READ_PONYCHART_RECEIPT_JS,
            monitor_id=context.monitor_id,
        )
        operation_timeout = deadline.protocol_timeout()
        raw = await wait_for_zendriver(
            self.page.evaluate(script),
            timeout=operation_timeout,
            owner=self.page,
        )
        deadline.require_remaining(
            "PonyChart receipt deadline expired during final state probe"
        )
        expected_fields = {
            "status",
            "monitorFound",
            "storageFound",
            "present",
            "battlePresent",
            "documentUrl",
            "origin",
            "disappeared",
            "selectionApplied",
            "diagnostic",
        }
        if (
            not isinstance(raw, dict)
            or set(raw) != expected_fields
            or raw.get("status") != "observed"
            or any(
                type(raw.get(field)) is not bool
                for field in (
                    "monitorFound",
                    "storageFound",
                    "present",
                    "battlePresent",
                    "disappeared",
                    "selectionApplied",
                )
            )
            or not isinstance(raw.get("documentUrl"), str)
            or not isinstance(raw.get("origin"), str)
        ):
            raise ValueError("PonyChart receipt monitor returned invalid state")
        diagnostic = _decode_page_diagnostic(raw["diagnostic"])
        return _PonyChartReceiptObservation(
            monitor_found=raw["monitorFound"],
            storage_found=raw["storageFound"],
            present=raw["present"],
            battle_present=raw["battlePresent"],
            document_url=raw["documentUrl"],
            origin=raw["origin"],
            disappeared=raw["disappeared"],
            selection_applied=raw["selectionApplied"],
            diagnostic=diagnostic,
        )

    async def _read_challenge_receipt(
        self,
        context: _PonyChartReceiptContext,
        *,
        deadline: SemanticDeadline,
    ) -> bool:
        observation = await self._observe_challenge_receipt(
            context,
            deadline=deadline,
        )
        confirmed = observation.confirms_submission(context)
        if confirmed or not observation.present:
            _log_page_diagnostic(
                phase="receipt",
                status="submitted" if confirmed else "transition-without-confirmation",
                diagnostic=observation.diagnostic,
            )
        diagnostic = observation.diagnostic
        if (
            not confirmed
            and observation.origin == context.origin
            and (observation.monitor_found or observation.storage_found)
            and not observation.present
            and observation.battle_present
            and observation.disappeared
            and observation.selection_applied
            and diagnostic.has_exact_submit_evidence
        ):
            _log_page_diagnostic(
                phase="receipt",
                status="submit-timing-inconclusive",
                diagnostic=diagnostic,
                warning=True,
            )
            raise BattleInterruptedError(
                "PonyChart click was observed, but the return could not be "
                "distinguished from countdown expiry",
                diagnostic_code="battle.ponychart.receipt-timing-inconclusive",
            )
        return confirmed

    async def _reconcile_natural_expiration(
        self,
        context: _PonyChartReceiptContext,
        *,
        deadline: SemanticDeadline,
    ) -> bool:
        """Classify expiry read-only, bounded by the armed counter's expiry."""
        last_error: Exception | None = None
        while deadline.remaining() > 0:
            try:
                observation = await self._observe_challenge_receipt(
                    context,
                    deadline=deadline,
                )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                last_error = error
            else:
                if observation.confirms_natural_expiration(context):
                    _log_page_diagnostic(
                        phase="receipt",
                        status="natural-expiration-before-submit",
                        diagnostic=observation.diagnostic,
                        warning=True,
                    )
                    return True
                if observation.origin != context.origin:
                    return False
                if observation.diagnostic.submit_invocation_count != 0:
                    return False
                if observation.present and observation.battle_present:
                    return False
            remaining = deadline.remaining()
            if remaining <= 0:
                break
            await asyncio.sleep(
                min(_PONYCHART_SUBMIT_RETRY_INTERVAL_SECONDS, remaining)
            )
        if last_error is not None:
            logger.debug(
                "PonyChart pre-submit transition reconciliation failed "
                "error_type=%s",
                type(last_error).__name__,
            )
        return False

    async def _wait_for_challenge_receipt(
        self,
        context: _PonyChartReceiptContext,
        *,
        deadline: SemanticDeadline,
        check_interval: float = 0.25,
    ) -> None:
        last_error: Exception | None = None
        while deadline.remaining() > 0:
            try:
                if await self._read_challenge_receipt(
                    context,
                    deadline=deadline,
                ):
                    return
            except BattleInterruptedError:
                raise
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                last_error = error
            remaining = deadline.remaining()
            if remaining <= 0:
                break
            await asyncio.sleep(min(check_interval, remaining))
        resolution_error = PonyChartResolutionError(
            "PonyChart submission was not confirmed before the receipt deadline"
        )
        if last_error is not None:
            raise resolution_error from last_error
        raise resolution_error

    async def _check(self, *, deadline: SemanticDeadline | None = None) -> bool:
        timeout = (
            PROTOCOL_TIMEOUT_SECONDS
            if deadline is None
            else deadline.protocol_timeout()
        )
        present = await wait_for_zendriver(
            self.page.evaluate("""
                (() => {
                    if (document.getElementById("riddlesubmit")) return true;
                    if (!document.getElementById("riddlemaster")) return false;
                    const matches = Array.from(document.querySelectorAll(
                        'input[type="submit"], button[type="submit"]'
                    )).filter((candidate) => String(
                        candidate.value || candidate.textContent || ""
                    ).trim().toLowerCase() === "submit answer");
                    return matches.length === 1;
                })()
                """),
            timeout=timeout,
            owner=self.page,
        )
        if deadline is not None:
            deadline.require_remaining(
                "PonyChart challenge-presence deadline expired during probe"
            )
        if type(present) is not bool:
            raise ValueError("PonyChart challenge presence returned invalid state")
        return present

    async def is_present(self) -> bool:
        """Inspect challenge presence without answering or clicking it."""
        return await self._check()

    async def check(self) -> PonyChartResolutionOutcome:
        isponychart: bool = await self._check()
        if not isponychart:
            return PonyChartResolutionOutcome.NOT_PRESENT

        monitor_id = uuid4().hex
        receipt_context = await self._arm_challenge_receipt_monitor(monitor_id)
        if receipt_context is None:
            logger.warning(
                "PonyChart disappeared before the receipt monitor was armed "
                "outcome=%s diagnostic_code=%s",
                PonyChartResolutionOutcome.EXPIRED_WITHOUT_SUBMISSION.value,
                "battle.ponychart.expired-before-monitor",
            )
            return PonyChartResolutionOutcome.EXPIRED_WITHOUT_SUBMISSION
        image: bytes | None = None
        try:
            if not self.hvdriver.headless:
                notify("PonyChart", "PonyChart detected")

            try:
                image = await self._capture_pony_chart_image(
                    deadline=receipt_context.deadline
                )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                if await self._reconcile_natural_expiration(
                    receipt_context,
                    deadline=receipt_context.expiration_classification_deadline,
                ):
                    return PonyChartResolutionOutcome.EXPIRED_WITHOUT_SUBMISSION
                raise

            try:
                labels = await self._predict_labels(
                    image,
                    deadline=receipt_context.deadline,
                )
            except BattleInterruptedError:
                raise
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                logger.warning(
                    "PonyChart inference failed before page mutation "
                    "error_type=%s image_bytes=%d",
                    type(error).__name__,
                    len(image),
                )
                logger.debug(
                    "PonyChart auto-answer error detail",
                    exc_info=True,
                )
                if await self._reconcile_natural_expiration(
                    receipt_context,
                    deadline=receipt_context.expiration_classification_deadline,
                ):
                    return PonyChartResolutionOutcome.EXPIRED_WITHOUT_SUBMISSION
                raise PonyChartResolutionError(
                    "PonyChart inference failed while the challenge remained present"
                ) from error

            try:
                submitted = await self._select_and_submit_answer(
                    labels,
                    monitor_id=monitor_id,
                    deadline=receipt_context.deadline,
                )
            except BattleInterruptedError as error:
                safe_pre_submit_codes = {
                    _PONYCHART_SUBMIT_DIAGNOSTIC_CODES[status]
                    for status in (
                        _PONYCHART_RETRIABLE_SUBMIT_STATUSES
                        | _PONYCHART_SAFE_PRE_SUBMIT_STOP_STATUSES
                    )
                }
                if (
                    error.diagnostic_code in safe_pre_submit_codes
                    and await self._reconcile_natural_expiration(
                        receipt_context,
                        deadline=receipt_context.expiration_classification_deadline,
                    )
                ):
                    return PonyChartResolutionOutcome.EXPIRED_WITHOUT_SUBMISSION
                raise
            if submitted:
                await self._wait_for_challenge_receipt(
                    receipt_context,
                    deadline=receipt_context.expiration_classification_deadline,
                )
                logger.debug("PonyChart submitted resolution confirmed")
                return PonyChartResolutionOutcome.SUBMISSION_CONFIRMED
            if await self._reconcile_natural_expiration(
                receipt_context,
                deadline=receipt_context.expiration_classification_deadline,
            ):
                return PonyChartResolutionOutcome.EXPIRED_WITHOUT_SUBMISSION
            raise PonyChartResolutionError(
                "PonyChart disappeared before an answer submission was observed"
            )
        finally:
            if image is not None:
                await self._retain_pony_chart_image(image)

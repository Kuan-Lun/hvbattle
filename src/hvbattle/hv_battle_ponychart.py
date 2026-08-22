import asyncio
import base64
import binascii
import json
import math
import struct
import threading
import time
from dataclasses import dataclass
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
from .contracts import BattleInterruptedError
from .ponychart_model_store import (
    LoadedPonyChartGeneration,
    PonyChartRefreshOutcome,
)

logger = setup_logger(__name__)

_PONYCHART_DOM_CAPTURE_TIMEOUT_SECONDS = 4.0
_PONYCHART_SCREENSHOT_TIMEOUT_SECONDS = PROTOCOL_TIMEOUT_SECONDS
_PONYCHART_MUTATION_TIMEOUT_SECONDS = PROTOCOL_TIMEOUT_SECONDS
_PONYCHART_IMAGE_READY_DEADLINE_SECONDS = 10.0
_PONYCHART_INFERENCE_DEADLINE_SECONDS = 5.0
_PONYCHART_WORKER_PRELOAD_DEADLINE_SECONDS = 15.0
_PONYCHART_WORKER_CLOSE_DEADLINE_SECONDS = 5.0
_PONYCHART_RECEIPT_DEADLINE_SECONDS = 15.0
_PONYCHART_MINIMUM_IMAGE_DIMENSION = 50
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
    const ready = Boolean(
        image
        && image.complete
        && source
        && Number.isFinite(width)
        && Number.isFinite(height)
        && width >= __MINIMUM_DIMENSION__
        && height >= __MINIMUM_DIMENSION__
    );
    const result = {ready, source, width, height};
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
_ARM_PONYCHART_RECEIPT_JS = r"""
(() => {
    const monitorId = __MONITOR_ID__;
    const key = "__hvbattlePonyChartReceiptMonitor";
    const previous = window[key];
    if (previous && previous.observer
            && typeof previous.observer.disconnect === "function") {
        previous.observer.disconnect();
    }
    const present = Boolean(document.getElementById("riddlesubmit"));
    const state = {
        id: monitorId,
        documentUrl: window.location.href,
        origin: window.location.origin,
        initialPresent: present,
        present,
        disappeared: !present,
        mutationCount: 0,
        selectionApplied: false,
        submissionStarted: false,
    };
    let observer = null;
    const update = () => {
        state.mutationCount += 1;
        state.present = Boolean(document.getElementById("riddlesubmit"));
        if (!state.present) {
            state.disappeared = true;
            if (observer) observer.disconnect();
        }
    };
    if (present && document.documentElement) {
        observer = new MutationObserver(update);
        observer.observe(document.documentElement, {
            childList: true,
            subtree: true,
            attributes: true,
        });
    }
    window[key] = {state, observer};
    return {
        status: "armed",
        present,
        documentUrl: state.documentUrl,
        origin: state.origin,
    };
})()
"""
_READ_PONYCHART_RECEIPT_JS = r"""
(() => {
    const monitorId = __MONITOR_ID__;
    const key = "__hvbattlePonyChartReceiptMonitor";
    const present = Boolean(document.getElementById("riddlesubmit"));
    const battlePresent = Boolean(document.getElementById("battle_main"));
    const monitor = window[key];
    if (!monitor || !monitor.state || monitor.state.id !== monitorId) {
        return {
            status: "observed",
            monitorFound: false,
            present,
            battlePresent,
            documentUrl: window.location.href,
            origin: window.location.origin,
            disappeared: false,
            selectionApplied: false,
            submissionStarted: false,
        };
    }
    monitor.state.present = present;
    if (!present) {
        monitor.state.disappeared = true;
        if (monitor.observer) monitor.observer.disconnect();
    }
    return {
        status: "observed",
        monitorFound: true,
        present,
        battlePresent,
        documentUrl: window.location.href,
        origin: window.location.origin,
        disappeared: monitor.state.disappeared === true,
        selectionApplied: monitor.state.selectionApplied === true,
        submissionStarted: monitor.state.submissionStarted === true,
        mutationCount: monitor.state.mutationCount,
    };
})()
"""
_SELECT_AND_SUBMIT_PONYCHART_JS = r"""
(() => {
    const monitorId = __MONITOR_ID__;
    const predictedLabels = __PREDICTED_LABELS__;
    const key = "__hvbattlePonyChartReceiptMonitor";
    const monitor = window[key];
    const normalize = (value) => String(value || "").trim().toLowerCase();
    const submit = document.getElementById("riddlesubmit");
    if (!monitor || !monitor.state || monitor.state.id !== monitorId) {
        return {status: "monitor-missing"};
    }
    if (!submit) return {status: "absent"};

    const requested = predictedLabels.map(normalize);
    const requestedSet = new Set(requested);
    if (requested.length === 0 || requestedSet.size !== requested.length
            || requested.some((name) => !name)) {
        return {status: "invalid-prediction"};
    }

    const rows = [];
    const seen = new Set();
    for (const label of document.querySelectorAll("label.lc")) {
        const name = normalize(label.innerText || label.textContent);
        if (!name || seen.has(name)) {
            return {status: "ambiguous-label-mapping"};
        }
        seen.add(name);
        const control = label.control
            || (label.htmlFor ? document.getElementById(label.htmlFor) : null)
            || label.querySelector('input[type="checkbox"], input[type="radio"]');
        if (!control || typeof control.checked !== "boolean" || control.disabled) {
            return {status: "invalid-label-control"};
        }
        rows.push({label, control, name});
    }
    const missing = requested.filter((name) => !seen.has(name));
    if (missing.length !== 0) {
        return {status: "missing-labels", missingCount: missing.length};
    }
    if (typeof submit.click !== "function" || submit.disabled) {
        return {status: "submit-unavailable"};
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
                return {
                    status: "selection-unconfirmed",
                    selectionStarted,
                };
            }
        }
        monitor.state.selectionApplied = true;
        monitor.state.submissionStarted = true;
        submit.click();
        return {
            status: "submitted",
            selectedCount: requested.length,
        };
    } catch (error) {
        return {
            status: "mutation-error",
            selectionStarted,
            submissionStarted: monitor.state.submissionStarted === true,
            errorName: error && typeof error.name === "string"
                ? error.name : "Error",
        };
    }
})()
"""


class PonyChartResolutionError(RuntimeError):
    """Raised when a detected timed challenge remains on screen."""


@dataclass(frozen=True, slots=True)
class _PonyChartImageState:
    ready: bool
    source: str
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class _PonyChartReceiptContext:
    monitor_id: str
    document_url: str
    origin: str


def _decode_image_state(raw: object) -> _PonyChartImageState:
    if not isinstance(raw, dict) or set(raw) != {
        "ready",
        "source",
        "width",
        "height",
    }:
        raise ValueError("PonyChart image readiness returned an invalid payload")
    ready = raw["ready"]
    source = raw["source"]
    if type(ready) is not bool or not isinstance(source, str):
        raise ValueError("PonyChart image readiness returned invalid state fields")
    width = _finite_number(raw["width"])
    height = _finite_number(raw["height"])
    if ready and (
        not source
        or width < _PONYCHART_MINIMUM_IMAGE_DIMENSION
        or height < _PONYCHART_MINIMUM_IMAGE_DIMENSION
    ):
        raise ValueError("PonyChart image readiness accepted placeholder geometry")
    return _PonyChartImageState(ready, source, width, height)


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
        while True:
            receipt = await self._wait_for_image_loaded(deadline=deadline)
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
                continue
            if status == "ok":
                reported_width = _positive_finite_number(canvas_result.get("width"))
                reported_height = _positive_finite_number(canvas_result.get("height"))
                image, png_dimensions = _decode_png_data_url(
                    canvas_result.get("dataUrl")
                )
                if png_dimensions != (reported_width, reported_height):
                    raise ValueError(
                        "PonyChart canvas dimensions did not match the PNG header"
                    )
            elif status == "security-error":
                screenshot = await self._capture_pony_chart_screenshot(
                    receipt=receipt,
                    deadline=deadline,
                )
                if screenshot is None:
                    continue
                image = screenshot
            else:
                error_name = canvas_result.get("errorName")
                raise ValueError(f"PonyChart canvas capture failed: {error_name!r}")

            deadline.require_remaining(
                "PonyChart image acquisition deadline expired while validating capture"
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

    async def _predict_labels(self, image: bytes) -> tuple[str, ...]:
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
        labels = await self._inference_owner.predict_reserved(
            lease,
            image,
            timeout=_PONYCHART_INFERENCE_DEADLINE_SECONDS,
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
        script = _ARM_PONYCHART_RECEIPT_JS.replace(
            "__MONITOR_ID__", json.dumps(monitor_id)
        )
        raw = await wait_for_zendriver(
            self.page.evaluate(script),
            timeout=PROTOCOL_TIMEOUT_SECONDS,
            owner=self.page,
        )
        if (
            not isinstance(raw, dict)
            or raw.get("status") != "armed"
            or type(raw.get("present")) is not bool
            or not isinstance(raw.get("documentUrl"), str)
            or not isinstance(raw.get("origin"), str)
        ):
            raise ValueError("PonyChart receipt monitor returned invalid state")
        if raw["present"] is not True:
            return None
        document_url = raw["documentUrl"]
        origin = raw["origin"]
        if not document_url or not origin:
            raise ValueError("PonyChart receipt monitor returned blank identity")
        return _PonyChartReceiptContext(monitor_id, document_url, origin)

    async def _select_and_submit_answer(
        self,
        labels: tuple[str, ...],
        *,
        monitor_id: str,
        deadline: SemanticDeadline,
    ) -> bool:
        """Apply the exact label mapping and submit once in one CDP mutation."""
        script = _SELECT_AND_SUBMIT_PONYCHART_JS.replace(
            "__MONITOR_ID__", json.dumps(monitor_id)
        ).replace("__PREDICTED_LABELS__", json.dumps(labels))
        try:
            operation_timeout = protocol_timeout(
                min(
                    _PONYCHART_MUTATION_TIMEOUT_SECONDS,
                    deadline.remaining(),
                )
            )
            raw = await wait_for_zendriver(
                self.page.evaluate(script),
                timeout=operation_timeout,
                owner=self.page,
            )
        except ZendriverOperationTimeout:
            # The one atomic mutation may have reached the page.  Never replay
            # it on this browser generation when its acknowledgement is lost.
            raise
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise BattleInterruptedError(
                "PonyChart answer submission outcome is unknown",
                diagnostic_code="battle.ponychart.submit-outcome-unknown",
            ) from error

        try:
            deadline.require_remaining(
                "PonyChart receipt deadline expired during answer submission"
            )
        except TimeoutError as error:
            raise BattleInterruptedError(
                "PonyChart answer submission outcome is unknown",
                diagnostic_code="battle.ponychart.submit-outcome-unknown",
            ) from error
        if not isinstance(raw, dict) or not isinstance(raw.get("status"), str):
            raise BattleInterruptedError(
                "PonyChart answer submission returned an invalid acknowledgement",
                diagnostic_code="battle.ponychart.submit-outcome-unknown",
            )
        status = raw["status"]
        if status == "absent":
            return False
        if status == "submitted" and raw.get("selectedCount") == len(labels):
            return True
        if status in {
            "invalid-prediction",
            "ambiguous-label-mapping",
            "invalid-label-control",
            "missing-labels",
            "submit-unavailable",
            "monitor-missing",
        }:
            raise BattleInterruptedError(
                "PonyChart label mapping could not be verified before submission",
                diagnostic_code="battle.ponychart.label-mapping-unverified",
            )
        raise BattleInterruptedError(
            "PonyChart label selection outcome is unknown",
            diagnostic_code="battle.ponychart.label-selection-outcome-unknown",
        )

    async def _read_challenge_receipt(
        self,
        context: _PonyChartReceiptContext,
        *,
        deadline: SemanticDeadline,
    ) -> bool:
        script = _READ_PONYCHART_RECEIPT_JS.replace(
            "__MONITOR_ID__", json.dumps(context.monitor_id)
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
        if (
            not isinstance(raw, dict)
            or raw.get("status") != "observed"
            or type(raw.get("present")) is not bool
            or type(raw.get("monitorFound")) is not bool
            or type(raw.get("battlePresent")) is not bool
            or type(raw.get("disappeared")) is not bool
            or type(raw.get("selectionApplied")) is not bool
            or type(raw.get("submissionStarted")) is not bool
            or not isinstance(raw.get("documentUrl"), str)
            or not isinstance(raw.get("origin"), str)
        ):
            raise ValueError("PonyChart receipt monitor returned invalid state")
        if raw["monitorFound"] is True:
            return (
                raw["origin"] == context.origin
                and raw["submissionStarted"] is True
                and raw["selectionApplied"] is True
                and raw["disappeared"] is True
                and raw["present"] is False
            )
        return (
            raw["origin"] == context.origin
            and raw["battlePresent"] is True
            and raw["present"] is False
        )

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
            except ZendriverOperationTimeout:
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
            "PonyChart remained present through the answer receipt deadline"
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
            self.page.evaluate("Boolean(document.getElementById('riddlesubmit'))"),
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

    async def check(self) -> bool:
        isponychart: bool = await self._check()
        if not isponychart:
            return isponychart

        image_deadline = SemanticDeadline.after(_PONYCHART_IMAGE_READY_DEADLINE_SECONDS)
        image = await self._capture_pony_chart_image(deadline=image_deadline)

        try:
            if not self.hvdriver.headless:
                notify("PonyChart", "PonyChart detected")

            try:
                labels = await self._predict_labels(image)
            except BattleInterruptedError:
                raise
            except ZendriverOperationTimeout:
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
                if not await self._check():
                    return isponychart
                raise PonyChartResolutionError(
                    "PonyChart inference failed while the challenge remained present"
                ) from error

            monitor_id = uuid4().hex
            receipt_context = await self._arm_challenge_receipt_monitor(monitor_id)
            if receipt_context is None:
                return isponychart

            receipt_deadline = SemanticDeadline.after(
                _PONYCHART_RECEIPT_DEADLINE_SECONDS
            )
            submitted = await self._select_and_submit_answer(
                labels,
                monitor_id=monitor_id,
                deadline=receipt_deadline,
            )
            if submitted:
                await self._wait_for_challenge_receipt(
                    receipt_context,
                    deadline=receipt_deadline,
                )
            logger.debug(
                "PonyChart challenge resolution confirmed submitted=%s",
                submitted,
            )
            return isponychart
        finally:
            await self._retain_pony_chart_image(image)

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

from hvbrowser.runtime import ZendriverOperationTimeout
from zendriver import cdp

import hvbattle.hv_battle_ponychart as ponychart_module
from hvbattle._timing import SemanticDeadline
from hvbattle.contracts import BattleInterruptedError
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
    return ponychart_module._PonyChartImageState(True, source, width, height)


def _receipt_context() -> object:
    return ponychart_module._PonyChartReceiptContext(
        "monitor",
        "https://hentaiverse.org/battle",
        "https://hentaiverse.org",
    )


def _receipt_observation(**overrides: object) -> dict[str, object]:
    observed: dict[str, object] = {
        "status": "observed",
        "monitorFound": True,
        "present": False,
        "battlePresent": True,
        "documentUrl": "https://hentaiverse.org/battle",
        "origin": "https://hentaiverse.org",
        "disappeared": True,
        "selectionApplied": True,
        "submissionStarted": True,
    }
    observed.update(overrides)
    return observed


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
}

globalThis.window = globalThis;
globalThis.MutationObserver = FakeMutationObserver;
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.__hvbattle_ponychart_image_changed__ = (token) => wakeups.push(token);
let image = new FakeImage("placeholder", 4, 4, true);
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
    naturalWidth: 4, naturalHeight: 4};
const placeholder = eval(expression);
image = {...image, src: "other-challenge", currentSrc: "other-challenge",
    naturalWidth: 640, naturalHeight: 480};
const changedSource = eval(expression);
process.stdout.write(JSON.stringify({matching, placeholder, changedSource}));
"""


class PonyChartArtifactTests(unittest.IsolatedAsyncioTestCase):
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
            {"ready": True, "source": "challenge", "width": 640, "height": 480}
        )
        challenge = PonyChart(driver)

        receipt = await challenge._wait_for_image_loaded(
            deadline=SemanticDeadline.after(1.0),
        )

        self.assertEqual(receipt.source, "challenge")
        self.assertEqual((receipt.width, receipt.height), (640, 480))
        self.assertEqual(driver.page.snapshot_calls, 1)
        self.assertTrue(all(not handlers for handlers in driver.page.handlers.values()))

    async def test_complete_four_pixel_placeholder_waits_for_binding_event(
        self,
    ) -> None:
        driver = Mock(headless=True)
        driver.page = _ImageEventPage(
            {"ready": False, "source": "placeholder", "width": 4, "height": 4}
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
        }
        await driver.page.emit_image_change()
        receipt = await waiter

        self.assertEqual(receipt.source, "challenge")
        self.assertEqual(driver.page.snapshot_calls, 2)

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
        image = _png_bytes(9, 7)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            side_effect=[
                {"status": "stale", "errorName": "ImageReceiptChanged"},
                _canvas_payload(image, 9, 7),
            ]
        )
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(
            side_effect=[
                _image_receipt("first-challenge"),
                _image_receipt("second-challenge"),
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
        image = _png_bytes(3, 4)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(return_value=_canvas_payload(image, 3, 4))
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(return_value=_image_receipt())
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

        self.assertTrue(detected)
        challenge._predict_labels.assert_awaited_once_with(image)
        challenge._select_and_submit_answer.assert_awaited_once_with(
            ("Twilight",), monitor_id=ANY, deadline=ANY
        )
        challenge._wait_for_challenge_receipt.assert_awaited_once_with(
            ANY, deadline=ANY
        )
        retention.submit.assert_not_called()

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

            self.assertTrue(detected)
            challenge._predict_labels.assert_awaited_once_with(image)
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
            challenge._capture_pony_chart_image = AsyncMock(return_value=image)
            answer_error = ValueError("bad model")
            challenge._predict_labels = AsyncMock(side_effect=answer_error)

            with (
                patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
                patch.object(ponychart_module, "logger") as ponychart_logger,
            ):
                detected = await challenge.check()

            self.assertTrue(detected)
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
            image = _png_bytes(13, 7)
            driver = Mock(headless=True)
            driver.page = Mock()
            driver.page.evaluate = AsyncMock(return_value=_canvas_payload(image, 13, 7))
            driver.page.send = AsyncMock()
            challenge = PonyChart(driver, image_directory=image_directory)
            challenge._wait_for_image_loaded = AsyncMock(return_value=_image_receipt())

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
        image = _png_bytes(17, 5)
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
        stale_screenshot = _png_bytes(17, 5)
        final_image = _png_bytes(19, 6)
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
                _canvas_payload(final_image, 19, 6),
            ]
        )
        driver.page.send = AsyncMock(
            return_value=base64.b64encode(stale_screenshot).decode("ascii")
        )
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock(
            side_effect=[
                _image_receipt("first-challenge"),
                _image_receipt("second-challenge"),
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
                    "width": 9,
                    "height": 8,
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
    async def test_cpu_prediction_is_sorted_and_never_mutates_the_page(self) -> None:
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock()
        inference = Mock()
        lease = Mock()
        inference.reserve.return_value = lease
        inference.predict_reserved = AsyncMock(return_value=("Zeta", "alpha"))
        descriptor = ponychart_module.PonyChartGenerationDescriptor(
            "a" * 64,
            Path("model.onnx"),
            Path("thresholds.json"),
        )
        challenge = PonyChart(driver, inference_owner=inference)

        with patch.object(ponychart_module, "_generation_descriptor", descriptor):
            labels = await challenge._predict_labels(b"challenge")

        self.assertEqual(labels, ("alpha", "Zeta"))
        inference.reserve.assert_called_once_with(descriptor)
        inference.predict_reserved.assert_awaited_once_with(
            lease,
            b"challenge",
            timeout=ponychart_module._PONYCHART_INFERENCE_DEADLINE_SECONDS,
        )
        driver.page.evaluate.assert_not_awaited()

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
            return_value={"status": "submitted", "selectedCount": 2}
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
        self.assertIn("submit.click()", script)
        self.assertIn('"Applejack"', script)
        self.assertIn('"Twilight Sparkle"', script)

    async def test_unverified_mapping_fails_before_any_retry(self) -> None:
        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            return_value={"status": "missing-labels", "missingCount": 1}
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
            "battle.ponychart.label-mapping-unverified",
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

    async def test_receipt_requires_submission_bound_document_transition(
        self,
    ) -> None:
        cases = {
            "same-document-authoritative": (
                _receipt_observation(),
                True,
            ),
            "absence-without-submission": (
                _receipt_observation(submissionStarted=False),
                False,
            ),
            "login-navigation": (
                _receipt_observation(
                    monitorFound=False,
                    documentUrl="https://hentaiverse.org/login",
                    battlePresent=False,
                    disappeared=False,
                    selectionApplied=False,
                    submissionStarted=False,
                ),
                False,
            ),
            "wrong-origin-battle-lookalike": (
                _receipt_observation(
                    monitorFound=False,
                    documentUrl="https://example.invalid/battle",
                    origin="https://example.invalid",
                    disappeared=False,
                    selectionApplied=False,
                    submissionStarted=False,
                ),
                False,
            ),
            "same-realm-new-battle-document": (
                _receipt_observation(
                    monitorFound=False,
                    documentUrl="https://hentaiverse.org/battle?next=1",
                    disappeared=False,
                    selectionApplied=False,
                    submissionStarted=False,
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

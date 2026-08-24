import asyncio
import base64
import binascii
import json
import os
import queue
import signal
import struct
import tempfile
import time
import unittest
import zlib
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import hvbattle._ponychart_workers as workers
from hvbattle._ponychart_workers import (
    PonyChartGenerationDescriptor,
    PonyChartInferenceOwner,
    PonyChartRetentionOwner,
)

# ``spawn`` starts a fresh interpreter and can be slow on shared CI runners.
# The owner reserves one second of this total budget for deterministic cleanup,
# leaving four seconds for an expected-success worker startup and READY frame.
_TEST_WORKER_STARTUP_TIMEOUT_SECONDS = 5.0


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


def _jpeg_bytes(width: int, height: int) -> bytes:
    # Static codec-valid black frame generated offline; tests do not import cv2.
    if (width, height) != (80, 60):
        raise ValueError("JPEG fixture has fixed 80x60 dimensions")
    return base64.b64decode(
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAME"
        "BgUGBgYFBgYGBwkIBgcJBwYGCAsICQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUD"
        "AwUKBwYHCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoK"
        "CgoKCgr/wAARCAA8AFADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQF"
        "BgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEI"
        "I0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNk"
        "ZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
        "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEB"
        "AQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJB"
        "UQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZH"
        "SElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaan"
        "qKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oA"
        "DAMBAAIRAxEAPwD+f+iiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiii"
        "gAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooA/9k=",
        validate=True,
    )


def _webp_bytes(width: int, height: int) -> bytes:
    # Static codec-valid black frame generated offline; tests do not import cv2.
    if (width, height) != (72, 58):
        raise ValueError("WebP fixture has fixed 72x58 dimensions")
    return base64.b64decode(
        "UklGRiAAAABXRUJQVlA4TBQAAAAvR0AOAAcQEf0PAAnh/3wpov8pAg==",
        validate=True,
    )


def _vp8x_only_bytes(width: int, height: int) -> bytes:
    def uint24(value: int) -> bytes:
        return bytes((value & 0xFF, value >> 8 & 0xFF, value >> 16 & 0xFF))

    payload = b"\x00\x00\x00\x00" + uint24(width - 1) + uint24(height - 1)
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def _fake_inference_worker(
    connection: workers._InferenceChannel,
    descriptor: PonyChartGenerationDescriptor,
) -> None:
    connection.send(workers._WorkerReady(descriptor.generation))
    try:
        while True:
            message = connection.recv()
            if isinstance(message, workers._StopWorker):
                return
            if not isinstance(message, workers._PredictRequest):
                raise AssertionError("invalid request")
            shared = SharedMemory(name=message.shared_memory_name, track=False)
            try:
                image = bytes(shared.buf[: message.image_size])
            finally:
                shared.close()
            if image.startswith(b"hang:"):
                marker = Path(image.removeprefix(b"hang:").decode())
                marker.write_text(message.shared_memory_name, encoding="utf-8")
                while True:
                    time.sleep(1)
            if image.startswith(b"block:"):
                marker = Path(image.removeprefix(b"block:").decode())
                marker.write_text("started", encoding="utf-8")
                release = marker.with_suffix(".release")
                while not release.exists():
                    time.sleep(0.01)
            if image == b"memory-name":
                labels = (message.shared_memory_name,)
            else:
                labels = (image.decode(),)
            connection.send(workers._PredictionResult(message.request_id, labels))
    except EOFError, BrokenPipeError, OSError:
        return
    finally:
        connection.close()


def _stubborn_inference_worker(
    connection: workers._InferenceChannel,
    descriptor: PonyChartGenerationDescriptor,
) -> None:
    marker = descriptor.model_path

    def record_term(_signum: int, _frame: object) -> None:
        marker.write_text("term", encoding="utf-8")

    signal.signal(signal.SIGTERM, record_term)
    connection.send(workers._WorkerReady(descriptor.generation))
    try:
        message = connection.recv()
        if isinstance(message, workers._PredictRequest):
            while True:
                time.sleep(1)
    finally:
        connection.close()


def _never_ready_worker(
    connection: workers._InferenceChannel,
    _descriptor: PonyChartGenerationDescriptor,
) -> None:
    try:
        while True:
            time.sleep(1)
    finally:
        connection.close()


def _partial_prediction_worker(
    connection: workers._InferenceChannel,
    descriptor: PonyChartGenerationDescriptor,
) -> None:
    connection.send(workers._WorkerReady(descriptor.generation))
    try:
        request = connection.recv()
        if not isinstance(request, workers._PredictRequest):
            raise AssertionError("invalid request")
        frame = workers._inference_message_payload(
            workers._PredictionResult(request.request_id, ("late",)),
            auth_token=connection._auth_token,
        )
        connection._transport.sendall(frame[:-1])
        while True:
            time.sleep(1)
    finally:
        connection.close()


def _partial_retention_ready_worker(
    _messages: object,
    ready: workers._InferenceChannel,
) -> None:
    try:
        frame = workers._inference_message_payload(
            workers._WorkerReady("retention"),
            auth_token=ready._auth_token,
        )
        ready._transport.sendall(frame[:-1])
        while True:
            time.sleep(1)
    finally:
        ready.close()


class InferenceFramedChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        parent, child = workers.socket.socketpair()
        self.parent = workers._InferenceChannel(parent, "test-token")
        self.child = workers._InferenceChannel(child, "test-token")

    def tearDown(self) -> None:
        self.parent.close()
        self.child.close()

    def test_sync_partial_frame_cannot_outlive_absolute_deadline(self) -> None:
        frame = workers._inference_message_payload(
            workers._WorkerReady("generation"),
            auth_token="test-token",
        )
        self.child._transport.sendall(frame[:-1])
        started = time.monotonic()

        with self.assertRaises(TimeoutError):
            self.parent.receive(expires_at=started + 0.05)

        self.assertLess(time.monotonic() - started, 0.5)

    async def test_async_partial_frame_cannot_outlive_absolute_deadline(self) -> None:
        frame = workers._inference_message_payload(
            workers._PredictionResult("request", ("label",)),
            auth_token="test-token",
        )
        self.child._transport.sendall(frame[:-1])

        with self.assertRaises(TimeoutError):
            await self.parent.receive_async(expires_at=time.monotonic() + 0.05)

    async def test_complete_frame_queued_after_deadline_is_rejected(self) -> None:
        self.child.send(workers._PredictionResult("request", ("label",)))

        with self.assertRaises(TimeoutError):
            await self.parent.receive_async(expires_at=time.monotonic() - 0.01)

    async def test_decode_finishing_after_deadline_is_rejected(self) -> None:
        self.child.send(workers._PredictionResult("request", ("label",)))
        original_decode = workers._decode_inference_message

        def slow_decode(payload: bytes, *, auth_token: str) -> object:
            time.sleep(0.03)
            return original_decode(payload, auth_token=auth_token)

        with (
            patch.object(workers, "_decode_inference_message", slow_decode),
            self.assertRaises(TimeoutError),
        ):
            await self.parent.receive_async(expires_at=time.monotonic() + 0.01)

    def test_frame_from_wrong_authenticated_channel_is_rejected(self) -> None:
        frame = workers._inference_message_payload(
            workers._WorkerReady("generation"),
            auth_token="wrong-token",
        )
        self.child._transport.sendall(frame)

        with self.assertRaisesRegex(RuntimeError, "authentication"):
            self.parent.receive(expires_at=time.monotonic() + 0.1)


class PonyChartInferenceOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.owner = PonyChartInferenceOwner(
            worker_target=_fake_inference_worker,
            register_atexit=False,
        )
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.descriptor = PonyChartGenerationDescriptor(
            "a" * 64,
            root / "model.onnx",
            root / "thresholds.json",
        )

    async def asyncTearDown(self) -> None:
        try:
            await self.owner.close(timeout=2.5)
        finally:
            self.directory.cleanup()

    async def test_public_timeout_policy_rejects_over_relaxed_values(self) -> None:
        with self.assertRaisesRegex(ValueError, r"inference startup.*\(0, 15\]"):
            self.owner.prepare(self.descriptor, timeout=15.01)

        with self.assertRaisesRegex(ValueError, r"inference.*\(0, 5\]"):
            await self.owner.predict(self.descriptor, b"image", timeout=5.01)

        with self.assertRaisesRegex(ValueError, r"inference cleanup.*\(0, 5\]"):
            await self.owner.close(timeout=5.01)

        self.assertEqual(self.owner._pending_leases, {})

    def test_dead_process_reap_finishing_after_deadline_is_rejected(self) -> None:
        process = Mock()
        process.is_alive.return_value = False
        process.pid = 123

        with (
            patch.object(workers, "_remaining", side_effect=(1.0, 0.0)),
            patch.object(workers, "_join_reaped", return_value=True),
            self.assertRaisesRegex(
                workers.PonyChartWorkerOwnershipError,
                "reaped after cleanup deadline",
            ),
        ):
            workers._terminate_process_sync(process, expires_at=time.monotonic() + 1)

    async def test_async_dead_process_reap_after_deadline_is_rejected(self) -> None:
        process = Mock()
        process.is_alive.return_value = False
        process.pid = 123

        with (
            patch.object(workers, "_remaining", side_effect=(1.0, 0.0)),
            patch.object(workers, "_join_reaped", return_value=True),
            self.assertRaisesRegex(
                workers.PonyChartWorkerOwnershipError,
                "reaped after cleanup deadline",
            ),
        ):
            await workers._terminate_process_async(
                process,
                expires_at=time.monotonic() + 1,
            )

    async def test_async_empty_close_cannot_return_after_deadline(self) -> None:
        with (
            patch.object(
                workers,
                "_remaining",
                side_effect=(1.0, 1.0, 1.0, 1.0, 0.0),
            ),
            self.assertRaisesRegex(
                workers.PonyChartWorkerOwnershipError,
                "inference close completed after",
            ),
        ):
            await self.owner.close(timeout=1.0)

    def test_sync_empty_close_cannot_return_after_deadline(self) -> None:
        with (
            patch.object(
                workers,
                "_remaining",
                side_effect=(1.0, 1.0, 1.0, 1.0, 0.0),
            ),
            self.assertRaisesRegex(
                workers.PonyChartWorkerOwnershipError,
                "inference close completed after",
            ),
        ):
            self.owner.close_sync(timeout=1.0)

    def test_sync_cached_prepare_rejects_result_after_ready_deadline(self) -> None:
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        original_state_for = self.owner._state_for

        def slow_state_for(
            descriptor: PonyChartGenerationDescriptor,
        ) -> workers._InferenceProcess | None:
            time.sleep(0.06)
            return original_state_for(descriptor)

        with (
            patch.object(self.owner, "_state_for", slow_state_for),
            self.assertRaisesRegex(TimeoutError, "preload timed out"),
        ):
            self.owner.prepare(self.descriptor, timeout=0.1)

    async def test_async_cached_prepare_rejects_result_after_ready_deadline(
        self,
    ) -> None:
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        original_state_for = self.owner._state_for

        def slow_state_for(
            descriptor: PonyChartGenerationDescriptor,
        ) -> workers._InferenceProcess | None:
            time.sleep(0.06)
            return original_state_for(descriptor)

        with (
            patch.object(self.owner, "_state_for", slow_state_for),
            self.assertRaisesRegex(TimeoutError, "preload timed out"),
        ):
            await self.owner.prepare_async(
                self.descriptor,
                expires_at=time.monotonic() + 0.1,
            )

    async def test_async_second_cache_check_cannot_return_after_deadline(
        self,
    ) -> None:
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        state = self.owner._states[self.descriptor.generation]
        call_count = 0

        def state_after_first_check(
            _descriptor: PonyChartGenerationDescriptor,
        ) -> workers._InferenceProcess | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None
            time.sleep(0.06)
            return state

        with (
            patch.object(self.owner, "_state_for", state_after_first_check),
            self.assertRaisesRegex(TimeoutError, "preload timed out"),
        ):
            await self.owner.prepare_async(
                self.descriptor,
                expires_at=time.monotonic() + 0.1,
            )

        self.assertEqual(call_count, 2)

    def test_stalled_supervisor_hello_preserves_cleanup_reserve(self) -> None:
        owner = PonyChartInferenceOwner(register_atexit=False)
        process_owner = Mock()
        process_owner.target_pid = 321
        process_owner.pid = 123
        process_owner.poll.return_value = None
        process_owner.shutdown.return_value = 0
        listener = Mock()
        listener.getsockname.return_value = ("127.0.0.1", 12345)
        accepted = Mock()
        listener.accept.return_value = (accepted, ("127.0.0.1", 54321))
        connection = Mock()
        connection.receive.side_effect = TimeoutError(
            "PonyChart inference IPC receive deadline expired"
        )
        ready_expires_at = time.monotonic() + 1.0
        expires_at = time.monotonic() + 5.0

        with (
            patch.object(workers.socket, "socket", return_value=listener),
            patch.object(workers, "start_owned_process", return_value=process_owner),
            patch.object(workers, "_InferenceChannel", return_value=connection),
            self.assertRaisesRegex(TimeoutError, "receive deadline"),
        ):
            owner._spawn_supervised(
                self.descriptor,
                ready_expires_at=ready_expires_at,
                expires_at=expires_at,
            )

        connection.receive.assert_called_once_with(expires_at=ready_expires_at)
        self.assertGreater(expires_at - time.monotonic(), 3.0)
        process_owner.shutdown.assert_called_once()
        self.assertEqual(owner._owned_states, {})

    async def test_prepared_worker_matches_request_id_and_reuses_loaded_process(
        self,
    ) -> None:
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(self.descriptor)
        state = self.owner._states[self.descriptor.generation]

        first = await self.owner.predict(self.descriptor, b"first", timeout=1.0)
        second = await self.owner.predict(self.descriptor, b"second", timeout=1.0)

        self.assertEqual(first, ("first",))
        self.assertEqual(second, ("second",))
        self.assertIs(self.owner._states[self.descriptor.generation], state)

    async def test_success_unlinks_parent_owned_shared_memory(self) -> None:
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(self.descriptor)

        (shared_memory_name,) = await self.owner.predict(
            self.descriptor,
            b"memory-name",
            timeout=1.0,
        )

        with self.assertRaises(FileNotFoundError):
            SharedMemory(name=shared_memory_name, track=False)

    async def test_deadline_expired_after_image_copy_does_not_send_request(
        self,
    ) -> None:
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(self.descriptor)
        state = self.owner._states[self.descriptor.generation]
        connection = state.connection
        self.assertIsNotNone(connection)

        with (
            patch.object(workers, "_remaining", side_effect=(1.0, 0.0)),
            patch.object(connection, "send_async", new_callable=AsyncMock) as send,
            self.assertRaisesRegex(TimeoutError, "semantic deadline"),
        ):
            await self.owner.predict(self.descriptor, b"image", timeout=1.0)

        send.assert_not_called()
        self.assertFalse(state.retired)
        self.assertFalse(state.cleanup_in_progress)
        self.assertEqual(state.leases, 0)
        self.assertTrue(state.process.is_alive())

    async def test_partial_prediction_frame_times_out_and_reaps_worker(self) -> None:
        await self.owner.close(timeout=2.5)
        self.owner = PonyChartInferenceOwner(
            worker_target=_partial_prediction_worker,
            register_atexit=False,
        )
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(self.descriptor)

        with self.assertRaises(TimeoutError):
            await self.owner.predict(self.descriptor, b"image", timeout=0.2)

        self.assertNotIn(self.descriptor.generation, self.owner._states)
        self.assertEqual(self.owner._owned_states, {})

    async def test_predict_startup_lock_wait_uses_total_deadline(self) -> None:
        await self.owner.close(timeout=2.5)
        self.owner = PonyChartInferenceOwner(
            worker_target=_never_ready_worker,
            register_atexit=False,
        )
        self.owner._active_generation = self.descriptor.generation
        lease = self.owner.reserve(self.descriptor)
        await self.owner._async_start_lock.acquire()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "startup ownership"):
                await self.owner.predict_reserved(lease, b"image", timeout=0.1)
        finally:
            self.owner._async_start_lock.release()

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.owner._pending_leases, {})

    async def test_existing_worker_attach_finishing_after_deadline_is_rejected(
        self,
    ) -> None:
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(self.descriptor)
        state = self.owner._states.pop(self.descriptor.generation)
        lease = self.owner.reserve(self.descriptor)
        self.owner._states[self.descriptor.generation] = state
        original_attach = self.owner._attach_pending_lease

        def slow_attach(
            pending: workers.PonyChartInferenceLease,
            selected: workers._InferenceProcess,
        ) -> None:
            time.sleep(0.06)
            original_attach(pending, selected)

        with (
            patch.object(self.owner, "_attach_pending_lease", slow_attach),
            self.assertRaisesRegex(TimeoutError, "semantic deadline"),
        ):
            await self.owner.predict_reserved(lease, b"image", timeout=0.1)

        self.assertTrue(lease.released)
        self.assertEqual(state.leases, 0)
        self.assertEqual(self.owner._pending_leases, {})

    async def test_retirement_rejects_cleanup_finishing_after_absolute_deadline(
        self,
    ) -> None:
        async def slow_cleanup(
            _state: workers._InferenceProcess,
            *,
            expires_at: float,
        ) -> None:
            del expires_at
            await asyncio.sleep(0.03)

        with (
            patch.object(self.owner, "_cleanup_state_async", slow_cleanup),
            self.assertRaisesRegex(
                workers.PonyChartWorkerOwnershipError,
                "completed after",
            ),
        ):
            await self.owner.retire_superseded_async(
                (Mock(),),
                expires_at=time.monotonic() + 0.01,
            )

    async def test_timeout_reaps_worker_unlinks_memory_and_next_call_rebuilds(
        self,
    ) -> None:
        marker = Path(self.directory.name) / "request-started"
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(self.descriptor)

        with self.assertRaisesRegex(TimeoutError, "semantic deadline"):
            await self.owner.predict(
                self.descriptor,
                f"hang:{marker}".encode(),
                timeout=0.1,
            )

        shared_memory_name = marker.read_text(encoding="utf-8")
        with self.assertRaises(FileNotFoundError):
            SharedMemory(name=shared_memory_name, track=False)
        self.assertNotIn(self.descriptor.generation, self.owner._states)
        self.assertEqual(
            await self.owner.predict(
                self.descriptor,
                b"rebuilt",
                timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
            ),
            ("rebuilt",),
        )

    async def test_cancellation_reaps_request_worker_before_propagating(self) -> None:
        marker = Path(self.directory.name) / "cancel-request-started"
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(self.descriptor)
        prediction = asyncio.create_task(
            self.owner.predict(
                self.descriptor,
                f"hang:{marker}".encode(),
                timeout=5.0,
            )
        )
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(marker.exists())

        prediction.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await prediction

        shared_memory_name = marker.read_text(encoding="utf-8")
        with self.assertRaises(FileNotFoundError):
            SharedMemory(name=shared_memory_name, track=False)
        self.assertNotIn(self.descriptor.generation, self.owner._states)

    @unittest.skipIf(os.name == "nt", "POSIX signal escalation test")
    async def test_timeout_escalates_from_term_to_kill_then_reaps(self) -> None:
        await self.owner.close(timeout=2.5)
        marker = Path(self.directory.name) / "term-observed"
        descriptor = PonyChartGenerationDescriptor(
            "b" * 64,
            marker,
            Path(self.directory.name) / "unused-thresholds",
        )
        self.owner = PonyChartInferenceOwner(
            worker_target=_stubborn_inference_worker,
            register_atexit=False,
        )
        self.owner.prepare(
            descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(descriptor)

        with self.assertRaises(TimeoutError):
            await self.owner.predict(descriptor, b"hang", timeout=0.05)

        self.assertEqual(marker.read_text(encoding="utf-8"), "term")
        self.assertNotIn(descriptor.generation, self.owner._states)

    async def test_generation_swap_retires_old_worker_after_exact_lease(self) -> None:
        marker = Path(self.directory.name) / "old-request"
        release = marker.with_suffix(".release")
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(self.descriptor)
        old_state = self.owner._states[self.descriptor.generation]
        old_prediction = asyncio.create_task(
            self.owner.predict(
                self.descriptor,
                f"block:{marker}".encode(),
                timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
            )
        )
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(marker.exists())

        replacement = PonyChartGenerationDescriptor(
            "c" * 64,
            Path(self.directory.name) / "new-model.onnx",
            Path(self.directory.name) / "new-thresholds.json",
        )
        self.owner.prepare(
            replacement,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        retired = self.owner.activate(replacement)
        self.owner.retire_superseded(retired, timeout=1.0)

        self.assertTrue(old_state.retired)
        self.assertEqual(old_state.leases, 1)
        self.assertIn(self.descriptor.generation, self.owner._states)
        release.write_text("release", encoding="utf-8")
        self.assertEqual(await old_prediction, (f"block:{marker}",))
        self.assertNotIn(self.descriptor.generation, self.owner._states)
        self.assertEqual(
            await self.owner.predict(replacement, b"new", timeout=1.0),
            ("new",),
        )

    async def test_close_racing_active_prediction_has_one_cleanup_owner(
        self,
    ) -> None:
        marker = Path(self.directory.name) / "close-request"
        self.owner.prepare(
            self.descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(self.descriptor)
        prediction = asyncio.create_task(
            self.owner.predict(
                self.descriptor,
                f"hang:{marker}".encode(),
                timeout=2.0,
            )
        )
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(marker.exists())

        prediction_result, close_result = await asyncio.gather(
            prediction,
            self.owner.close(timeout=1.0),
            return_exceptions=True,
        )

        self.assertIsNone(close_result)
        self.assertIsInstance(prediction_result, RuntimeError)
        self.assertEqual(self.owner._owned_states, {})
        with self.assertRaisesRegex(RuntimeError, "closing"):
            self.owner.prepare(self.descriptor, timeout=1.0)

    async def test_repeated_cancellation_waits_for_kill_and_reap(self) -> None:
        await self.owner.close(timeout=2.5)
        marker = Path(self.directory.name) / "cancel-term-observed"
        descriptor = PonyChartGenerationDescriptor(
            "d" * 64,
            marker,
            Path(self.directory.name) / "unused-thresholds",
        )
        self.owner = PonyChartInferenceOwner(
            worker_target=_stubborn_inference_worker,
            register_atexit=False,
        )
        self.owner.prepare(
            descriptor,
            timeout=_TEST_WORKER_STARTUP_TIMEOUT_SECONDS,
        )
        self.owner.activate(descriptor)
        prediction = asyncio.create_task(
            self.owner.predict(descriptor, b"hang", timeout=2.0)
        )
        await asyncio.sleep(0.05)

        prediction.cancel()
        asyncio.get_running_loop().call_later(0.05, prediction.cancel)
        with self.assertRaises(asyncio.CancelledError):
            await prediction

        self.assertEqual(marker.read_text(encoding="utf-8"), "term")
        self.assertEqual(self.owner._owned_states, {})

    async def test_failed_preload_cleanup_remains_registered_for_close(self) -> None:
        await self.owner.close(timeout=2.5)
        self.owner = PonyChartInferenceOwner(
            worker_target=_never_ready_worker,
            register_atexit=False,
        )
        with (
            patch.object(
                workers,
                "_terminate_process_sync",
                side_effect=workers.PonyChartWorkerOwnershipError("not reaped"),
            ),
            self.assertRaises(workers.PonyChartWorkerOwnershipError),
        ):
            self.owner.prepare(self.descriptor, timeout=0.05)

        self.assertEqual(len(self.owner._owned_states), 1)
        await self.owner.close(timeout=1.0)
        self.assertEqual(self.owner._owned_states, {})


class PonyChartRetentionOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_retention_timeouts_are_capped_at_five_seconds(self) -> None:
        owner = PonyChartRetentionOwner(register_atexit=False)

        with self.assertRaisesRegex(ValueError, r"retention startup.*\(0, 5\]"):
            await owner.prepare_async(timeout=5.01)
        with self.assertRaisesRegex(ValueError, r"retention cleanup.*\(0, 5\]"):
            await owner.close(timeout=5.01)

    def test_partial_injected_ready_frame_times_out_and_reaps_worker(self) -> None:
        owner = PonyChartRetentionOwner(
            worker_target=_partial_retention_ready_worker,
            register_atexit=False,
        )

        with self.assertRaises(TimeoutError):
            owner.prepare(timeout=0.1)

        self.assertIsNone(owner._process)
        self.assertIsNone(owner._messages)

    def test_supervised_ready_decode_finishing_after_deadline_is_rejected(
        self,
    ) -> None:
        owner = PonyChartRetentionOwner(register_atexit=False)
        process_owner = Mock()
        process_owner.target_pid = 321
        process_owner.pid = 123
        process_owner.shutdown.return_value = 0
        transport = Mock()
        transport.getsockname.return_value = ("127.0.0.1", 12345)
        transport.recvfrom.return_value = (
            json.dumps({"type": "ready", "token": "retention-token"}).encode(),
            ("127.0.0.1", 54321),
        )
        token = Mock(hex="retention-token")
        original_loads = json.loads

        def slow_loads(payload: bytes) -> object:
            time.sleep(0.03)
            return original_loads(payload)

        with (
            patch.object(workers.socket, "socket", return_value=transport),
            patch.object(workers, "start_owned_process", return_value=process_owner),
            patch.object(workers, "uuid4", return_value=token),
            patch.object(workers.json, "loads", slow_loads),
            self.assertRaisesRegex(TimeoutError, "preload timed out"),
        ):
            owner._prepare_supervised_locked(
                ready_expires_at=time.monotonic() + 0.01,
                expires_at=time.monotonic() + 1.0,
            )

        process_owner.shutdown.assert_called_once()
        self.assertIsNone(owner._process)
        self.assertIsNone(owner._transport)
        self.assertIsNone(owner._transport_token)

    async def test_async_prepare_counts_executor_queue_wait(self) -> None:
        owner = PonyChartRetentionOwner(register_atexit=False)

        async def delayed_to_thread(
            function: object,
            /,
            *args: object,
            **kwargs: object,
        ) -> object:
            await asyncio.sleep(0.06)
            if not callable(function):
                raise TypeError("test function must be callable")
            return function(*args, **kwargs)

        with (
            patch.object(workers.asyncio, "to_thread", delayed_to_thread),
            self.assertRaisesRegex(TimeoutError, "preload timed out"),
        ):
            await owner.prepare_async(expires_at=time.monotonic() + 0.05)

        self.assertIsNone(owner._process)

    async def test_async_close_lock_wait_is_bounded_without_blocking_loop(self) -> None:
        owner = PonyChartRetentionOwner(register_atexit=False)
        owner._lock.acquire()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                workers.PonyChartWorkerOwnershipError,
                "ownership lock",
            ):
                await owner.close(timeout=0.05)
        finally:
            owner._lock.release()

        self.assertLess(time.monotonic() - started, 0.5)
        await owner.close(timeout=0.5)

    async def test_async_empty_close_cannot_return_after_deadline(self) -> None:
        owner = PonyChartRetentionOwner(register_atexit=False)

        with (
            patch.object(
                workers,
                "_remaining",
                side_effect=(1.0, 1.0, 1.0, 0.0),
            ),
            self.assertRaisesRegex(
                workers.PonyChartWorkerOwnershipError,
                "retention close completed after",
            ),
        ):
            await owner.close(timeout=1.0)

    def test_sync_close_lock_wait_is_bounded(self) -> None:
        owner = PonyChartRetentionOwner(register_atexit=False)
        owner._lock.acquire()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                workers.PonyChartWorkerOwnershipError,
                "ownership lock",
            ):
                owner.close_sync(timeout=0.05)
        finally:
            owner._lock.release()

        self.assertLess(time.monotonic() - started, 0.5)
        owner.close_sync(timeout=0.5)

    def test_sync_empty_close_cannot_return_after_deadline(self) -> None:
        owner = PonyChartRetentionOwner(register_atexit=False)

        with (
            patch.object(workers, "_remaining", side_effect=(1.0, 1.0, 0.0)),
            self.assertRaisesRegex(
                workers.PonyChartWorkerOwnershipError,
                "retention close completed after",
            ),
        ):
            owner.close_sync(timeout=1.0)

    def test_submit_does_not_wait_for_startup_ownership_lock(self) -> None:
        owner = PonyChartRetentionOwner(register_atexit=False)
        owner._lock.acquire()
        started = time.monotonic()
        try:
            status = owner.submit(b"image", Path("captures"))
        finally:
            owner._lock.release()

        self.assertEqual(status, "full")
        self.assertLess(time.monotonic() - started, 0.1)

    async def test_writer_process_persists_then_close_proves_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "captures"
            first_image = _png_bytes(64, 64)
            second_image = _jpeg_bytes(80, 60)
            owner = PonyChartRetentionOwner(register_atexit=False)
            await owner.prepare_async(timeout=3.0)

            self.assertEqual(owner.submit(first_image, destination), "queued")
            self.assertEqual(owner.submit(second_image, destination), "queued")
            await owner.close(timeout=3.0)

            self.assertIsNone(owner._process)
            self.assertIsNone(owner._messages)
            self.assertIsNone(owner._transport)
            self.assertEqual(owner._pending_shared, {})
            self.assertCountEqual(
                (path.read_bytes() for path in destination.iterdir()),
                (first_image, second_image),
            )

    def test_retained_bytes_are_exact_and_suffix_comes_from_magic(self) -> None:
        cases = (
            (_png_bytes(64, 64), ".png"),
            (_jpeg_bytes(80, 60), ".jpg"),
            (_webp_bytes(72, 58), ".webp"),
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "captures"
            for image, suffix in cases:
                with self.subTest(suffix=suffix):
                    before = (
                        set(destination.iterdir()) if destination.exists() else set()
                    )

                    workers._write_retained_capture(image, destination)

                    created = set(destination.iterdir()) - before
                    self.assertEqual(len(created), 1)
                    path = created.pop()
                    self.assertEqual(path.suffix, suffix)
                    self.assertEqual(path.read_bytes(), image)

    def test_magic_headers_without_complete_image_data_are_rejected(self) -> None:
        header = struct.pack(">IIBBBBB", 64, 64, 8, 6, 0, 0, 0)
        invalid_png = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", b"not-zlib")
            + _png_chunk(b"IEND", b"")
        )
        jpeg_without_scan = (
            b"\xff\xd8"
            + b"\xff\xc0\x00\x0b\x08"
            + struct.pack(">HH", 60, 80)
            + b"\x01\x01\x11\x00"
            + b"\xff\xd9"
        )
        cases = (
            invalid_png,
            jpeg_without_scan,
            _vp8x_only_bytes(72, 58),
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "captures"
            for image in cases:
                with self.subTest(magic=image[:12]):
                    with self.assertRaises(ValueError):
                        workers._write_retained_capture(image, destination)
            self.assertFalse(destination.exists())

    def test_full_queue_drops_immediately(self) -> None:
        owner = PonyChartRetentionOwner(register_atexit=False)
        process = Mock()
        process.is_alive.return_value = True
        messages = Mock()
        messages.put_nowait.side_effect = queue.Full
        owner._process = process
        owner._messages = messages

        started = time.monotonic()
        status = owner.submit(b"image", Path("captures"))

        self.assertEqual(status, "full")
        self.assertLess(time.monotonic() - started, 0.1)
        messages.put_nowait.assert_called_once()


if __name__ == "__main__":
    unittest.main()

"""Owned background processes for PonyChart inference and image retention."""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import math
import multiprocessing
import queue
import select
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from typing import Any, Final, Literal, Protocol
from uuid import uuid4

from hvbrowser.runtime import (
    OwnedProcess,
    ProcessOwnershipError,
    start_owned_process,
)

from ._ponychart_image import inspect_ponychart_image

_INFERENCE_TERMINATE_GRACE_SECONDS: Final = 0.25
_INFERENCE_KILL_GRACE_SECONDS: Final = 0.75
_INFERENCE_STARTUP_TIMEOUT_SECONDS: Final = 15.0
_INFERENCE_TIMEOUT_SECONDS: Final = 5.0
_WORKER_CLEANUP_TIMEOUT_SECONDS: Final = 5.0
_RETENTION_STARTUP_TIMEOUT_SECONDS: Final = 5.0
_RETENTION_DRAIN_GRACE_SECONDS: Final = 2.0
_RETENTION_QUEUE_CAPACITY: Final = 8
_IPC_RECONCILIATION_INTERVAL_SECONDS: Final = 0.05
_INFERENCE_IPC_HEADER: Final = struct.Struct("!I")
_INFERENCE_IPC_MAX_PAYLOAD_BYTES: Final = 64 * 1024
_STOP: Final = "stop"

logger = logging.getLogger(__name__)


class PonyChartWorkerOwnershipError(RuntimeError):
    """A PonyChart child process could not be proven reaped."""


@dataclass(frozen=True, slots=True)
class PonyChartGenerationDescriptor:
    """Pickle-safe immutable inputs required to load one classifier generation."""

    generation: str
    model_path: Path
    thresholds_path: Path


@dataclass(frozen=True, slots=True)
class _WorkerReady:
    generation: str


@dataclass(frozen=True, slots=True)
class _WorkerHello:
    token: str


@dataclass(frozen=True, slots=True)
class _PredictRequest:
    request_id: str
    shared_memory_name: str
    image_size: int


@dataclass(frozen=True, slots=True)
class _PredictionResult:
    request_id: str
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _WorkerFailure:
    request_id: str | None
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class _StopWorker:
    command: Literal["stop"] = _STOP


def _inference_message_payload(message: object, *, auth_token: str) -> bytes:
    if isinstance(message, _WorkerHello):
        if message.token != auth_token:
            raise RuntimeError("PonyChart inference IPC authentication mismatch")
        payload: dict[str, object] = {"type": "hello", "token": auth_token}
    elif isinstance(message, _WorkerReady):
        payload = {
            "type": "ready",
            "token": auth_token,
            "generation": message.generation,
        }
    elif isinstance(message, _PredictRequest):
        payload = {
            "type": "predict",
            "token": auth_token,
            "requestId": message.request_id,
            "sharedMemory": message.shared_memory_name,
            "imageSize": message.image_size,
        }
    elif isinstance(message, _PredictionResult):
        payload = {
            "type": "prediction",
            "token": auth_token,
            "requestId": message.request_id,
            "labels": list(message.labels),
        }
    elif isinstance(message, _WorkerFailure):
        payload = {
            "type": "failure",
            "token": auth_token,
            "requestId": message.request_id,
            "errorType": message.error_type,
            "message": message.message,
        }
    elif isinstance(message, _StopWorker):
        payload = {"type": "stop", "token": auth_token}
    else:
        raise TypeError("Unsupported PonyChart inference IPC message")
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _INFERENCE_IPC_MAX_PAYLOAD_BYTES:
        raise RuntimeError("PonyChart inference IPC payload is too large")
    return _INFERENCE_IPC_HEADER.pack(len(encoded)) + encoded


def _decode_inference_message(payload: bytes, *, auth_token: str) -> object:
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("PonyChart inference IPC returned invalid JSON") from error
    if not isinstance(raw, dict) or raw.get("token") != auth_token:
        raise RuntimeError("PonyChart inference IPC authentication failed")
    message_type = raw.get("type")
    if message_type == "hello" and set(raw) == {"type", "token"}:
        return _WorkerHello(auth_token)
    if message_type == "ready" and set(raw) == {"type", "token", "generation"}:
        generation = raw.get("generation")
        if isinstance(generation, str) and generation:
            return _WorkerReady(generation)
    if message_type == "predict" and set(raw) == {
        "type",
        "token",
        "requestId",
        "sharedMemory",
        "imageSize",
    }:
        request_id = raw.get("requestId")
        shared_memory_name = raw.get("sharedMemory")
        image_size = raw.get("imageSize")
        if (
            isinstance(request_id, str)
            and request_id
            and isinstance(shared_memory_name, str)
            and shared_memory_name
            and isinstance(image_size, int)
            and not isinstance(image_size, bool)
            and image_size >= 0
        ):
            return _PredictRequest(request_id, shared_memory_name, image_size)
    if message_type == "prediction" and set(raw) == {
        "type",
        "token",
        "requestId",
        "labels",
    }:
        request_id = raw.get("requestId")
        labels = raw.get("labels")
        if (
            isinstance(request_id, str)
            and request_id
            and isinstance(labels, list)
            and all(isinstance(label, str) for label in labels)
        ):
            return _PredictionResult(request_id, tuple(labels))
    if message_type == "failure" and set(raw) == {
        "type",
        "token",
        "requestId",
        "errorType",
        "message",
    }:
        request_id = raw.get("requestId")
        error_type = raw.get("errorType")
        message = raw.get("message")
        if (
            (request_id is None or isinstance(request_id, str))
            and isinstance(error_type, str)
            and error_type
            and isinstance(message, str)
        ):
            return _WorkerFailure(request_id, error_type, message)
    if message_type == "stop" and set(raw) == {"type", "token"}:
        return _StopWorker()
    raise RuntimeError("PonyChart inference IPC returned an invalid message")


def _require_inference_ipc_time(expires_at: float, *, operation: str) -> float:
    remaining = expires_at - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"PonyChart inference {operation} deadline expired")
    return remaining


class _InferenceChannel:
    """Authenticated framed socket channel with absolute-deadline receives."""

    def __init__(self, transport: socket.socket, auth_token: str) -> None:
        if not isinstance(auth_token, str) or not auth_token:
            raise ValueError("PonyChart inference IPC token is invalid")
        self._transport = transport
        self._auth_token = auth_token

    def fileno(self) -> int:
        return self._transport.fileno()

    def close(self) -> None:
        self._transport.close()

    def send(self, message: object, *, expires_at: float | None = None) -> None:
        frame = _inference_message_payload(message, auth_token=self._auth_token)
        self._transport.setblocking(False)
        view = memoryview(frame)
        while view:
            timeout = (
                None
                if expires_at is None
                else _require_inference_ipc_time(
                    expires_at,
                    operation="IPC send",
                )
            )
            try:
                _, writable, _ = select.select([], [self._transport], [], timeout)
            except InterruptedError:
                continue
            if not writable:
                raise TimeoutError("PonyChart inference IPC send deadline expired")
            if expires_at is not None:
                _require_inference_ipc_time(expires_at, operation="IPC send")
            try:
                sent = self._transport.send(view)
            except BlockingIOError:
                continue
            if sent <= 0:
                raise EOFError("PonyChart inference IPC channel closed during send")
            view = view[sent:]
        if expires_at is not None:
            _require_inference_ipc_time(expires_at, operation="IPC send")

    async def send_async(self, message: object, *, expires_at: float) -> None:
        frame = _inference_message_payload(message, auth_token=self._auth_token)
        _require_inference_ipc_time(
            expires_at,
            operation="IPC send",
        )
        self._transport.setblocking(False)
        try:
            async with asyncio.timeout_at(expires_at):
                await asyncio.get_running_loop().sock_sendall(self._transport, frame)
        except TimeoutError as error:
            raise TimeoutError(
                "PonyChart inference IPC send deadline expired"
            ) from error
        _require_inference_ipc_time(expires_at, operation="IPC send")

    def _receive_exact(self, size: int, *, expires_at: float | None) -> bytes:
        content = bytearray()
        self._transport.setblocking(False)
        while len(content) < size:
            timeout = (
                None
                if expires_at is None
                else _require_inference_ipc_time(
                    expires_at,
                    operation="IPC receive",
                )
            )
            try:
                readable, _, _ = select.select([self._transport], [], [], timeout)
            except InterruptedError:
                continue
            if not readable:
                raise TimeoutError("PonyChart inference IPC receive deadline expired")
            if expires_at is not None:
                _require_inference_ipc_time(expires_at, operation="IPC receive")
            try:
                chunk = self._transport.recv(size - len(content))
            except BlockingIOError:
                continue
            if not chunk:
                raise EOFError("PonyChart inference IPC channel closed")
            content.extend(chunk)
        if expires_at is not None:
            _require_inference_ipc_time(expires_at, operation="IPC receive")
        return bytes(content)

    async def _receive_exact_async(self, size: int, *, expires_at: float) -> bytes:
        content = bytearray()
        loop = asyncio.get_running_loop()
        self._transport.setblocking(False)
        while len(content) < size:
            _require_inference_ipc_time(
                expires_at,
                operation="IPC receive",
            )
            try:
                async with asyncio.timeout_at(expires_at):
                    chunk = await loop.sock_recv(
                        self._transport,
                        size - len(content),
                    )
            except TimeoutError as error:
                raise TimeoutError(
                    "PonyChart inference IPC receive deadline expired"
                ) from error
            if not chunk:
                raise EOFError("PonyChart inference IPC channel closed")
            content.extend(chunk)
        _require_inference_ipc_time(expires_at, operation="IPC receive")
        return bytes(content)

    def receive(self, *, expires_at: float | None = None) -> object:
        header = self._receive_exact(
            _INFERENCE_IPC_HEADER.size,
            expires_at=expires_at,
        )
        (payload_size,) = _INFERENCE_IPC_HEADER.unpack(header)
        if payload_size > _INFERENCE_IPC_MAX_PAYLOAD_BYTES:
            raise RuntimeError("PonyChart inference IPC payload is too large")
        payload = self._receive_exact(payload_size, expires_at=expires_at)
        message = _decode_inference_message(payload, auth_token=self._auth_token)
        if expires_at is not None:
            _require_inference_ipc_time(expires_at, operation="IPC receive")
        return message

    def recv(self) -> object:
        """Blocking child-side receive for injected worker targets."""

        return self.receive()

    async def receive_async(self, *, expires_at: float) -> object:
        header = await self._receive_exact_async(
            _INFERENCE_IPC_HEADER.size,
            expires_at=expires_at,
        )
        (payload_size,) = _INFERENCE_IPC_HEADER.unpack(header)
        if payload_size > _INFERENCE_IPC_MAX_PAYLOAD_BYTES:
            raise RuntimeError("PonyChart inference IPC payload is too large")
        payload = await self._receive_exact_async(payload_size, expires_at=expires_at)
        message = _decode_inference_message(payload, auth_token=self._auth_token)
        _require_inference_ipc_time(expires_at, operation="IPC receive")
        return message


class _ClassifierResult(Protocol):
    @property
    def labels(self) -> frozenset[str]: ...


class _Classifier(Protocol):
    def load(self) -> None: ...

    def predict_bytes(self, image: bytes) -> _ClassifierResult: ...


type _InferenceWorkerTarget = Callable[
    [_InferenceChannel, PonyChartGenerationDescriptor],
    None,
]
type _RetentionWorkerTarget = Callable[[Queue[object], _InferenceChannel], None]


def _load_classifier(descriptor: PonyChartGenerationDescriptor) -> _Classifier:
    """Import the production classifier only inside the spawned worker."""

    from ponychart_classifier import PonyChartClassifier

    classifier: _Classifier = PonyChartClassifier(
        model_path=descriptor.model_path,
        thresholds_path=descriptor.thresholds_path,
    )
    classifier.load()
    return classifier


def _validated_labels(result: _ClassifierResult) -> tuple[str, ...]:
    labels = result.labels
    if not isinstance(labels, frozenset) or any(
        not isinstance(label, str) or not label.strip() for label in labels
    ):
        raise ValueError("PonyChart classifier returned invalid labels")
    ordered = tuple(sorted(labels, key=lambda label: (label.casefold(), label)))
    if not ordered:
        raise ValueError("PonyChart classifier returned no labels")
    return ordered


def _inference_worker_main(
    connection: _InferenceChannel,
    descriptor: PonyChartGenerationDescriptor,
) -> None:
    """Load one immutable model once, then serve request-id-bound predictions."""

    try:
        try:
            classifier = _load_classifier(descriptor)
        except BaseException as error:
            connection.send(
                _WorkerFailure(
                    request_id=None,
                    error_type=type(error).__name__,
                    message=str(error),
                )
            )
            return
        connection.send(_WorkerReady(descriptor.generation))
        while True:
            message = connection.receive()
            if isinstance(message, _StopWorker):
                return
            if not isinstance(message, _PredictRequest):
                raise RuntimeError("PonyChart inference worker received invalid IPC")
            shared = SharedMemory(name=message.shared_memory_name, track=False)
            try:
                buffer = shared.buf
                if buffer is None:
                    raise RuntimeError("PonyChart shared image memory is unavailable")
                image = bytes(buffer[: message.image_size])
            finally:
                shared.close()
            try:
                labels = _validated_labels(classifier.predict_bytes(image))
            except BaseException as error:
                connection.send(
                    _WorkerFailure(
                        request_id=message.request_id,
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                )
            else:
                connection.send(_PredictionResult(message.request_id, labels))
    except EOFError, BrokenPipeError, OSError:
        return
    finally:
        connection.close()


@dataclass(slots=True)
class _InferenceProcess:
    descriptor: PonyChartGenerationDescriptor
    process: BaseProcess | _SupervisedProcess
    connection: _InferenceChannel | None
    request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    leases: int = 0
    retired: bool = False
    cleanup_in_progress: bool = False
    cleanup_complete: bool = False
    cleanup_error: BaseException | None = None


@dataclass(slots=True)
class PonyChartInferenceLease:
    """A generation reservation taken atomically with publication lookup."""

    descriptor: PonyChartGenerationDescriptor
    state: _InferenceProcess | None = None
    released: bool = False


def _validate_timeout(
    timeout: float,
    *,
    operation: str,
    maximum: float,
) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= maximum
    ):
        raise ValueError(f"{operation} timeout must be finite and in (0, {maximum:g}]")
    return float(timeout)


def _resolve_operation_deadline(
    *,
    timeout: float | None,
    expires_at: float | None,
    operation: str,
    maximum: float,
) -> tuple[float, float]:
    if (timeout is None) == (expires_at is None):
        raise TypeError(f"{operation} requires exactly one of timeout or expires_at")
    if timeout is not None:
        duration = _validate_timeout(
            timeout,
            operation=operation,
            maximum=maximum,
        )
        return time.monotonic() + duration, duration
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int | float)
        or not math.isfinite(expires_at)
    ):
        raise ValueError(f"{operation} deadline must be finite")
    deadline = float(expires_at)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{operation} deadline expired")
    if remaining > maximum:
        raise ValueError(
            f"{operation} deadline must not be more than {maximum:g} seconds away"
        )
    return deadline, remaining


def _remaining(expires_at: float) -> float:
    return max(0.0, expires_at - time.monotonic())


def _cleanup_reserve(timeout: float) -> float:
    """Reserve ownership proof time inside the caller's one total deadline."""

    maximum = _INFERENCE_TERMINATE_GRACE_SECONDS + _INFERENCE_KILL_GRACE_SECONDS
    return min(maximum, timeout / 2.0)


def _process_is_alive(process: BaseProcess | _SupervisedProcess) -> bool:
    if isinstance(process, _SupervisedProcess):
        return process.owner.poll() is None
    try:
        return process.is_alive()
    except ValueError:
        return False


def _spawn_context() -> Any:
    return multiprocessing.get_context("spawn")


@dataclass(slots=True)
class _SupervisedProcess:
    """Adapter for hbrowser's process-tree owner used by Pony workers."""

    owner: OwnedProcess

    @property
    def pid(self) -> int:
        return int(self.owner.target_pid or self.owner.pid)

    def is_alive(self) -> bool:
        return self.owner.poll() is None


def _join_reaped(process: BaseProcess) -> bool:
    try:
        process.join(0)
        reaped = not process.is_alive() and process.exitcode is not None
    except ValueError:
        # ``BaseProcess.close`` is permitted only after the child has stopped.
        # A retry after a strict late-completion error may therefore encounter
        # an already-closed handle whose prior reap proof remains valid.
        return True
    if reaped:
        process.close()
    return reaped


def _terminate_process_sync(
    process: BaseProcess | _SupervisedProcess,
    *,
    expires_at: float,
) -> None:
    """TERM, KILL if needed, then prove the multiprocessing child was reaped."""

    if _remaining(expires_at) <= 0:
        raise PonyChartWorkerOwnershipError(
            "PonyChart process cleanup deadline expired before ownership proof"
        )
    if isinstance(process, _SupervisedProcess):
        try:
            process.owner.shutdown(
                graceful_timeout=0.0,
                terminate_timeout=_INFERENCE_TERMINATE_GRACE_SECONDS,
                kill_timeout=_INFERENCE_KILL_GRACE_SECONDS,
                cleanup_timeout=_remaining(expires_at),
                deadline=expires_at,
            )
        except ProcessOwnershipError as error:
            raise PonyChartWorkerOwnershipError(
                "Supervised PonyChart process tree was not reaped before its deadline"
            ) from error
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "Supervised PonyChart process tree was reaped after its deadline"
            )
        return

    if _process_is_alive(process):
        process.terminate()
        terminate_budget = min(
            _INFERENCE_TERMINATE_GRACE_SECONDS,
            _remaining(expires_at) / 4.0,
        )
        process.join(terminate_budget)
    if _process_is_alive(process):
        process.kill()
        process.join(min(_INFERENCE_KILL_GRACE_SECONDS, _remaining(expires_at)))
    if not _join_reaped(process):
        raise PonyChartWorkerOwnershipError(
            f"PonyChart worker pid={process.pid} was not reaped before cleanup deadline"
        )
    if _remaining(expires_at) <= 0:
        raise PonyChartWorkerOwnershipError(
            "PonyChart worker was reaped after cleanup deadline"
        )


async def _await_process_exit(
    process: BaseProcess | _SupervisedProcess,
    *,
    expires_at: float,
) -> bool:
    if isinstance(process, _SupervisedProcess):
        while process.owner.poll() is None and _remaining(expires_at) > 0:
            await asyncio.sleep(
                min(_IPC_RECONCILIATION_INTERVAL_SECONDS, _remaining(expires_at))
            )
        return process.owner.poll() is not None
    if not process.is_alive():
        return True
    if _remaining(expires_at) <= 0:
        return False
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()

    def ready() -> None:
        if not future.done():
            future.set_result(None)

    try:
        loop.add_reader(process.sentinel, ready)
    except AttributeError, NotImplementedError:
        while process.is_alive() and _remaining(expires_at) > 0:
            await asyncio.sleep(
                min(
                    _IPC_RECONCILIATION_INTERVAL_SECONDS,
                    _remaining(expires_at),
                )
            )
        return not process.is_alive()
    try:
        try:
            async with asyncio.timeout_at(expires_at):
                await future
        except TimeoutError:
            return not process.is_alive()
        return True
    finally:
        loop.remove_reader(process.sentinel)


async def _terminate_process_async(
    process: BaseProcess | _SupervisedProcess,
    *,
    expires_at: float,
) -> None:
    """Async counterpart that never blocks the event loop in ``join``."""

    if _remaining(expires_at) <= 0:
        raise PonyChartWorkerOwnershipError(
            "PonyChart process cleanup deadline expired before ownership proof"
        )
    if isinstance(process, _SupervisedProcess):
        cleanup = asyncio.create_task(
            asyncio.to_thread(
                _terminate_process_sync,
                process,
                expires_at=expires_at,
            )
        )
        cancelled = await _complete_owned_cleanup(cleanup)
        if cancelled:
            raise asyncio.CancelledError
        return

    if _process_is_alive(process):
        process.terminate()
        terminate_budget = min(
            _INFERENCE_TERMINATE_GRACE_SECONDS,
            _remaining(expires_at) / 4.0,
        )
        await _await_process_exit(
            process,
            expires_at=min(expires_at, time.monotonic() + terminate_budget),
        )
    if _process_is_alive(process):
        process.kill()
        await _await_process_exit(
            process,
            expires_at=min(
                expires_at,
                time.monotonic() + _INFERENCE_KILL_GRACE_SECONDS,
            ),
        )
    if not _join_reaped(process):
        raise PonyChartWorkerOwnershipError(
            f"PonyChart worker pid={process.pid} was not reaped before cleanup deadline"
        )
    if _remaining(expires_at) <= 0:
        raise PonyChartWorkerOwnershipError(
            "PonyChart worker was reaped after cleanup deadline"
        )


async def _complete_owned_cleanup(
    cleanup: asyncio.Task[None],
) -> bool:
    """Finish ownership proof despite cancellation; report cancellation afterward."""

    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    return cancelled


async def _receive_before(
    state: _InferenceProcess,
    *,
    expires_at: float,
) -> object:
    """Receive one complete authenticated frame before its absolute deadline."""

    connection = state.connection
    if connection is None:
        raise RuntimeError("PonyChart inference worker IPC was not established")
    try:
        return await connection.receive_async(expires_at=expires_at)
    except TimeoutError as error:
        raise TimeoutError("PonyChart inference semantic deadline expired") from error
    except EOFError as error:
        raise RuntimeError(
            "PonyChart inference worker exited without a complete IPC response"
        ) from error


class PonyChartInferenceOwner:
    """Own preloaded generation workers and their exact process lifetimes."""

    def __init__(
        self,
        *,
        register_atexit: bool = True,
    ) -> None:
        self._initialize(
            context=_spawn_context(),
            worker_target=_inference_worker_main,
            use_process_supervisor=True,
            register_atexit=register_atexit,
        )

    @classmethod
    def _for_testing(
        cls,
        *,
        worker_target: _InferenceWorkerTarget,
        context: Any | None = None,
        register_atexit: bool = False,
    ) -> PonyChartInferenceOwner:
        """Build the raw-process test double path outside the production API."""
        owner = cls.__new__(cls)
        owner._initialize(
            context=context or _spawn_context(),
            worker_target=worker_target,
            use_process_supervisor=False,
            register_atexit=register_atexit,
        )
        return owner

    def _initialize(
        self,
        *,
        context: Any,
        worker_target: _InferenceWorkerTarget,
        use_process_supervisor: bool,
        register_atexit: bool,
    ) -> None:
        self._context = context
        self._worker_target = worker_target
        self._use_process_supervisor = use_process_supervisor
        self._states: dict[str, _InferenceProcess] = {}
        self._owned_states: dict[int, _InferenceProcess] = {}
        self._pending_leases: dict[str, int] = {}
        self._active_generation: str | None = None
        self._spawning = 0
        self._closing = False
        self._state_lock = threading.Lock()
        self._async_start_lock = asyncio.Lock()
        if register_atexit:
            atexit.register(self.close_sync, timeout=5.0)

    def _spawn_supervised(
        self,
        descriptor: PonyChartGenerationDescriptor,
        *,
        ready_expires_at: float,
        expires_at: float,
    ) -> _InferenceProcess:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()
        token = uuid4().hex
        state: _InferenceProcess | None = None
        try:
            startup_timeout = min(5.0, _remaining(ready_expires_at))
            if startup_timeout <= 0:
                raise TimeoutError("PonyChart inference worker preload timed out")
            owner = start_owned_process(
                sys.executable,
                (
                    "-m",
                    "hvbattle._ponychart_worker_entry",
                    "inference",
                    str(host),
                    str(port),
                    token,
                    descriptor.generation,
                    str(descriptor.model_path),
                    str(descriptor.thresholds_path),
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                drain_output=True,
                forward_logging=True,
                startup_timeout=startup_timeout,
                deadline=expires_at,
            )
            state = _InferenceProcess(
                descriptor,
                _SupervisedProcess(owner),
                None,
            )
            # Register immediately after the supervisor proves target identity;
            # connection setup is fallible and must not orphan that process tree.
            with self._state_lock:
                self._owned_states[id(state)] = state
            remaining = _remaining(ready_expires_at)
            if remaining <= 0:
                raise TimeoutError("PonyChart inference IPC setup timed out")
            listener.settimeout(remaining)
            accepted, address = listener.accept()
            if address[0] != "127.0.0.1":
                accepted.close()
                raise RuntimeError("PonyChart inference IPC peer is not local")
            connection = _InferenceChannel(accepted, token)
            state.connection = connection
            if connection.receive(expires_at=ready_expires_at) != _WorkerHello(token):
                raise RuntimeError("PonyChart inference worker handshake was invalid")
            return state
        except BaseException:
            if state is not None:
                self._cleanup_state_sync(state, expires_at=expires_at)
            raise
        finally:
            listener.close()

    def _spawn(
        self,
        descriptor: PonyChartGenerationDescriptor,
        *,
        ready_expires_at: float,
        expires_at: float,
    ) -> _InferenceProcess:
        with self._state_lock:
            if self._closing:
                raise RuntimeError("PonyChart inference owner is closing")
            self._spawning += 1
        if self._use_process_supervisor:
            try:
                return self._spawn_supervised(
                    descriptor,
                    ready_expires_at=ready_expires_at,
                    expires_at=expires_at,
                )
            finally:
                with self._state_lock:
                    self._spawning -= 1
        parent: _InferenceChannel | None = None
        child: _InferenceChannel | None = None
        process: BaseProcess | None = None
        started = False
        try:
            parent_transport, child_transport = socket.socketpair()
            token = uuid4().hex
            parent = _InferenceChannel(parent_transport, token)
            child = _InferenceChannel(child_transport, token)
            process = self._context.Process(
                target=self._worker_target,
                args=(child, descriptor),
                name=f"ponychart-inference-{descriptor.generation[:12]}",
                daemon=True,
            )
            process.start()
            started = True
            child.close()
            state = _InferenceProcess(descriptor, process, parent)
            with self._state_lock:
                self._owned_states[id(state)] = state
            return state
        except BaseException:
            if parent is not None:
                parent.close()
            if child is not None:
                child.close()
            if started and process is not None:
                # A successfully started child must become durable ownership even
                # if an unusual post-start setup operation failed.
                state = _InferenceProcess(descriptor, process, parent)
                with self._state_lock:
                    self._owned_states[id(state)] = state
            raise
        finally:
            with self._state_lock:
                self._spawning -= 1

    @staticmethod
    def _validate_descriptor(
        state: _InferenceProcess,
        descriptor: PonyChartGenerationDescriptor,
    ) -> None:
        if state.descriptor != descriptor:
            raise RuntimeError(
                "PonyChart generation id resolved to different artifact paths"
            )

    def _usable_state_locked(
        self,
        descriptor: PonyChartGenerationDescriptor,
    ) -> _InferenceProcess | None:
        state = self._states.get(descriptor.generation)
        if state is None:
            return None
        self._validate_descriptor(state, descriptor)
        if (
            state.retired
            or state.cleanup_in_progress
            or state.cleanup_complete
            or not _process_is_alive(state.process)
        ):
            return None
        return state

    def _state_for(
        self,
        descriptor: PonyChartGenerationDescriptor,
    ) -> _InferenceProcess | None:
        with self._state_lock:
            return self._usable_state_locked(descriptor)

    @staticmethod
    def _validate_ready(
        state: _InferenceProcess,
        message: object,
    ) -> None:
        if isinstance(message, _WorkerFailure) and message.request_id is None:
            raise RuntimeError(
                "PonyChart inference worker failed to preload "
                f"({message.error_type}: {message.message})"
            )
        if message != _WorkerReady(state.descriptor.generation):
            raise RuntimeError("PonyChart inference worker sent invalid READY receipt")

    def _finish_cleanup(
        self,
        state: _InferenceProcess,
        error: BaseException | None,
    ) -> None:
        with self._state_lock:
            state.cleanup_in_progress = False
            state.cleanup_error = error
            if error is None:
                state.cleanup_complete = True
                self._owned_states.pop(id(state), None)
                if self._states.get(state.descriptor.generation) is state:
                    self._states.pop(state.descriptor.generation, None)

    def _claim_cleanup(
        self, state: _InferenceProcess
    ) -> Literal["owner", "wait", "complete"]:
        with self._state_lock:
            if state.cleanup_complete:
                return "complete"
            if state.cleanup_in_progress:
                return "wait"
            state.cleanup_in_progress = True
            state.cleanup_error = None
            return "owner"

    async def _cleanup_state_async(
        self,
        state: _InferenceProcess,
        *,
        expires_at: float,
    ) -> None:
        while True:
            claim = self._claim_cleanup(state)
            if claim == "complete":
                if _remaining(expires_at) <= 0:
                    raise PonyChartWorkerOwnershipError(
                        "PonyChart worker cleanup was observed after its deadline"
                    )
                return
            if claim == "wait":
                remaining = _remaining(expires_at)
                if remaining <= 0:
                    raise PonyChartWorkerOwnershipError(
                        "PonyChart worker cleanup did not finish before its deadline"
                    )
                await asyncio.sleep(
                    min(_IPC_RECONCILIATION_INTERVAL_SECONDS, remaining)
                )
                continue
            try:
                await _terminate_process_async(state.process, expires_at=expires_at)
            except BaseException as error:
                if state.connection is not None:
                    with suppress(OSError):
                        state.connection.close()
                self._finish_cleanup(state, error)
                raise
            if state.connection is not None:
                with suppress(OSError):
                    state.connection.close()
            self._finish_cleanup(state, None)
            if _remaining(expires_at) <= 0:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart worker cleanup completed after its deadline"
                )
            return

    def _cleanup_state_sync(
        self,
        state: _InferenceProcess,
        *,
        expires_at: float,
    ) -> None:
        while True:
            claim = self._claim_cleanup(state)
            if claim == "complete":
                if _remaining(expires_at) <= 0:
                    raise PonyChartWorkerOwnershipError(
                        "PonyChart worker cleanup was observed after its deadline"
                    )
                return
            if claim == "wait":
                remaining = _remaining(expires_at)
                if remaining <= 0:
                    raise PonyChartWorkerOwnershipError(
                        "PonyChart worker cleanup did not finish before its deadline"
                    )
                time.sleep(min(_IPC_RECONCILIATION_INTERVAL_SECONDS, remaining))
                continue
            try:
                _terminate_process_sync(state.process, expires_at=expires_at)
            except BaseException as error:
                if state.connection is not None:
                    with suppress(OSError):
                        state.connection.close()
                self._finish_cleanup(state, error)
                raise
            if state.connection is not None:
                with suppress(OSError):
                    state.connection.close()
            self._finish_cleanup(state, None)
            if _remaining(expires_at) <= 0:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart worker cleanup completed after its deadline"
                )
            return

    def prepare(
        self,
        descriptor: PonyChartGenerationDescriptor,
        *,
        timeout: float,
    ) -> None:
        """Synchronously preload a spawned worker during application startup."""

        timeout = _validate_timeout(
            timeout,
            operation="inference startup",
            maximum=_INFERENCE_STARTUP_TIMEOUT_SECONDS,
        )
        expires_at = time.monotonic() + timeout
        ready_expires_at = expires_at - _cleanup_reserve(timeout)
        if self._state_for(descriptor) is not None:
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart inference worker preload timed out")
            return
        state = self._spawn(
            descriptor,
            ready_expires_at=ready_expires_at,
            expires_at=expires_at,
        )
        try:
            connection = state.connection
            if connection is None:
                raise RuntimeError("PonyChart inference IPC was not established")
            self._validate_ready(
                state,
                connection.receive(expires_at=ready_expires_at),
            )
        except BaseException:
            self._cleanup_state_sync(state, expires_at=expires_at)
            raise

        duplicate: _InferenceProcess | None = None
        unresolved_owner = False
        with self._state_lock:
            existing = None if self._closing else self._usable_state_locked(descriptor)
            if self._closing:
                duplicate = state
                unresolved_owner = True
            elif existing is None:
                occupied = self._states.get(descriptor.generation)
                if occupied is not None and not occupied.cleanup_complete:
                    duplicate = state
                    unresolved_owner = True
                else:
                    self._states[descriptor.generation] = state
            else:
                duplicate = state
        if duplicate is not None:
            self._cleanup_state_sync(duplicate, expires_at=expires_at)
        if unresolved_owner:
            raise PonyChartWorkerOwnershipError(
                "A retired PonyChart generation still has unresolved ownership"
            )
        if _remaining(ready_expires_at) <= 0:
            raise TimeoutError("PonyChart inference worker preload timed out")

    async def prepare_async(
        self,
        descriptor: PonyChartGenerationDescriptor,
        *,
        timeout: float | None = None,
        expires_at: float | None = None,
    ) -> None:
        """Preload one worker without blocking the event loop on its READY IPC."""

        expires_at, operation_timeout = _resolve_operation_deadline(
            timeout=timeout,
            expires_at=expires_at,
            operation="inference startup",
            maximum=_INFERENCE_STARTUP_TIMEOUT_SECONDS,
        )
        ready_expires_at = expires_at - _cleanup_reserve(operation_timeout)
        if self._state_for(descriptor) is not None:
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart inference worker preload timed out")
            return

        remaining = _remaining(ready_expires_at)
        if remaining <= 0:
            raise TimeoutError("PonyChart inference worker preload timed out")
        try:
            async with asyncio.timeout_at(ready_expires_at):
                await self._async_start_lock.acquire()
        except TimeoutError as error:
            raise TimeoutError(
                "PonyChart inference worker preload timed out"
            ) from error
        try:
            if self._state_for(descriptor) is not None:
                if _remaining(ready_expires_at) <= 0:
                    raise TimeoutError("PonyChart inference worker preload timed out")
                return
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart inference worker preload timed out")
            spawn = asyncio.create_task(
                asyncio.to_thread(
                    self._spawn,
                    descriptor,
                    ready_expires_at=ready_expires_at,
                    expires_at=expires_at,
                )
            )
            cancelled = False
            while not spawn.done():
                try:
                    await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    cancelled = True
            state = spawn.result()
            if cancelled:
                cleanup = asyncio.create_task(
                    self._cleanup_state_async(state, expires_at=expires_at)
                )
                await _complete_owned_cleanup(cleanup)
                raise asyncio.CancelledError
            try:
                message = await _receive_before(
                    state,
                    expires_at=ready_expires_at,
                )
                self._validate_ready(state, message)
            except BaseException as error:
                cleanup = asyncio.create_task(
                    self._cleanup_state_async(state, expires_at=expires_at)
                )
                cancelled = await _complete_owned_cleanup(cleanup)
                if cancelled and not isinstance(error, asyncio.CancelledError):
                    raise asyncio.CancelledError from error
                raise

            duplicate: _InferenceProcess | None = None
            unresolved_owner = False
            with self._state_lock:
                existing = (
                    None if self._closing else self._usable_state_locked(descriptor)
                )
                if self._closing:
                    duplicate = state
                    unresolved_owner = True
                elif existing is None:
                    occupied = self._states.get(descriptor.generation)
                    if occupied is None or occupied.cleanup_complete:
                        self._states[descriptor.generation] = state
                    else:
                        duplicate = state
                        unresolved_owner = True
                else:
                    duplicate = state
            if duplicate is not None:
                cleanup = asyncio.create_task(
                    self._cleanup_state_async(duplicate, expires_at=expires_at)
                )
                cancelled = await _complete_owned_cleanup(cleanup)
                if cancelled:
                    raise asyncio.CancelledError
            if unresolved_owner:
                raise PonyChartWorkerOwnershipError(
                    "A retired PonyChart generation still has unresolved ownership"
                )
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart inference worker preload timed out")
        finally:
            self._async_start_lock.release()

    def reserve(
        self,
        descriptor: PonyChartGenerationDescriptor,
    ) -> PonyChartInferenceLease:
        """Reserve the currently published generation without awaiting anything."""

        with self._state_lock:
            if self._active_generation != descriptor.generation:
                raise RuntimeError(
                    "PonyChart publication does not match the active worker generation"
                )
            state = self._usable_state_locked(descriptor)
            if state is not None:
                state.leases += 1
                return PonyChartInferenceLease(descriptor, state)
            self._pending_leases[descriptor.generation] = (
                self._pending_leases.get(descriptor.generation, 0) + 1
            )
            return PonyChartInferenceLease(descriptor)

    def _attach_pending_lease(
        self,
        lease: PonyChartInferenceLease,
        state: _InferenceProcess,
    ) -> None:
        with self._state_lock:
            if lease.released:
                raise RuntimeError("PonyChart inference lease was already released")
            pending = self._pending_leases.get(lease.descriptor.generation, 0)
            if pending <= 0:
                raise RuntimeError("PonyChart inference lease accounting was lost")
            if pending == 1:
                self._pending_leases.pop(lease.descriptor.generation, None)
            else:
                self._pending_leases[lease.descriptor.generation] = pending - 1
            state.leases += 1
            if self._active_generation != lease.descriptor.generation:
                state.retired = True
            lease.state = state

    async def _prepare_async(
        self,
        lease: PonyChartInferenceLease,
        *,
        ready_expires_at: float,
        expires_at: float,
    ) -> _InferenceProcess:
        descriptor = lease.descriptor
        remaining = _remaining(ready_expires_at)
        if remaining <= 0:
            raise TimeoutError("PonyChart inference semantic deadline expired")
        try:
            async with asyncio.timeout_at(ready_expires_at):
                await self._async_start_lock.acquire()
        except TimeoutError as error:
            raise TimeoutError(
                "PonyChart inference semantic deadline expired waiting for startup "
                "ownership"
            ) from error
        try:
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart inference semantic deadline expired")
            existing = self._state_for(descriptor)
            if existing is not None:
                self._attach_pending_lease(lease, existing)
                if _remaining(ready_expires_at) <= 0:
                    raise TimeoutError("PonyChart inference semantic deadline expired")
                return existing
            spawn = asyncio.create_task(
                asyncio.to_thread(
                    self._spawn,
                    descriptor,
                    ready_expires_at=ready_expires_at,
                    expires_at=expires_at,
                )
            )
            cancelled = False
            while not spawn.done():
                try:
                    await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    cancelled = True
            state = spawn.result()
            if cancelled:
                cleanup = asyncio.create_task(
                    self._cleanup_state_async(state, expires_at=expires_at)
                )
                await _complete_owned_cleanup(cleanup)
                raise asyncio.CancelledError
            try:
                message = await _receive_before(state, expires_at=ready_expires_at)
                self._validate_ready(state, message)
            except BaseException as error:
                cleanup = asyncio.create_task(
                    self._cleanup_state_async(state, expires_at=expires_at)
                )
                cancelled = await _complete_owned_cleanup(cleanup)
                if cancelled and not isinstance(error, asyncio.CancelledError):
                    raise asyncio.CancelledError from error
                raise

            duplicate: _InferenceProcess | None = None
            unresolved_owner = False
            with self._state_lock:
                existing = (
                    None if self._closing else self._usable_state_locked(descriptor)
                )
                if self._closing:
                    duplicate = state
                    selected = state
                    unresolved_owner = True
                elif existing is None:
                    occupied = self._states.get(descriptor.generation)
                    if occupied is None or occupied.cleanup_complete:
                        self._states[descriptor.generation] = state
                        selected = state
                    else:
                        duplicate = state
                        selected = state
                        unresolved_owner = True
                else:
                    duplicate = state
                    selected = existing
            if duplicate is not None:
                cleanup = asyncio.create_task(
                    self._cleanup_state_async(duplicate, expires_at=expires_at)
                )
                cancelled = await _complete_owned_cleanup(cleanup)
                if cancelled:
                    raise asyncio.CancelledError
            if unresolved_owner:
                raise PonyChartWorkerOwnershipError(
                    "A retired PonyChart generation still has unresolved ownership"
                )
            self._attach_pending_lease(lease, selected)
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart inference semantic deadline expired")
            return selected
        finally:
            self._async_start_lock.release()

    def activate(
        self,
        descriptor: PonyChartGenerationDescriptor,
    ) -> tuple[_InferenceProcess, ...]:
        """Atomically activate READY generation and identify idle old workers."""

        with self._state_lock:
            if self._closing:
                raise RuntimeError("PonyChart inference owner is closing")
            state = self._usable_state_locked(descriptor)
            if state is None:
                raise RuntimeError(
                    "PonyChart generation was published before worker READY"
                )
            self._active_generation = descriptor.generation
            retired: list[_InferenceProcess] = []
            for candidate in self._owned_states.values():
                if candidate is state:
                    candidate.retired = False
                    continue
                candidate.retired = True
                if candidate.leases == 0 and not candidate.cleanup_complete:
                    retired.append(candidate)
            return tuple(retired)

    def retire_superseded(
        self,
        states: tuple[_InferenceProcess, ...],
        *,
        timeout: float,
    ) -> None:
        """Reap a swap's idle old workers under one shared cleanup deadline."""

        timeout = _validate_timeout(
            timeout,
            operation="generation retirement",
            maximum=_WORKER_CLEANUP_TIMEOUT_SECONDS,
        )
        expires_at = time.monotonic() + timeout
        errors: list[BaseException] = []
        for candidate in states:
            try:
                self._cleanup_state_sync(candidate, expires_at=expires_at)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise PonyChartWorkerOwnershipError(
                f"Failed to reap {len(errors)} superseded PonyChart worker(s)"
            ) from errors[0]

    async def retire_superseded_async(
        self,
        states: tuple[_InferenceProcess, ...],
        *,
        timeout: float | None = None,
        expires_at: float | None = None,
    ) -> None:
        """Reap idle old generations concurrently under one absolute deadline."""

        expires_at, _ = _resolve_operation_deadline(
            timeout=timeout,
            expires_at=expires_at,
            operation="generation retirement",
            maximum=_WORKER_CLEANUP_TIMEOUT_SECONDS,
        )

        async def retire_owned() -> None:
            results = await asyncio.gather(
                *(
                    self._cleanup_state_async(state, expires_at=expires_at)
                    for state in states
                ),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise PonyChartWorkerOwnershipError(
                    f"Failed to reap {len(errors)} superseded PonyChart worker(s)"
                ) from errors[0]
            if _remaining(expires_at) <= 0:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart generation retirement completed after its deadline"
                )

        cleanup = asyncio.create_task(retire_owned())
        cancelled = await _complete_owned_cleanup(cleanup)
        if cancelled:
            raise asyncio.CancelledError

    async def _release_lease(
        self,
        lease: PonyChartInferenceLease,
        *,
        expires_at: float,
    ) -> None:
        cleanup: _InferenceProcess | None = None
        with self._state_lock:
            if lease.released:
                return
            lease.released = True
            state = lease.state
            if state is None:
                pending = self._pending_leases.get(lease.descriptor.generation, 0)
                if pending <= 1:
                    self._pending_leases.pop(lease.descriptor.generation, None)
                else:
                    self._pending_leases[lease.descriptor.generation] = pending - 1
            else:
                state.leases = max(0, state.leases - 1)
                if state.retired and state.leases == 0:
                    cleanup = state
        if cleanup is not None:
            cleanup_task = asyncio.create_task(
                self._cleanup_state_async(cleanup, expires_at=expires_at)
            )
            cancelled = await _complete_owned_cleanup(cleanup_task)
            if cancelled:
                raise asyncio.CancelledError

    async def _retire_request_worker(
        self,
        state: _InferenceProcess,
        *,
        expires_at: float,
    ) -> None:
        with self._state_lock:
            state.retired = True
        await self._cleanup_state_async(state, expires_at=expires_at)

    async def predict_reserved(
        self,
        lease: PonyChartInferenceLease,
        image: bytes,
        *,
        timeout: float,
    ) -> tuple[str, ...]:
        """Predict and prove failed-worker cleanup inside one total deadline."""

        timeout = _validate_timeout(
            timeout,
            operation="inference",
            maximum=_INFERENCE_TIMEOUT_SECONDS,
        )
        expires_at = time.monotonic() + timeout
        work_expires_at = expires_at - _cleanup_reserve(timeout)
        state = lease.state
        request_lock_acquired = False
        request_started = False
        shared: SharedMemory | None = None
        try:
            if lease.released:
                raise RuntimeError("PonyChart inference lease was already released")
            if state is None:
                state = await self._prepare_async(
                    lease,
                    ready_expires_at=work_expires_at,
                    expires_at=expires_at,
                )
            remaining = _remaining(work_expires_at)
            if remaining <= 0:
                raise TimeoutError("PonyChart inference semantic deadline expired")
            try:
                async with asyncio.timeout_at(work_expires_at):
                    await state.request_lock.acquire()
            except TimeoutError as error:
                raise TimeoutError(
                    "PonyChart inference semantic deadline expired"
                ) from error
            request_lock_acquired = True
            if not _process_is_alive(state.process) or state.cleanup_in_progress:
                raise RuntimeError("PonyChart inference worker is not available")

            request_id = uuid4().hex
            shared = SharedMemory(create=True, size=max(1, len(image)))
            buffer = shared.buf
            if buffer is None:
                raise RuntimeError("PonyChart shared image memory is unavailable")
            buffer[: len(image)] = image
            connection = state.connection
            if connection is None:
                raise RuntimeError("PonyChart inference worker IPC is unavailable")
            if _remaining(work_expires_at) <= 0:
                raise TimeoutError("PonyChart inference semantic deadline expired")
            # From this point onward the child may have observed the request, so any
            # failure must retire it before the ownership deadline is allowed to end.
            request_started = True
            await connection.send_async(
                _PredictRequest(
                    request_id=request_id,
                    shared_memory_name=shared.name,
                    image_size=len(image),
                ),
                expires_at=work_expires_at,
            )
            message = await _receive_before(state, expires_at=work_expires_at)
            if isinstance(message, _PredictionResult):
                if message.request_id != request_id:
                    raise RuntimeError(
                        "PonyChart inference worker returned mismatched request id"
                    )
                return message.labels
            if isinstance(message, _WorkerFailure) and message.request_id == request_id:
                raise ValueError(
                    "PonyChart inference failed "
                    f"({message.error_type}: {message.message})"
                )
            raise RuntimeError("PonyChart inference worker returned invalid IPC")
        except asyncio.CancelledError:
            if request_started and state is not None:
                cleanup = asyncio.create_task(
                    self._retire_request_worker(state, expires_at=expires_at)
                )
                await _complete_owned_cleanup(cleanup)
            raise
        except TimeoutError as error:
            if request_started and state is not None:
                cleanup = asyncio.create_task(
                    self._retire_request_worker(state, expires_at=expires_at)
                )
                if await _complete_owned_cleanup(cleanup):
                    raise asyncio.CancelledError from error
            raise
        except (EOFError, BrokenPipeError, OSError, RuntimeError) as error:
            if request_started and state is not None:
                cleanup = asyncio.create_task(
                    self._retire_request_worker(state, expires_at=expires_at)
                )
                if await _complete_owned_cleanup(cleanup):
                    raise asyncio.CancelledError from error
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(
                "PonyChart inference worker IPC closed before its receipt"
            ) from error
        finally:
            if shared is not None:
                shared.close()
                with suppress(FileNotFoundError):
                    shared.unlink()
            if request_lock_acquired and state is not None:
                state.request_lock.release()
            await self._release_lease(lease, expires_at=expires_at)

    async def predict(
        self,
        descriptor: PonyChartGenerationDescriptor,
        image: bytes,
        *,
        timeout: float,
    ) -> tuple[str, ...]:
        """Convenience API for callers not sharing a publication lock."""

        _validate_timeout(
            timeout,
            operation="inference",
            maximum=_INFERENCE_TIMEOUT_SECONDS,
        )
        lease = self.reserve(descriptor)
        return await self.predict_reserved(lease, image, timeout=timeout)

    async def close(self, *, timeout: float) -> None:
        """Stop all children concurrently within one shared ownership deadline."""

        timeout = _validate_timeout(
            timeout,
            operation="inference cleanup",
            maximum=_WORKER_CLEANUP_TIMEOUT_SECONDS,
        )
        await self._close_at(expires_at=time.monotonic() + timeout)

    async def _close_at(self, *, expires_at: float) -> None:
        """Stop all inference children against one caller-owned deadline."""

        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int | float)
            or not math.isfinite(expires_at)
        ):
            raise ValueError("PonyChart inference close deadline must be finite")
        expires_at = float(expires_at)
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart inference close deadline expired before cleanup"
            )

        async def close_owned() -> None:
            with self._state_lock:
                self._closing = True
                self._active_generation = None
            if _remaining(expires_at) <= 0:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart inference state ownership completed after close deadline"
                )
            while True:
                with self._state_lock:
                    spawning = self._spawning
                if _remaining(expires_at) <= 0:
                    raise PonyChartWorkerOwnershipError(
                        "PonyChart inference spawn snapshot completed "
                        "after close deadline"
                    )
                if spawning == 0:
                    break
                remaining = _remaining(expires_at)
                if remaining <= 0:
                    raise PonyChartWorkerOwnershipError(
                        "PonyChart worker spawn did not settle before close deadline"
                    )
                await asyncio.sleep(
                    min(_IPC_RECONCILIATION_INTERVAL_SECONDS, remaining)
                )
            with self._state_lock:
                states = tuple(self._owned_states.values())
                for state in states:
                    state.retired = True
            if _remaining(expires_at) <= 0:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart inference state snapshot completed after close deadline"
                )
            results = await asyncio.gather(
                *(
                    self._cleanup_state_async(state, expires_at=expires_at)
                    for state in states
                ),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise PonyChartWorkerOwnershipError(
                    f"Failed to reap {len(errors)} PonyChart inference worker(s)"
                ) from errors[0]

        cleanup = asyncio.create_task(close_owned())
        cancelled = await _complete_owned_cleanup(cleanup)
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart inference close completed after its deadline"
            )
        if cancelled:
            raise asyncio.CancelledError

    def close_sync(self, *, timeout: float) -> None:
        """Bounded terminal cleanup used by ``atexit`` and sync startup failure."""

        timeout = _validate_timeout(
            timeout,
            operation="inference cleanup",
            maximum=_WORKER_CLEANUP_TIMEOUT_SECONDS,
        )
        expires_at = time.monotonic() + timeout
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart inference close deadline expired before cleanup"
            )
        with self._state_lock:
            self._closing = True
            self._active_generation = None
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart inference state ownership completed after close deadline"
            )
        while True:
            with self._state_lock:
                spawning = self._spawning
            if _remaining(expires_at) <= 0:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart inference spawn snapshot completed after close deadline"
                )
            if spawning == 0:
                break
            remaining = _remaining(expires_at)
            if remaining <= 0:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart worker spawn did not settle before close deadline"
                )
            time.sleep(min(_IPC_RECONCILIATION_INTERVAL_SECONDS, remaining))
        with self._state_lock:
            states = tuple(self._owned_states.values())
            for state in states:
                state.retired = True
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart inference state snapshot completed after close deadline"
            )
        errors: list[BaseException] = []
        for state in states:
            try:
                self._cleanup_state_sync(state, expires_at=expires_at)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise PonyChartWorkerOwnershipError(
                f"Failed to reap {len(errors)} PonyChart inference worker(s)"
            ) from errors[0]
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart inference close completed after its deadline"
            )


@dataclass(frozen=True, slots=True)
class _RetentionWrite:
    image: bytes
    directory: Path


def _write_retained_capture(image: bytes, directory: Path) -> None:
    image_info = inspect_ponychart_image(image)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f"pony_chart_{timestamp}_",
            suffix=image_info.extension,
            dir=directory,
            delete=False,
        ) as temporary:
            destination = Path(temporary.name)
            temporary.write(image)
    except BaseException:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise


def _retention_worker_main(
    messages: Queue[object],
    ready: _InferenceChannel,
) -> None:
    try:
        ready.send(_WorkerReady("retention"))
    finally:
        ready.close()
    while True:
        message = messages.get()
        if isinstance(message, _StopWorker):
            return
        if not isinstance(message, _RetentionWrite):
            logger.error("PonyChart retention worker received invalid IPC")
            return
        try:
            _write_retained_capture(message.image, message.directory)
        except (OSError, ValueError) as error:
            logger.warning(
                "PonyChart image retention write failed error_type=%s",
                type(error).__name__,
            )


def _retention_supervised_worker_main(
    transport: socket.socket,
    *,
    token: str,
) -> None:
    """Consume parent-owned shared-memory writes over best-effort datagrams."""

    transport.send(
        json.dumps(
            {"type": "ready", "token": token},
            separators=(",", ":"),
        ).encode()
    )
    while True:
        raw = transport.recv(64 * 1024)
        message = json.loads(raw)
        if not isinstance(message, dict) or message.get("token") != token:
            continue
        if message.get("type") == "stop":
            return
        if message.get("type") != "write":
            continue
        request_id = message.get("requestId")
        shared_name = message.get("sharedMemory")
        image_size = message.get("imageSize")
        raw_directory = message.get("directory")
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(shared_name, str)
            or not shared_name
            or isinstance(image_size, bool)
            or not isinstance(image_size, int)
            or image_size < 0
            or not isinstance(raw_directory, str)
            or not raw_directory
        ):
            continue
        error_type: str | None = None
        try:
            shared = SharedMemory(name=shared_name, track=False)
            try:
                buffer = shared.buf
                if buffer is None:
                    raise RuntimeError("PonyChart retained image memory is unavailable")
                image = bytes(buffer[:image_size])
            finally:
                shared.close()
            _write_retained_capture(image, Path(raw_directory))
        except BaseException as error:
            error_type = type(error).__name__
        transport.send(
            json.dumps(
                {
                    "type": "ack",
                    "token": token,
                    "requestId": request_id,
                    "errorType": error_type,
                },
                separators=(",", ":"),
            ).encode()
        )


class PonyChartRetentionOwner:
    """Own one bounded best-effort writer queue and its process."""

    def __init__(
        self,
        *,
        capacity: int = _RETENTION_QUEUE_CAPACITY,
        register_atexit: bool = True,
    ) -> None:
        self._initialize(
            context=_spawn_context(),
            worker_target=_retention_worker_main,
            use_process_supervisor=True,
            capacity=capacity,
            register_atexit=register_atexit,
        )

    @classmethod
    def _for_testing(
        cls,
        *,
        worker_target: _RetentionWorkerTarget,
        context: Any | None = None,
        capacity: int = _RETENTION_QUEUE_CAPACITY,
        register_atexit: bool = False,
    ) -> PonyChartRetentionOwner:
        """Build the raw-process test double path outside the production API."""
        owner = cls.__new__(cls)
        owner._initialize(
            context=context or _spawn_context(),
            worker_target=worker_target,
            use_process_supervisor=False,
            capacity=capacity,
            register_atexit=register_atexit,
        )
        return owner

    def _initialize(
        self,
        *,
        context: Any,
        worker_target: _RetentionWorkerTarget,
        use_process_supervisor: bool,
        capacity: int,
        register_atexit: bool,
    ) -> None:
        if capacity <= 0:
            raise ValueError("retention queue capacity must be positive")
        self._context = context
        self._worker_target = worker_target
        self._use_process_supervisor = use_process_supervisor
        self._capacity = capacity
        self._process: BaseProcess | _SupervisedProcess | None = None
        self._messages: Queue[object] | None = None
        self._transport: socket.socket | None = None
        self._transport_token: str | None = None
        self._pending_shared: dict[str, SharedMemory] = {}
        self._closing = False
        self._cleanup_in_progress = False
        self._lock = threading.Lock()
        if register_atexit:
            atexit.register(self.close_sync, timeout=5.0)

    async def _acquire_lock_async(self, *, expires_at: float) -> None:
        while not self._lock.acquire(blocking=False):
            remaining = _remaining(expires_at)
            if remaining <= 0:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart retention ownership lock exceeded its deadline"
                )
            await asyncio.sleep(min(_IPC_RECONCILIATION_INTERVAL_SECONDS, remaining))
        if _remaining(expires_at) <= 0:
            self._lock.release()
            raise PonyChartWorkerOwnershipError(
                "PonyChart retention ownership lock completed after its deadline"
            )

    def _acquire_lock_sync(self, *, expires_at: float) -> None:
        remaining = _remaining(expires_at)
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise PonyChartWorkerOwnershipError(
                "PonyChart retention ownership lock exceeded its deadline"
            )
        if _remaining(expires_at) <= 0:
            self._lock.release()
            raise PonyChartWorkerOwnershipError(
                "PonyChart retention ownership lock completed after its deadline"
            )

    def _release_pending_shared_locked(self) -> None:
        for shared in self._pending_shared.values():
            shared.close()
            with suppress(FileNotFoundError):
                shared.unlink()
        self._pending_shared.clear()

    def _reconcile_retention_locked(self) -> None:
        transport = self._transport
        token = self._transport_token
        if transport is None or token is None:
            return
        while True:
            try:
                raw = transport.recv(64 * 1024)
            except BlockingIOError:
                return
            except OSError:
                return
            try:
                receipt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(receipt, dict)
                or receipt.get("type") != "ack"
                or receipt.get("token") != token
            ):
                continue
            request_id = receipt.get("requestId")
            if not isinstance(request_id, str):
                continue
            shared = self._pending_shared.pop(request_id, None)
            if shared is None:
                continue
            shared.close()
            with suppress(FileNotFoundError):
                shared.unlink()
            error_type = receipt.get("errorType")
            if isinstance(error_type, str) and error_type:
                logger.warning(
                    "PonyChart image retention write failed error_type=%s",
                    error_type,
                )

    def _prepare_supervised_locked(
        self,
        *,
        ready_expires_at: float,
        expires_at: float,
    ) -> None:
        transport = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        transport.bind(("127.0.0.1", 0))
        host, port = transport.getsockname()
        token = uuid4().hex
        process: _SupervisedProcess | None = None
        try:
            startup_timeout = min(5.0, _remaining(ready_expires_at))
            if startup_timeout <= 0:
                raise TimeoutError("PonyChart retention worker preload timed out")
            owner = start_owned_process(
                sys.executable,
                (
                    "-m",
                    "hvbattle._ponychart_worker_entry",
                    "retention",
                    str(host),
                    str(port),
                    token,
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                drain_output=True,
                forward_logging=True,
                startup_timeout=startup_timeout,
                deadline=expires_at,
            )
            process = _SupervisedProcess(owner)
            # Durable owner fields are assigned before the fallible READY wait.
            self._process = process
            self._transport = transport
            self._transport_token = token
            remaining = _remaining(ready_expires_at)
            if remaining <= 0:
                raise TimeoutError("PonyChart retention worker preload timed out")
            transport.settimeout(remaining)
            raw, child_address = transport.recvfrom(64 * 1024)
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart retention worker preload timed out")
            receipt = json.loads(raw)
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart retention worker preload timed out")
            if receipt != {"type": "ready", "token": token}:
                raise RuntimeError("PonyChart retention worker READY was invalid")
            if child_address[0] != "127.0.0.1":
                raise RuntimeError("PonyChart retention worker READY peer is not local")
            transport.connect(child_address)
            transport.setblocking(False)
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart retention worker preload timed out")
        except BaseException:
            if process is not None:
                _terminate_process_sync(process, expires_at=expires_at)
            transport.close()
            self._process = None
            self._transport = None
            self._transport_token = None
            self._release_pending_shared_locked()
            raise

    def _prepare_until(
        self,
        *,
        ready_expires_at: float,
        expires_at: float,
    ) -> None:
        remaining = _remaining(ready_expires_at)
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise TimeoutError(
                "PonyChart retention worker preload timed out waiting for ownership"
            )
        try:
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart retention worker preload timed out")
            if self._closing:
                raise RuntimeError("PonyChart retention owner is closing")
            if self._process is not None and self._process.is_alive():
                if _remaining(ready_expires_at) <= 0:
                    raise TimeoutError("PonyChart retention worker preload timed out")
                return
            if self._process is not None:
                previous = self._process
                previous_messages = self._messages
                if isinstance(previous, _SupervisedProcess):
                    _terminate_process_sync(previous, expires_at=expires_at)
                elif not _join_reaped(previous):
                    raise PonyChartWorkerOwnershipError(
                        "Previous PonyChart retention worker was not reaped"
                    )
                if previous_messages is not None:
                    previous_messages.close()
                if self._transport is not None:
                    self._transport.close()
                self._release_pending_shared_locked()
                self._process = None
                self._messages = None
                self._transport = None
                self._transport_token = None
            if _remaining(ready_expires_at) <= 0:
                raise TimeoutError("PonyChart retention worker preload timed out")
            if self._use_process_supervisor:
                self._prepare_supervised_locked(
                    ready_expires_at=ready_expires_at,
                    expires_at=expires_at,
                )
                return
            messages: Queue[object] = self._context.Queue(maxsize=self._capacity)
            parent_transport, child_transport = socket.socketpair()
            token = uuid4().hex
            parent = _InferenceChannel(parent_transport, token)
            child = _InferenceChannel(child_transport, token)
            process = self._context.Process(
                target=self._worker_target,
                args=(messages, child),
                name="ponychart-retention-writer",
                daemon=True,
            )
            try:
                process.start()
            except BaseException:
                parent.close()
                child.close()
                messages.close()
                messages.cancel_join_thread()
                raise
            child.close()
            messages.cancel_join_thread()
            self._process = process
            self._messages = messages
            try:
                if parent.receive(expires_at=ready_expires_at) != _WorkerReady(
                    "retention"
                ):
                    raise RuntimeError(
                        "PonyChart retention worker sent invalid READY receipt"
                    )
            except BaseException:
                try:
                    _terminate_process_sync(
                        process,
                        expires_at=expires_at,
                    )
                finally:
                    parent.close()
                messages.close()
                self._process = None
                self._messages = None
                raise
            parent.close()
        finally:
            self._lock.release()

    def prepare(self, *, timeout: float) -> None:
        timeout = _validate_timeout(
            timeout,
            operation="retention startup",
            maximum=_RETENTION_STARTUP_TIMEOUT_SECONDS,
        )
        expires_at = time.monotonic() + timeout
        self._prepare_until(
            ready_expires_at=expires_at - _cleanup_reserve(timeout),
            expires_at=expires_at,
        )

    async def prepare_async(
        self,
        *,
        timeout: float | None = None,
        expires_at: float | None = None,
    ) -> None:
        """Prepare the bounded writer while settling its owned startup thread."""

        expires_at, operation_timeout = _resolve_operation_deadline(
            timeout=timeout,
            expires_at=expires_at,
            operation="retention startup",
            maximum=_RETENTION_STARTUP_TIMEOUT_SECONDS,
        )
        startup = asyncio.create_task(
            asyncio.to_thread(
                self._prepare_until,
                ready_expires_at=(expires_at - _cleanup_reserve(operation_timeout)),
                expires_at=expires_at,
            )
        )
        cancelled = await _complete_owned_cleanup(startup)
        if cancelled:
            raise asyncio.CancelledError
        if _remaining(expires_at) <= 0:
            raise TimeoutError("PonyChart retention worker preload timed out")

    def submit(
        self, image: bytes, directory: Path
    ) -> Literal["queued", "full", "dead"]:
        """Enqueue immediately; never perform filesystem IO or wait for capacity."""

        if not self._lock.acquire(blocking=False):
            return "full"
        try:
            process = self._process
            messages = self._messages
            if isinstance(process, _SupervisedProcess):
                self._reconcile_retention_locked()
                transport = self._transport
                token = self._transport_token
                if (
                    self._closing
                    or transport is None
                    or token is None
                    or not process.is_alive()
                ):
                    return "dead"
                if len(self._pending_shared) >= self._capacity:
                    return "full"
                request_id = uuid4().hex
                shared: SharedMemory | None = None
                try:
                    shared = SharedMemory(create=True, size=max(1, len(image)))
                    buffer = shared.buf
                    if buffer is None:
                        raise RuntimeError(
                            "PonyChart retained image memory is unavailable"
                        )
                    buffer[: len(image)] = image
                    payload = json.dumps(
                        {
                            "type": "write",
                            "token": token,
                            "requestId": request_id,
                            "sharedMemory": shared.name,
                            "imageSize": len(image),
                            "directory": str(directory),
                        },
                        separators=(",", ":"),
                    ).encode()
                    transport.send(payload)
                except BlockingIOError:
                    if shared is not None:
                        shared.close()
                        shared.unlink()
                    return "full"
                except OSError, ValueError:
                    if shared is not None:
                        shared.close()
                        with suppress(FileNotFoundError):
                            shared.unlink()
                    return "dead"
                assert shared is not None
                self._pending_shared[request_id] = shared
                return "queued"
            if (
                self._closing
                or process is None
                or messages is None
                or not process.is_alive()
            ):
                return "dead"
            try:
                messages.put_nowait(_RetentionWrite(image, directory))
            except queue.Full:
                return "full"
            except OSError, ValueError:
                return "dead"
            return "queued"
        finally:
            self._lock.release()

    async def _close_once(self, *, expires_at: float) -> None:
        while True:
            await self._acquire_lock_async(expires_at=expires_at)
            try:
                self._closing = True
                process = self._process
                messages = self._messages
                if process is None:
                    if _remaining(expires_at) <= 0:
                        raise PonyChartWorkerOwnershipError(
                            "PonyChart retention close completed after its deadline"
                        )
                    return
                if not self._cleanup_in_progress:
                    self._cleanup_in_progress = True
                    break
            finally:
                self._lock.release()
            remaining = _remaining(expires_at)
            if remaining <= 0:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart retention cleanup did not settle before its deadline"
                )
            await asyncio.sleep(min(_IPC_RECONCILIATION_INTERVAL_SECONDS, remaining))
        transport = self._transport
        token = self._transport_token
        if isinstance(process, _SupervisedProcess) and transport is not None and token:
            try:
                transport.send(
                    json.dumps(
                        {"type": "stop", "token": token},
                        separators=(",", ":"),
                    ).encode()
                )
            except BlockingIOError, OSError:
                pass
        elif messages is not None:
            try:
                messages.put_nowait(_StopWorker())
            except queue.Full, OSError, ValueError:
                pass
        try:
            if process.is_alive():
                drain_budget = max(
                    0.0,
                    min(
                        _RETENTION_DRAIN_GRACE_SECONDS,
                        _remaining(expires_at)
                        - _INFERENCE_TERMINATE_GRACE_SECONDS
                        - _INFERENCE_KILL_GRACE_SECONDS,
                    ),
                )
                await _await_process_exit(
                    process,
                    expires_at=min(
                        expires_at,
                        time.monotonic() + drain_budget,
                    ),
                )
            if process.is_alive() or isinstance(process, _SupervisedProcess):
                await _terminate_process_async(process, expires_at=expires_at)
            elif not _join_reaped(process):
                raise PonyChartWorkerOwnershipError(
                    "PonyChart retention worker could not be reaped"
                )
        except BaseException:
            try:
                await self._acquire_lock_async(expires_at=expires_at)
            except PonyChartWorkerOwnershipError:
                # Closing prevents another startup, and submit never changes the
                # cleanup claim. Release it atomically so a later close can retry
                # the still-owned process after this deadline has expired.
                self._cleanup_in_progress = False
                raise
            else:
                try:
                    self._cleanup_in_progress = False
                finally:
                    self._lock.release()
            raise
        try:
            await self._acquire_lock_async(expires_at=expires_at)
        except PonyChartWorkerOwnershipError:
            self._cleanup_in_progress = False
            raise
        try:
            if messages is not None:
                messages.close()
            if transport is not None:
                self._reconcile_retention_locked()
                transport.close()
            self._release_pending_shared_locked()
            self._process = None
            self._messages = None
            self._transport = None
            self._transport_token = None
            self._cleanup_in_progress = False
        finally:
            self._lock.release()
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart retention close completed after its deadline"
            )

    async def close(self, *, timeout: float) -> None:
        timeout = _validate_timeout(
            timeout,
            operation="retention cleanup",
            maximum=_WORKER_CLEANUP_TIMEOUT_SECONDS,
        )
        await self._close_at(expires_at=time.monotonic() + timeout)

    async def _close_at(self, *, expires_at: float) -> None:
        """Stop the retention child against one caller-owned deadline."""

        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int | float)
            or not math.isfinite(expires_at)
        ):
            raise ValueError("PonyChart retention close deadline must be finite")
        expires_at = float(expires_at)
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart retention close deadline expired before cleanup"
            )
        cleanup = asyncio.create_task(self._close_once(expires_at=expires_at))
        cancelled = await _complete_owned_cleanup(cleanup)
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart retention close completed after its deadline"
            )
        if cancelled:
            raise asyncio.CancelledError

    def close_sync(self, *, timeout: float) -> None:
        timeout = _validate_timeout(
            timeout,
            operation="retention cleanup",
            maximum=_WORKER_CLEANUP_TIMEOUT_SECONDS,
        )
        expires_at = time.monotonic() + timeout
        while True:
            self._acquire_lock_sync(expires_at=expires_at)
            try:
                self._closing = True
                process = self._process
                messages = self._messages
                if process is None:
                    if _remaining(expires_at) <= 0:
                        raise PonyChartWorkerOwnershipError(
                            "PonyChart retention close completed after its deadline"
                        )
                    return
                if not self._cleanup_in_progress:
                    self._cleanup_in_progress = True
                    break
            finally:
                self._lock.release()
            remaining = _remaining(expires_at)
            if remaining <= 0:
                raise PonyChartWorkerOwnershipError(
                    "PonyChart retention cleanup did not settle before its deadline"
                )
            time.sleep(min(_IPC_RECONCILIATION_INTERVAL_SECONDS, remaining))
        transport = self._transport
        token = self._transport_token
        if isinstance(process, _SupervisedProcess) and transport is not None and token:
            try:
                transport.send(
                    json.dumps(
                        {"type": "stop", "token": token},
                        separators=(",", ":"),
                    ).encode()
                )
            except BlockingIOError, OSError:
                pass
        elif messages is not None:
            try:
                messages.put_nowait(_StopWorker())
            except queue.Full, OSError, ValueError:
                pass
        drain_budget = max(
            0.0,
            min(
                _RETENTION_DRAIN_GRACE_SECONDS,
                _remaining(expires_at)
                - _INFERENCE_TERMINATE_GRACE_SECONDS
                - _INFERENCE_KILL_GRACE_SECONDS,
            ),
        )
        try:
            if isinstance(process, _SupervisedProcess):
                deadline = time.monotonic() + drain_budget
                while process.is_alive() and _remaining(deadline) > 0:
                    time.sleep(
                        min(
                            _IPC_RECONCILIATION_INTERVAL_SECONDS,
                            _remaining(deadline),
                        )
                    )
            else:
                process.join(drain_budget)
            if process.is_alive() or isinstance(process, _SupervisedProcess):
                _terminate_process_sync(process, expires_at=expires_at)
            elif not _join_reaped(process):
                raise PonyChartWorkerOwnershipError(
                    "PonyChart retention worker could not be reaped"
                )
        except BaseException:
            try:
                self._acquire_lock_sync(expires_at=expires_at)
            except PonyChartWorkerOwnershipError:
                self._cleanup_in_progress = False
                raise
            else:
                try:
                    self._cleanup_in_progress = False
                finally:
                    self._lock.release()
            raise
        try:
            self._acquire_lock_sync(expires_at=expires_at)
        except PonyChartWorkerOwnershipError:
            self._cleanup_in_progress = False
            raise
        try:
            if messages is not None:
                messages.close()
            if transport is not None:
                self._reconcile_retention_locked()
                transport.close()
            self._release_pending_shared_locked()
            self._process = None
            self._messages = None
            self._transport = None
            self._transport_token = None
            self._cleanup_in_progress = False
        finally:
            self._lock.release()
        if _remaining(expires_at) <= 0:
            raise PonyChartWorkerOwnershipError(
                "PonyChart retention close completed after its deadline"
            )


__all__ = [
    "PonyChartGenerationDescriptor",
    "PonyChartInferenceLease",
    "PonyChartInferenceOwner",
    "PonyChartRetentionOwner",
    "PonyChartWorkerOwnershipError",
]

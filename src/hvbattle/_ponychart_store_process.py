"""Owned process boundary for PonyChart artifact operations."""

from __future__ import annotations

import asyncio
import atexit
import json
import math
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Final
from uuid import uuid4

from hvbrowser.runtime import OwnedProcess, ProcessOwnershipError, start_owned_process

from .ponychart_model_store import (
    LoadedPonyChartGeneration,
    PonyChartArtifactError,
    PonyChartGenerationStore,
    PonyChartRefreshOutcome,
    PonyChartStoreConfig,
    PonyChartStoreRefresh,
)

_DEFAULT_OPERATION_TIMEOUT_SECONDS: Final = 120.0
_STORE_START_TIMEOUT_SECONDS: Final = 5.0
_STORE_CHILD_CONNECT_TIMEOUT_SECONDS: Final = 5.0
_STORE_CLEANUP_RESERVE_SECONDS: Final = 5.0
_STORE_GRACEFUL_EXIT_SECONDS: Final = 0.1
_STORE_TERMINATE_SECONDS: Final = 1.0
_STORE_KILL_SECONDS: Final = 1.0
_STORE_PRIVATE_CLEANUP_SECONDS: Final = 2.0
_IPC_AUTH_TOKEN_BYTES: Final = 32
_IPC_HEADER: Final = struct.Struct("!I")
_IPC_MAX_PAYLOAD_BYTES: Final = 64 * 1024


class PonyChartStoreProcessOwnershipError(ProcessOwnershipError):
    """A one-shot artifact process tree could not be proven released."""


class _StoreOperation(StrEnum):
    LOAD_OR_BOOTSTRAP = "load-or-bootstrap"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True)
class _StoreRequest:
    request_id: str
    operation: _StoreOperation
    published_generation: str | None
    semantic_expires_at: float
    config: PonyChartStoreConfig


@dataclass(slots=True)
class _OwnedStoreProcess:
    request_id: str
    auth_token: str
    process: OwnedProcess
    listener: socket.socket | None
    channel: socket.socket | None = None


type _ProcessLauncher = Callable[..., OwnedProcess]


def _remaining(expires_at: float) -> float:
    return max(0.0, expires_at - time.monotonic())


def _validate_timeout(timeout: float) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > _DEFAULT_OPERATION_TIMEOUT_SECONDS
    ):
        raise ValueError("PonyChart store timeout must be finite and in (0, 120]")
    return float(timeout)


def _resolve_operation_deadline(
    *,
    timeout: float | None,
    expires_at: float | None,
) -> tuple[float, float]:
    if timeout is None and expires_at is None:
        timeout = _DEFAULT_OPERATION_TIMEOUT_SECONDS
    elif timeout is not None and expires_at is not None:
        raise TypeError(
            "PonyChart store requires either timeout or expires_at, not both"
        )
    if timeout is not None:
        duration = _validate_timeout(timeout)
        return time.monotonic() + duration, duration
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int | float)
        or not math.isfinite(expires_at)
    ):
        raise ValueError("PonyChart store deadline must be finite")
    deadline = float(expires_at)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("PonyChart artifact deadline expired")
    if remaining > _DEFAULT_OPERATION_TIMEOUT_SECONDS:
        raise ValueError(
            "PonyChart store deadline must not be more than 120 seconds away"
        )
    return deadline, remaining


def _validate_close_timeout(timeout: float) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > _STORE_CLEANUP_RESERVE_SECONDS
    ):
        raise ValueError("PonyChart store close timeout must be finite and in (0, 5]")
    return float(timeout)


def _validate_auth_token(auth_token: str) -> None:
    if len(auth_token) != _IPC_AUTH_TOKEN_BYTES * 2:
        raise ValueError("PonyChart store IPC token has an invalid length")
    try:
        bytes.fromhex(auth_token)
    except ValueError as error:
        raise ValueError("PonyChart store IPC token is invalid") from error


def _json_bytes(message: Mapping[str, object]) -> bytes:
    payload = json.dumps(
        message,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _IPC_MAX_PAYLOAD_BYTES:
        raise ValueError("PonyChart store IPC payload is too large")
    return _IPC_HEADER.pack(len(payload)) + payload


def _decode_json_payload(payload: bytes) -> dict[str, object]:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("PonyChart store IPC returned invalid JSON") from error
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise RuntimeError("PonyChart store IPC returned an invalid object")
    return raw


def _sync_receive_exact(
    channel: socket.socket,
    size: int,
    *,
    expires_at: float,
) -> bytes:
    content = bytearray()
    while len(content) < size:
        remaining = _remaining(expires_at)
        if remaining <= 0:
            raise TimeoutError("PonyChart store IPC receive deadline expired")
        channel.settimeout(remaining)
        chunk = channel.recv(size - len(content))
        if not chunk:
            raise EOFError("PonyChart store IPC channel closed")
        content.extend(chunk)
    if _remaining(expires_at) <= 0:
        raise TimeoutError("PonyChart store IPC receive deadline expired")
    return bytes(content)


def _sync_receive_json(
    channel: socket.socket,
    *,
    expires_at: float,
) -> dict[str, object]:
    header = _sync_receive_exact(channel, _IPC_HEADER.size, expires_at=expires_at)
    (payload_size,) = _IPC_HEADER.unpack(header)
    if payload_size > _IPC_MAX_PAYLOAD_BYTES:
        raise RuntimeError("PonyChart store IPC payload is too large")
    message = _decode_json_payload(
        _sync_receive_exact(channel, payload_size, expires_at=expires_at)
    )
    if _remaining(expires_at) <= 0:
        raise TimeoutError("PonyChart store IPC receive deadline expired")
    return message


def _sync_send_json(
    channel: socket.socket,
    message: Mapping[str, object],
    *,
    expires_at: float,
) -> None:
    remaining = _remaining(expires_at)
    if remaining <= 0:
        raise TimeoutError("PonyChart store IPC send deadline expired")
    channel.settimeout(remaining)
    channel.sendall(_json_bytes(message))
    if _remaining(expires_at) <= 0:
        raise TimeoutError("PonyChart store IPC send deadline expired")


async def _async_receive_exact(
    channel: socket.socket,
    size: int,
    *,
    expires_at: float,
) -> bytes:
    content = bytearray()
    loop = asyncio.get_running_loop()
    while len(content) < size:
        remaining = _remaining(expires_at)
        if remaining <= 0:
            raise TimeoutError("PonyChart store IPC receive deadline expired")
        try:
            async with asyncio.timeout_at(expires_at):
                chunk = await loop.sock_recv(channel, size - len(content))
        except TimeoutError as error:
            raise TimeoutError(
                "PonyChart store IPC receive deadline expired"
            ) from error
        if not chunk:
            raise EOFError("PonyChart store IPC channel closed")
        content.extend(chunk)
    if _remaining(expires_at) <= 0:
        raise TimeoutError("PonyChart store IPC receive deadline expired")
    return bytes(content)


async def _async_receive_json(
    channel: socket.socket,
    *,
    expires_at: float,
) -> dict[str, object]:
    header = await _async_receive_exact(
        channel,
        _IPC_HEADER.size,
        expires_at=expires_at,
    )
    (payload_size,) = _IPC_HEADER.unpack(header)
    if payload_size > _IPC_MAX_PAYLOAD_BYTES:
        raise RuntimeError("PonyChart store IPC payload is too large")
    message = _decode_json_payload(
        await _async_receive_exact(channel, payload_size, expires_at=expires_at)
    )
    if _remaining(expires_at) <= 0:
        raise TimeoutError("PonyChart store IPC receive deadline expired")
    return message


async def _async_send_json(
    channel: socket.socket,
    message: Mapping[str, object],
    *,
    expires_at: float,
) -> None:
    remaining = _remaining(expires_at)
    if remaining <= 0:
        raise TimeoutError("PonyChart store IPC send deadline expired")
    try:
        async with asyncio.timeout_at(expires_at):
            await asyncio.get_running_loop().sock_sendall(
                channel,
                _json_bytes(message),
            )
    except TimeoutError as error:
        raise TimeoutError("PonyChart store IPC send deadline expired") from error
    if _remaining(expires_at) <= 0:
        raise TimeoutError("PonyChart store IPC send deadline expired")


def _config_as_json(config: PonyChartStoreConfig) -> dict[str, object]:
    return {
        "root": None if config.root is None else str(config.root),
        "baseUrl": config.base_url,
        "connectTimeout": config.connect_timeout,
        "readIdleTimeout": config.read_idle_timeout,
        "refreshTimeout": config.refresh_timeout,
        "maxModelBytes": config.max_model_bytes,
        "maxThresholdsBytes": config.max_thresholds_bytes,
    }


def _config_from_json(raw: object) -> PonyChartStoreConfig:
    if not isinstance(raw, dict) or set(raw) != {
        "root",
        "baseUrl",
        "connectTimeout",
        "readIdleTimeout",
        "refreshTimeout",
        "maxModelBytes",
        "maxThresholdsBytes",
    }:
        raise ValueError("PonyChart store child received invalid configuration")
    root = raw["root"]
    base_url = raw["baseUrl"]
    if (root is not None and not isinstance(root, str)) or not isinstance(
        base_url, str
    ):
        raise ValueError("PonyChart store child received invalid paths")
    duration_values = (
        raw["connectTimeout"],
        raw["readIdleTimeout"],
        raw["refreshTimeout"],
    )
    size_values = (raw["maxModelBytes"], raw["maxThresholdsBytes"])
    if any(
        isinstance(value, bool) or not isinstance(value, int | float)
        for value in duration_values
    ) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in size_values
    ):
        raise ValueError("PonyChart store child received invalid configuration values")
    try:
        return PonyChartStoreConfig(
            root=None if root is None else Path(root),
            base_url=base_url,
            connect_timeout=float(raw["connectTimeout"]),
            read_idle_timeout=float(raw["readIdleTimeout"]),
            refresh_timeout=float(raw["refreshTimeout"]),
            max_model_bytes=int(raw["maxModelBytes"]),
            max_thresholds_bytes=int(raw["maxThresholdsBytes"]),
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            "PonyChart store child received invalid configuration values"
        ) from error


def _request_as_json(request: _StoreRequest) -> dict[str, object]:
    return {
        "type": "request",
        "requestId": request.request_id,
        "operation": request.operation.value,
        "publishedGeneration": request.published_generation,
        "semanticExpiresAt": request.semantic_expires_at,
        "config": _config_as_json(request.config),
    }


def _request_from_json(raw: Mapping[str, object]) -> _StoreRequest:
    if (
        set(raw)
        != {
            "type",
            "requestId",
            "operation",
            "publishedGeneration",
            "semanticExpiresAt",
            "config",
        }
        or raw.get("type") != "request"
    ):
        raise ValueError("PonyChart store child received invalid request")
    request_id = raw["requestId"]
    published_generation = raw["publishedGeneration"]
    semantic_expires_at = raw["semanticExpiresAt"]
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("PonyChart store child received invalid request id")
    if published_generation is not None and not isinstance(published_generation, str):
        raise ValueError("PonyChart store child received invalid generation")
    if isinstance(semantic_expires_at, bool) or not isinstance(
        semantic_expires_at, int | float
    ):
        raise ValueError("PonyChart store child received invalid deadline")
    try:
        operation = _StoreOperation(str(raw["operation"]))
    except ValueError as error:
        raise ValueError("PonyChart store child received invalid operation") from error
    return _StoreRequest(
        request_id=request_id,
        operation=operation,
        published_generation=published_generation,
        semantic_expires_at=float(semantic_expires_at),
        config=_config_from_json(raw["config"]),
    )


def _loaded_as_json(loaded: LoadedPonyChartGeneration) -> dict[str, object]:
    return {
        "generation": loaded.generation,
        "modelPath": str(loaded.model_path),
        "thresholdsPath": str(loaded.thresholds_path),
    }


def _loaded_from_json(raw: object) -> LoadedPonyChartGeneration:
    if not isinstance(raw, dict) or set(raw) != {
        "generation",
        "modelPath",
        "thresholdsPath",
    }:
        raise RuntimeError("PonyChart store child returned invalid generation data")
    values = tuple(raw.values())
    if any(not isinstance(value, str) for value in values):
        raise RuntimeError("PonyChart store child returned invalid generation fields")
    generation = raw["generation"]
    model_path = raw["modelPath"]
    thresholds_path = raw["thresholdsPath"]
    assert isinstance(generation, str)
    assert isinstance(model_path, str)
    assert isinstance(thresholds_path, str)
    if len(generation) != 64:
        raise RuntimeError("PonyChart store child returned invalid generation id")
    try:
        bytes.fromhex(generation)
    except ValueError as error:
        raise RuntimeError(
            "PonyChart store child returned invalid generation id"
        ) from error
    return LoadedPonyChartGeneration(
        generation=generation,
        model_path=Path(model_path),
        thresholds_path=Path(thresholds_path),
    )


def _success_as_json(
    request_id: str,
    result: PonyChartStoreRefresh,
) -> dict[str, object]:
    return {
        "type": "success",
        "requestId": request_id,
        "outcome": result.outcome.value,
        "loaded": None if result.loaded is None else _loaded_as_json(result.loaded),
    }


def _result_from_json(
    raw: Mapping[str, object],
    *,
    request_id: str,
) -> PonyChartStoreRefresh:
    if raw.get("requestId") != request_id:
        raise RuntimeError("PonyChart store child returned a mismatched receipt")
    if raw.get("type") == "failure":
        error_type = raw.get("errorType")
        if not isinstance(error_type, str) or not error_type.isidentifier():
            raise RuntimeError("PonyChart store child returned invalid failure data")
        raise PonyChartArtifactError(
            f"PonyChart artifact child failed: error_type={error_type}"
        )
    if raw.get("type") != "success" or set(raw) != {
        "type",
        "requestId",
        "outcome",
        "loaded",
    }:
        raise RuntimeError("PonyChart store child returned an invalid receipt")
    try:
        outcome = PonyChartRefreshOutcome(str(raw["outcome"]))
    except ValueError as error:
        raise RuntimeError("PonyChart store child returned invalid outcome") from error
    return PonyChartStoreRefresh(
        outcome=outcome,
        loaded=None if raw["loaded"] is None else _loaded_from_json(raw["loaded"]),
    )


def run_store_child(port: int, auth_token: str) -> None:
    """Execute all network, filesystem, hash, and ONNX work in the child."""

    if not 1 <= port <= 65_535:
        raise ValueError("PonyChart store IPC port is out of range")
    _validate_auth_token(auth_token)
    channel = socket.create_connection(
        ("127.0.0.1", port),
        timeout=_STORE_CHILD_CONNECT_TIMEOUT_SECONDS,
    )
    try:
        handshake_expires_at = time.monotonic() + _STORE_CHILD_CONNECT_TIMEOUT_SECONDS
        _sync_send_json(
            channel,
            {"type": "ready", "authToken": auth_token},
            expires_at=handshake_expires_at,
        )
        request = _request_from_json(
            _sync_receive_json(channel, expires_at=handshake_expires_at)
        )
        try:
            store = PonyChartGenerationStore.from_config(request.config)
            if request.operation is _StoreOperation.LOAD_OR_BOOTSTRAP:
                loaded = store.load_or_bootstrap(deadline=request.semantic_expires_at)
                result = PonyChartStoreRefresh(
                    PonyChartRefreshOutcome.UPDATED,
                    loaded,
                )
            else:
                result = store.refresh(
                    request.published_generation,
                    deadline=request.semantic_expires_at,
                )
            response = _success_as_json(request.request_id, result)
        except BaseException as error:
            response = {
                "type": "failure",
                "requestId": request.request_id,
                "errorType": type(error).__name__,
            }
        _sync_send_json(
            channel,
            response,
            expires_at=request.semantic_expires_at,
        )
    finally:
        channel.close()


async def _settle_task_despite_cancellation(
    task: asyncio.Task[int],
) -> tuple[int | None, BaseException | None, asyncio.CancelledError | None]:
    delayed_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if delayed_cancellation is None:
                delayed_cancellation = error
    try:
        return task.result(), None, delayed_cancellation
    except BaseException as error:
        return None, error, delayed_cancellation


class PonyChartStoreProcessOwner:
    """Serialize one-shot store children and prove tree exit before return."""

    def __init__(
        self,
        *,
        config: PonyChartStoreConfig,
        process_launcher: _ProcessLauncher = start_owned_process,
        child_module: str = "hvbattle._ponychart_store_child",
        register_atexit: bool = True,
    ) -> None:
        self._config = config
        self._process_launcher = process_launcher
        self._child_module = child_module
        self._operation_lock = asyncio.Lock()
        self._owned: dict[int, _OwnedStoreProcess] = {}
        self._owned_lock = threading.Lock()
        self._closed = False
        if register_atexit:
            atexit.register(self.close_sync, timeout=5.0)

    @classmethod
    def default(cls) -> PonyChartStoreProcessOwner:
        return cls(config=PonyChartGenerationStore.default_config())

    async def load_or_bootstrap(
        self,
        *,
        timeout: float | None = None,
        expires_at: float | None = None,
    ) -> LoadedPonyChartGeneration:
        result = await self._run(
            _StoreOperation.LOAD_OR_BOOTSTRAP,
            published_generation=None,
            timeout=timeout,
            expires_at=expires_at,
        )
        if result.loaded is None:
            raise RuntimeError("PonyChart bootstrap child returned no generation")
        return result.loaded

    async def refresh(
        self,
        published_generation: str | None,
        *,
        timeout: float | None = None,
        expires_at: float | None = None,
    ) -> PonyChartStoreRefresh:
        return await self._run(
            _StoreOperation.REFRESH,
            published_generation=published_generation,
            timeout=timeout,
            expires_at=expires_at,
        )

    def _start(
        self,
        request: _StoreRequest,
        *,
        semantic_expires_at: float,
        expires_at: float,
    ) -> _OwnedStoreProcess:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.setblocking(False)
            port = int(listener.getsockname()[1])
            auth_token = secrets.token_hex(_IPC_AUTH_TOKEN_BYTES)
            if _remaining(semantic_expires_at) <= 0:
                raise TimeoutError(
                    "PonyChart artifact deadline expired before process startup"
                )
            process = self._process_launcher(
                sys.executable,
                ["-m", self._child_module, str(port), auth_token],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                forward_logging=True,
                startup_timeout=min(
                    _STORE_START_TIMEOUT_SECONDS,
                    _remaining(semantic_expires_at),
                ),
                deadline=expires_at,
            )
        except BaseException:
            listener.close()
            raise
        state = _OwnedStoreProcess(
            request.request_id,
            auth_token,
            process,
            listener,
        )
        with self._owned_lock:
            self._owned[id(state)] = state
        return state

    async def _connect(
        self,
        state: _OwnedStoreProcess,
        request: _StoreRequest,
        *,
        semantic_expires_at: float,
    ) -> socket.socket:
        listener = state.listener
        if listener is None:
            raise RuntimeError("PonyChart store listener ownership was lost")
        remaining = _remaining(semantic_expires_at)
        if remaining <= 0:
            raise TimeoutError("PonyChart artifact process handshake timed out")
        try:
            async with asyncio.timeout_at(semantic_expires_at):
                channel, peer = await asyncio.get_running_loop().sock_accept(listener)
        except TimeoutError as error:
            raise TimeoutError(
                "PonyChart artifact process handshake timed out"
            ) from error
        state.channel = channel
        listener.close()
        state.listener = None
        channel.setblocking(False)
        if peer[0] != "127.0.0.1":
            raise RuntimeError("PonyChart store IPC peer is not local")
        ready = await _async_receive_json(
            channel,
            expires_at=semantic_expires_at,
        )
        if ready != {"type": "ready", "authToken": state.auth_token}:
            raise RuntimeError("PonyChart store child failed authentication")
        await _async_send_json(
            channel,
            _request_as_json(request),
            expires_at=semantic_expires_at,
        )
        return channel

    async def _settle_owned(
        self,
        state: _OwnedStoreProcess,
        *,
        expires_at: float,
    ) -> asyncio.CancelledError | None:
        for channel in (state.channel, state.listener):
            if channel is not None:
                try:
                    channel.close()
                except OSError:
                    pass
        shutdown = asyncio.create_task(
            asyncio.to_thread(
                partial(
                    state.process.shutdown,
                    graceful_timeout=_STORE_GRACEFUL_EXIT_SECONDS,
                    terminate_timeout=_STORE_TERMINATE_SECONDS,
                    kill_timeout=_STORE_KILL_SECONDS,
                    cleanup_timeout=_STORE_PRIVATE_CLEANUP_SECONDS,
                    deadline=expires_at,
                )
            )
        )
        (
            returncode,
            cleanup_error,
            delayed_cancellation,
        ) = await _settle_task_despite_cancellation(shutdown)
        if cleanup_error is not None:
            raise PonyChartStoreProcessOwnershipError(
                "PonyChart store process-tree cleanup failed"
            ) from cleanup_error
        if type(returncode) is not int:
            raise PonyChartStoreProcessOwnershipError(
                "PonyChart store process owner returned no exit proof"
            )
        state.channel = None
        state.listener = None
        with self._owned_lock:
            self._owned.pop(id(state), None)
        if _remaining(expires_at) <= 0:
            raise PonyChartStoreProcessOwnershipError(
                "PonyChart store process cleanup completed after its deadline"
            )
        return delayed_cancellation

    async def _run(
        self,
        operation: _StoreOperation,
        *,
        published_generation: str | None,
        timeout: float | None,
        expires_at: float | None,
    ) -> PonyChartStoreRefresh:
        expires_at, operation_timeout = _resolve_operation_deadline(
            timeout=timeout,
            expires_at=expires_at,
        )
        semantic_expires_at = expires_at - min(
            _STORE_CLEANUP_RESERVE_SECONDS,
            operation_timeout / 2.0,
        )
        try:
            async with asyncio.timeout_at(expires_at):
                await self._operation_lock.acquire()
        except TimeoutError as error:
            raise TimeoutError(
                "PonyChart artifact deadline expired waiting for operation ownership"
            ) from error
        try:
            if self._closed:
                raise RuntimeError("PonyChart store process owner is closed")
            request = _StoreRequest(
                request_id=uuid4().hex,
                operation=operation,
                published_generation=published_generation,
                semantic_expires_at=semantic_expires_at,
                config=self._config,
            )
            state = self._start(
                request,
                semantic_expires_at=semantic_expires_at,
                expires_at=expires_at,
            )
            primary_error: BaseException | None = None
            result: PonyChartStoreRefresh | None = None
            try:
                channel = await self._connect(
                    state,
                    request,
                    semantic_expires_at=semantic_expires_at,
                )
                response = await _async_receive_json(
                    channel,
                    expires_at=semantic_expires_at,
                )
                result = _result_from_json(response, request_id=request.request_id)
                if _remaining(semantic_expires_at) <= 0:
                    raise TimeoutError(
                        "PonyChart artifact receipt arrived after its deadline"
                    )
            except BaseException as error:
                primary_error = error

            try:
                delayed_cancellation = await self._settle_owned(
                    state,
                    expires_at=expires_at,
                )
            except BaseException as ownership_error:
                if primary_error is not None:
                    ownership_error.add_note(
                        "PonyChart store operation also ended with "
                        f"{type(primary_error).__name__}"
                    )
                raise
            cancellation = (
                primary_error
                if isinstance(primary_error, asyncio.CancelledError)
                else delayed_cancellation
            )
            if cancellation is not None:
                raise cancellation
            if primary_error is not None:
                raise primary_error
            if result is None:
                raise RuntimeError("PonyChart store child receipt disappeared")
            if _remaining(expires_at) <= 0:
                raise TimeoutError(
                    "PonyChart artifact cleanup completed after its deadline"
                )
            return result
        finally:
            self._operation_lock.release()

    async def close(self, *, timeout: float) -> None:
        timeout = _validate_close_timeout(timeout)
        await self._close_at(expires_at=time.monotonic() + timeout)

    async def _close_at(self, *, expires_at: float) -> None:
        """Close against a caller-owned absolute monotonic deadline."""

        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int | float)
            or not math.isfinite(expires_at)
        ):
            raise ValueError("PonyChart store close deadline must be finite")
        expires_at = float(expires_at)
        if _remaining(expires_at) <= 0:
            raise PonyChartStoreProcessOwnershipError(
                "PonyChart store close deadline expired before cleanup"
            )
        try:
            async with asyncio.timeout_at(expires_at):
                await self._operation_lock.acquire()
        except TimeoutError as error:
            raise PonyChartStoreProcessOwnershipError(
                "PonyChart store close could not acquire operation ownership"
            ) from error
        try:
            if _remaining(expires_at) <= 0:
                raise PonyChartStoreProcessOwnershipError(
                    "PonyChart store operation ownership arrived after close deadline"
                )
            self._closed = True
            with self._owned_lock:
                states = tuple(self._owned.values())
            if _remaining(expires_at) <= 0:
                raise PonyChartStoreProcessOwnershipError(
                    "PonyChart store state snapshot completed after close deadline"
                )
            cancellations: list[asyncio.CancelledError] = []
            errors: list[BaseException] = []
            for state in states:
                try:
                    cancellation = await self._settle_owned(
                        state,
                        expires_at=expires_at,
                    )
                except BaseException as error:
                    errors.append(error)
                else:
                    if cancellation is not None:
                        cancellations.append(cancellation)
            if errors:
                raise PonyChartStoreProcessOwnershipError(
                    f"Failed to reap {len(errors)} PonyChart store process tree(s)"
                ) from errors[0]
            if _remaining(expires_at) <= 0:
                raise PonyChartStoreProcessOwnershipError(
                    "PonyChart store close completed after its deadline"
                )
            if cancellations:
                raise cancellations[0]
        finally:
            self._operation_lock.release()

    def close_sync(self, *, timeout: float) -> None:
        timeout = _validate_close_timeout(timeout)
        expires_at = time.monotonic() + timeout
        if _remaining(expires_at) <= 0:
            raise PonyChartStoreProcessOwnershipError(
                "PonyChart store close deadline expired before cleanup"
            )
        self._closed = True
        remaining = _remaining(expires_at)
        if remaining <= 0 or not self._owned_lock.acquire(timeout=remaining):
            raise PonyChartStoreProcessOwnershipError(
                "PonyChart store close could not acquire state ownership"
            )
        try:
            states = tuple(self._owned.values())
        finally:
            self._owned_lock.release()
        if _remaining(expires_at) <= 0:
            raise PonyChartStoreProcessOwnershipError(
                "PonyChart store state snapshot completed after close deadline"
            )
        errors: list[BaseException] = []
        for state in states:
            for channel in (state.channel, state.listener):
                if channel is not None:
                    try:
                        channel.close()
                    except OSError:
                        pass
            try:
                returncode = state.process.shutdown(
                    graceful_timeout=_STORE_GRACEFUL_EXIT_SECONDS,
                    terminate_timeout=_STORE_TERMINATE_SECONDS,
                    kill_timeout=_STORE_KILL_SECONDS,
                    cleanup_timeout=_STORE_PRIVATE_CLEANUP_SECONDS,
                    deadline=expires_at,
                )
                if type(returncode) is not int:
                    raise ProcessOwnershipError("Process owner returned no exit proof")
                remaining = _remaining(expires_at)
                if remaining <= 0 or not self._owned_lock.acquire(timeout=remaining):
                    raise PonyChartStoreProcessOwnershipError(
                        "PonyChart store close could not acquire state ownership"
                    )
                try:
                    self._owned.pop(id(state), None)
                finally:
                    self._owned_lock.release()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise PonyChartStoreProcessOwnershipError(
                f"Failed to reap {len(errors)} PonyChart store process tree(s)"
            ) from errors[0]
        if _remaining(expires_at) <= 0:
            raise PonyChartStoreProcessOwnershipError(
                "PonyChart store close completed after its deadline"
            )


__all__ = [
    "PonyChartStoreProcessOwner",
    "PonyChartStoreProcessOwnershipError",
]

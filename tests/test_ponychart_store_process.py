from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import pytest
from hvbrowser.runtime import OwnedProcess

import hvbattle._ponychart_store_process as process_module
from hvbattle.ponychart_model_store import (
    LoadedPonyChartGeneration,
    PonyChartArtifactError,
    PonyChartGenerationStore,
    PonyChartRefreshOutcome,
    PonyChartStoreConfig,
    PonyChartStoreRefresh,
)

type _ConnectAction = Callable[
    [socket.socket, Mapping[str, object], process_module._OwnedStoreProcess],
    None,
]


def _loaded(root: Path) -> LoadedPonyChartGeneration:
    generation = "a" * 64
    directory = root / "generations" / generation
    return LoadedPonyChartGeneration(
        generation=generation,
        model_path=directory / "model.onnx",
        thresholds_path=directory / "thresholds.json",
    )


def _success(request: Mapping[str, object], root: Path) -> dict[str, object]:
    return process_module._success_as_json(
        cast(str, request["requestId"]),
        PonyChartStoreRefresh(
            PonyChartRefreshOutcome.UPDATED,
            _loaded(root),
        ),
    )


class _FakeOwnedProcess:
    def __init__(self, *, shutdown_delay: float = 0.0) -> None:
        self.peers: list[socket.socket] = []
        self.shutdown_delay = shutdown_delay
        self.shutdown_deadlines: list[float] = []

    def shutdown(
        self,
        *,
        graceful_timeout: float,
        terminate_timeout: float,
        kill_timeout: float,
        cleanup_timeout: float,
        deadline: float,
    ) -> int:
        del graceful_timeout, terminate_timeout, kill_timeout, cleanup_timeout
        self.shutdown_deadlines.append(deadline)
        if self.shutdown_delay:
            time.sleep(self.shutdown_delay)
        for peer in self.peers:
            peer.close()
        self.peers.clear()
        return 0


def _owner(
    root: Path,
    action: _ConnectAction,
    *,
    shutdown_delay: float = 0.0,
) -> tuple[process_module.PonyChartStoreProcessOwner, _FakeOwnedProcess]:
    owner = process_module.PonyChartStoreProcessOwner(
        config=PonyChartStoreConfig(root=root),
        process_launcher=Mock(),
        register_atexit=False,
    )
    process = _FakeOwnedProcess(shutdown_delay=shutdown_delay)

    def start(
        request: process_module._StoreRequest,
        *,
        semantic_expires_at: float,
        expires_at: float,
    ) -> process_module._OwnedStoreProcess:
        del semantic_expires_at, expires_at
        state = process_module._OwnedStoreProcess(
            request.request_id,
            "a" * 64,
            cast(OwnedProcess, process),
            None,
        )
        owner._owned[id(state)] = state
        return state

    async def connect(
        state: process_module._OwnedStoreProcess,
        request: process_module._StoreRequest,
        *,
        semantic_expires_at: float,
    ) -> socket.socket:
        del semantic_expires_at
        parent, child = socket.socketpair()
        parent.setblocking(False)
        state.channel = parent
        process.peers.append(child)
        action(child, process_module._request_as_json(request), state)
        return parent

    owner._start = start  # type: ignore[method-assign]
    owner._connect = connect  # type: ignore[method-assign]
    return owner, process


def test_default_config_crosses_ipc_without_import_time_path_resolution() -> None:
    config = PonyChartGenerationStore.default_config()

    assert config.root is None
    encoded = process_module._config_as_json(config)
    assert encoded["root"] is None
    assert process_module._config_from_json(encoded) == config


def _send_success(
    channel: socket.socket,
    request: Mapping[str, object],
    state: process_module._OwnedStoreProcess,
) -> None:
    del state
    config = request["config"]
    assert isinstance(config, dict)
    root = config["root"]
    assert isinstance(root, str)
    channel.sendall(process_module._json_bytes(_success(request, Path(root))))
    channel.close()


def _send_failure(
    channel: socket.socket,
    request: Mapping[str, object],
    state: process_module._OwnedStoreProcess,
) -> None:
    del state
    channel.sendall(
        process_module._json_bytes(
            {
                "type": "failure",
                "requestId": request["requestId"],
                "errorType": "PrivateFailure",
            }
        )
    )
    channel.close()


def _keep_open(
    _channel: socket.socket,
    _request: Mapping[str, object],
    _state: process_module._OwnedStoreProcess,
) -> None:
    return


def test_success_returns_descriptor_after_process_tree_is_reaped(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        owner, process = _owner(tmp_path, _send_success)

        loaded = await owner.load_or_bootstrap(timeout=5.0)

        assert loaded == _loaded(tmp_path)
        assert len(process.shutdown_deadlines) == 1
        assert owner._owned == {}

    asyncio.run(exercise())


def test_supervisor_start_receives_one_shared_absolute_deadline(
    tmp_path: Path,
) -> None:
    listener = Mock(spec=socket.socket)
    listener.getsockname.return_value = ("127.0.0.1", 43123)
    process = Mock(spec=OwnedProcess)
    launcher = Mock(return_value=process)
    owner = process_module.PonyChartStoreProcessOwner(
        config=PonyChartStoreConfig(root=tmp_path),
        process_launcher=launcher,
        register_atexit=False,
    )
    now = time.monotonic()
    request = process_module._StoreRequest(
        "request-id",
        process_module._StoreOperation.LOAD_OR_BOOTSTRAP,
        None,
        now + 10.0,
        PonyChartStoreConfig(root=tmp_path),
    )

    with patch(
        "hvbattle._ponychart_store_process.socket.socket",
        return_value=listener,
    ):
        state = owner._start(
            request,
            semantic_expires_at=now + 10.0,
            expires_at=now + 15.0,
        )

    assert state.process is process
    assert launcher.call_args.kwargs["forward_logging"] is True
    assert launcher.call_args.kwargs["startup_timeout"] <= 5.0
    assert launcher.call_args.kwargs["deadline"] == now + 15.0
    assert owner._owned[id(state)] is state


def test_child_failure_contains_only_its_type(tmp_path: Path) -> None:
    async def exercise() -> None:
        owner, _ = _owner(tmp_path, _send_failure)

        with pytest.raises(PonyChartArtifactError) as raised:
            await owner.refresh("b" * 64, timeout=5.0)

        assert str(raised.value).endswith("error_type=PrivateFailure")
        assert owner._owned == {}

    asyncio.run(exercise())


def test_timeout_reaps_the_owned_tree_inside_the_total_budget(tmp_path: Path) -> None:
    async def exercise() -> None:
        owner, _ = _owner(tmp_path, _keep_open)
        started = time.monotonic()

        with pytest.raises(TimeoutError, match="receive deadline"):
            await owner.load_or_bootstrap(timeout=0.1)

        assert time.monotonic() - started < 0.5
        assert owner._owned == {}

    asyncio.run(exercise())


def test_sync_receipt_decode_finishing_after_deadline_is_rejected() -> None:
    parent, child = socket.socketpair()
    original_decode = process_module._decode_json_payload

    def slow_decode(payload: bytes) -> dict[str, object]:
        time.sleep(0.03)
        return original_decode(payload)

    try:
        child.sendall(process_module._json_bytes({"type": "ready"}))
        with (
            patch.object(process_module, "_decode_json_payload", slow_decode),
            pytest.raises(TimeoutError, match="receive deadline"),
        ):
            process_module._sync_receive_json(
                parent,
                expires_at=time.monotonic() + 0.01,
            )
    finally:
        parent.close()
        child.close()


def test_async_receipt_decode_finishing_after_deadline_is_rejected() -> None:
    async def exercise() -> None:
        parent, child = socket.socketpair()
        parent.setblocking(False)
        original_decode = process_module._decode_json_payload

        def slow_decode(payload: bytes) -> dict[str, object]:
            time.sleep(0.03)
            return original_decode(payload)

        try:
            child.sendall(process_module._json_bytes({"type": "ready"}))
            with (
                patch.object(process_module, "_decode_json_payload", slow_decode),
                pytest.raises(TimeoutError, match="receive deadline"),
            ):
                await process_module._async_receive_json(
                    parent,
                    expires_at=time.monotonic() + 0.01,
                )
        finally:
            parent.close()
            child.close()

    asyncio.run(exercise())


def test_result_parsing_finishing_after_semantic_deadline_is_rejected(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        owner, _ = _owner(tmp_path, _send_success)
        original_result = process_module._result_from_json

        def slow_result(
            message: Mapping[str, object],
            *,
            request_id: str,
        ) -> PonyChartStoreRefresh:
            time.sleep(0.08)
            return original_result(message, request_id=request_id)

        with (
            patch.object(process_module, "_result_from_json", slow_result),
            pytest.raises(TimeoutError, match="receipt arrived after"),
        ):
            await owner.load_or_bootstrap(timeout=0.12)

        assert owner._owned == {}

    asyncio.run(exercise())


def test_repeated_cancellation_cannot_interrupt_tree_reaping(tmp_path: Path) -> None:
    async def exercise() -> None:
        connected = asyncio.Event()

        def connected_open(
            channel: socket.socket,
            request: Mapping[str, object],
            state: process_module._OwnedStoreProcess,
        ) -> None:
            del channel, request, state
            connected.set()

        owner, _ = _owner(tmp_path, connected_open, shutdown_delay=0.05)
        running = asyncio.create_task(owner.load_or_bootstrap(timeout=5.0))
        await connected.wait()

        running.cancel()
        await asyncio.sleep(0)
        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await running
        assert owner._owned == {}

    asyncio.run(exercise())


def test_reply_is_not_success_until_process_tree_is_reaped(tmp_path: Path) -> None:
    async def exercise() -> None:
        owner, process = _owner(tmp_path, _send_success, shutdown_delay=0.05)
        started = time.monotonic()

        loaded = await owner.load_or_bootstrap(timeout=5.0)

        assert loaded.generation == "a" * 64
        assert time.monotonic() - started >= 0.04
        assert process.shutdown_deadlines

    asyncio.run(exercise())


def test_commit_then_reply_loss_is_adoptable_next_run(tmp_path: Path) -> None:
    def commit_without_reply(
        channel: socket.socket,
        request: Mapping[str, object],
        state: process_module._OwnedStoreProcess,
    ) -> None:
        del state
        (tmp_path / "committed").write_text("committed", encoding="utf-8")
        channel.close()

    def adopt(
        channel: socket.socket,
        request: Mapping[str, object],
        state: process_module._OwnedStoreProcess,
    ) -> None:
        assert (tmp_path / "committed").is_file()
        _send_success(channel, request, state)

    async def exercise() -> None:
        first, _ = _owner(tmp_path, commit_without_reply)
        with pytest.raises(EOFError, match="channel closed"):
            await first.load_or_bootstrap(timeout=5.0)
        assert first._owned == {}

        adopter, _ = _owner(tmp_path, adopt)
        loaded = await adopter.load_or_bootstrap(timeout=5.0)
        assert loaded.generation == "a" * 64
        assert adopter._owned == {}

    asyncio.run(exercise())


def test_operation_lock_wait_counts_against_the_total_deadline(tmp_path: Path) -> None:
    async def exercise() -> None:
        owner, _ = _owner(tmp_path, _send_success)
        await owner._operation_lock.acquire()
        try:
            with pytest.raises(TimeoutError, match="operation ownership"):
                await owner.load_or_bootstrap(timeout=0.01)
        finally:
            owner._operation_lock.release()
        assert owner._owned == {}

    asyncio.run(exercise())


def test_absolute_operation_deadline_is_not_reset_after_lock_wait(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        owner, _ = _owner(tmp_path, _send_success)
        await owner._operation_lock.acquire()
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="operation ownership"):
                await owner.load_or_bootstrap(expires_at=started + 0.05)
        finally:
            owner._operation_lock.release()

        assert time.monotonic() - started < 0.5
        assert owner._owned == {}

    asyncio.run(exercise())


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), 120.1, True])
def test_timeout_is_bounded(timeout: float, tmp_path: Path) -> None:
    owner, _ = _owner(tmp_path, _send_success)

    with pytest.raises(ValueError, match=r"in \(0, 120\]"):
        asyncio.run(owner.load_or_bootstrap(timeout=timeout))


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), 5.1, True])
def test_close_timeout_is_bounded(timeout: float, tmp_path: Path) -> None:
    owner, _ = _owner(tmp_path, _send_success)

    with pytest.raises(ValueError, match=r"in \(0, 5\]"):
        asyncio.run(owner.close(timeout=timeout))


def test_async_empty_close_cannot_return_after_deadline(tmp_path: Path) -> None:
    async def exercise() -> None:
        owner, _ = _owner(tmp_path, _send_success)

        with (
            patch.object(
                process_module,
                "_remaining",
                side_effect=(1.0, 1.0, 1.0, 0.0),
            ),
            pytest.raises(
                process_module.PonyChartStoreProcessOwnershipError,
                match="store close completed after",
            ),
        ):
            await owner.close(timeout=1.0)

    asyncio.run(exercise())


def test_sync_empty_close_cannot_return_after_deadline(tmp_path: Path) -> None:
    owner, _ = _owner(tmp_path, _send_success)

    with (
        patch.object(
            process_module,
            "_remaining",
            side_effect=(1.0, 1.0, 1.0, 0.0),
        ),
        pytest.raises(
            process_module.PonyChartStoreProcessOwnershipError,
            match="store close completed after",
        ),
    ):
        owner.close_sync(timeout=1.0)


def test_store_cleanup_primitive_rejects_late_exit_proof(tmp_path: Path) -> None:
    async def exercise() -> None:
        owner, process = _owner(tmp_path, _send_success)
        state = process_module._OwnedStoreProcess(
            "request-id",
            "a" * 64,
            cast(OwnedProcess, process),
            None,
        )
        owner._owned[id(state)] = state

        with (
            patch.object(process_module, "_remaining", return_value=0.0),
            pytest.raises(
                process_module.PonyChartStoreProcessOwnershipError,
                match="cleanup completed after",
            ),
        ):
            await owner._settle_owned(
                state,
                expires_at=time.monotonic() + 1.0,
            )

        assert owner._owned == {}

    asyncio.run(exercise())

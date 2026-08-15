import asyncio
import errno
import json
import multiprocessing
import os
import shutil
import ssl
import sys
import tempfile
import threading
import unittest
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import URLError

import hvbattle.hv_battle_ponychart as ponychart_module
import hvbattle.ponychart_model_store as store_module
from hvbattle.hv_battle_ponychart import PonyChart
from hvbattle.ponychart_model_store import (
    LoadedPonyChartGeneration,
    PonyChartArtifactError,
    PonyChartGenerationStore,
    PonyChartRefreshOutcome,
    PonyChartStoreRefresh,
)


def _hold_store_process_lock(root: str, connection: object) -> None:
    store = PonyChartGenerationStore(root=Path(root))
    with store._process_lock():
        connection.send("locked")  # type: ignore[attr-defined]
        connection.recv()  # type: ignore[attr-defined]
    connection.close()  # type: ignore[attr-defined]


class _Response:
    def __init__(
        self,
        content: bytes = b"",
        *,
        etag: str | None = None,
        final_url: str,
        content_length: str | None = None,
    ) -> None:
        self._content = content
        self._offset = 0
        self._final_url = final_url
        self.headers: dict[str, str] = {}
        if etag is not None:
            self.headers["ETag"] = etag
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._final_url

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._content):
            return b""
        end = len(self._content) if size < 0 else self._offset + size
        chunk = self._content[self._offset : end]
        self._offset += len(chunk)
        return chunk

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)


class _Remote:
    def __init__(
        self,
        *,
        model: bytes,
        thresholds: bytes,
        model_etag: str | None,
        thresholds_etag: str | None,
    ) -> None:
        self.content = {
            "model.onnx": model,
            "thresholds.json": thresholds,
        }
        self.etags = {
            "model.onnx": model_etag,
            "thresholds.json": thresholds_etag,
        }
        self.get_etags = dict(self.etags)
        self.content_lengths: dict[str, str | None] = {
            filename: str(len(content)) for filename, content in self.content.items()
        }
        self.head_final_urls: dict[str, str] = {}
        self.get_final_urls: dict[str, str] = {}
        self.extra_get_headers: dict[str, dict[str, str]] = {}
        self.head_etags: dict[str, list[str | None]] = {}
        self.fail_get: str | None = None
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self._head_counts: Counter[str] = Counter()

    def __call__(self, request: object, **kwargs: object) -> _Response:
        method = request.get_method()  # type: ignore[attr-defined]
        requested_url = request.full_url  # type: ignore[attr-defined]
        filename = requested_url.rsplit("/", 1)[-1]
        self.calls.append((method, filename, kwargs))
        if method == "HEAD":
            index = self._head_counts[filename]
            self._head_counts[filename] += 1
            scripted = self.head_etags.get(filename)
            etag = (
                scripted[min(index, len(scripted) - 1)]
                if scripted
                else self.etags[filename]
            )
            return _Response(
                etag=etag,
                final_url=self.head_final_urls.get(filename, requested_url),
            )
        if filename == self.fail_get:
            raise URLError("offline failure")
        response = _Response(
            self.content[filename],
            etag=self.get_etags[filename],
            final_url=self.get_final_urls.get(filename, requested_url),
            content_length=self.content_lengths[filename],
        )
        response.headers.update(self.extra_get_headers.get(filename, {}))
        return response

    def set_bundle(
        self,
        *,
        model: bytes,
        thresholds: bytes,
        model_etag: str | None,
        thresholds_etag: str | None,
    ) -> None:
        self.content.update({"model.onnx": model, "thresholds.json": thresholds})
        self.etags.update(
            {
                "model.onnx": model_etag,
                "thresholds.json": thresholds_etag,
            }
        )
        self.get_etags = dict(self.etags)
        self.content_lengths = {
            filename: str(len(content)) for filename, content in self.content.items()
        }
        self.head_etags.clear()
        self._head_counts.clear()
        self.calls.clear()


class _Candidate:
    def __init__(self, model_path: Path, thresholds_path: Path) -> None:
        self.model_path = model_path
        self.thresholds_path = thresholds_path
        self.loaded = False

    def load(self) -> None:
        model = self.model_path.read_bytes()
        thresholds = self.thresholds_path.read_bytes()
        if b"corrupt" in model or b"corrupt" in thresholds:
            raise RuntimeError("candidate rejected")
        self.loaded = True

    def predict(self, _path: str) -> object:
        if not self.loaded:
            raise AssertionError("candidate was not prewarmed")
        return SimpleNamespace(labels=frozenset({self.model_path.read_text()}))


class _CandidateFactory:
    def __init__(self) -> None:
        self.candidates: list[_Candidate] = []

    def __call__(self, model_path: Path, thresholds_path: Path) -> _Candidate:
        candidate = _Candidate(model_path, thresholds_path)
        self.candidates.append(candidate)
        return candidate


class PonyChartGenerationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.base = Path(self._temporary.name)
        self.root = self.base / "store"
        self.factory = _CandidateFactory()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _write_legacy_canonical(
        self,
        model: bytes = b"model-v1",
        thresholds: bytes = b'{"Twilight Sparkle":0.5}',
        *,
        model_etag: str | None = '"model-v1"',
        thresholds_etag: str | None = '"thresholds-v1"',
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        canonical_model = self.root / "model.onnx"
        canonical_thresholds = self.root / "thresholds.json"
        canonical_model.write_bytes(model)
        canonical_thresholds.write_bytes(thresholds)
        for path, etag in (
            (canonical_model, model_etag),
            (canonical_thresholds, thresholds_etag),
        ):
            if etag is not None:
                path.with_suffix(path.suffix + ".etag").write_text(etag)

    def _store(
        self,
        remote: _Remote | Mock,
        *,
        timeout: float = 9.5,
        transfer_timeout: float = 40.0,
        max_model_bytes: int = 1024 * 1024,
        max_thresholds_bytes: int = 1024 * 1024,
    ) -> PonyChartGenerationStore:
        return PonyChartGenerationStore(
            root=self.root,
            base_url="https://models.invalid/ponychart",
            timeout=timeout,
            transfer_timeout=transfer_timeout,
            max_model_bytes=max_model_bytes,
            max_thresholds_bytes=max_thresholds_bytes,
            candidate_factory=self.factory,
            urlopen=remote,
        )

    def _initial_store(
        self,
    ) -> tuple[_Remote, PonyChartGenerationStore, LoadedPonyChartGeneration]:
        remote = _Remote(
            model=b"model-v1",
            thresholds=b'{"Twilight Sparkle":0.5}',
            model_etag='"model-v1"',
            thresholds_etag='"thresholds-v1"',
        )
        store = self._store(remote)
        loaded = store.load_or_bootstrap()
        remote.set_bundle(
            model=remote.content["model.onnx"],
            thresholds=remote.content["thresholds.json"],
            model_etag=remote.etags["model.onnx"],
            thresholds_etag=remote.etags["thresholds.json"],
        )
        return remote, store, loaded

    def _pointer(self) -> dict[str, object]:
        return json.loads((self.root / "current.json").read_text())

    def test_startup_ignores_legacy_canonical_pair_and_fetches_full_bundle(
        self,
    ) -> None:
        self._write_legacy_canonical(
            model=b"legacy-model",
            thresholds=b"legacy-thresholds",
            model_etag='"remote-model"',
            thresholds_etag='"remote-thresholds"',
        )
        remote = _Remote(
            model=b"remote-model",
            thresholds=b"{}",
            model_etag='"remote-model"',
            thresholds_etag='"remote-thresholds"',
        )
        store = self._store(remote)

        loaded = store.load_or_bootstrap()
        pointer = self._pointer()

        self.assertEqual(pointer["generation"], loaded.generation)
        generation = self.root / "generations" / loaded.generation
        self.assertEqual((generation / "model.onnx").read_bytes(), b"remote-model")
        self.assertTrue((generation / "manifest.json").is_file())
        self.assertEqual(
            self.factory.candidates[-1].model_path,
            generation / "model.onnx",
        )
        self.assertEqual(
            [method for method, _, _ in remote.calls],
            ["HEAD", "HEAD", "GET", "GET", "HEAD", "HEAD"],
        )

        second_factory = _CandidateFactory()
        no_network = Mock(side_effect=AssertionError("pointer load must be local"))
        second_store = PonyChartGenerationStore(
            root=self.root,
            candidate_factory=second_factory,
            urlopen=no_network,
        )
        reloaded = second_store.load_or_bootstrap()
        self.assertEqual(reloaded.generation, loaded.generation)
        self.assertEqual(
            second_factory.candidates[-1].model_path,
            generation / "model.onnx",
        )
        no_network.assert_not_called()

    def test_head_etags_without_get_etags_remain_non_authoritative(self) -> None:
        remote = _Remote(
            model=b"model",
            thresholds=b"{}",
            model_etag='"head-model"',
            thresholds_etag='"head-thresholds"',
        )
        remote.get_etags = {"model.onnx": None, "thresholds.json": None}
        store = self._store(remote)

        loaded = store.load_or_bootstrap()

        self.assertEqual(
            self._pointer()["remote_etags"],
            {"model.onnx": None, "thresholds.json": None},
        )
        remote.calls.clear()
        remote._head_counts.clear()
        result = store.refresh(loaded.generation)
        self.assertEqual(result.outcome, PonyChartRefreshOutcome.CURRENT)
        self.assertEqual(
            [method for method, _, _ in remote.calls],
            ["HEAD", "HEAD", "GET", "GET", "HEAD", "HEAD"],
        )

    def test_blank_get_etags_are_not_persisted_and_pointer_reopens(self) -> None:
        remote = _Remote(
            model=b"model",
            thresholds=b"{}",
            model_etag='"head-model"',
            thresholds_etag='"head-thresholds"',
        )
        remote.get_etags = {"model.onnx": "   ", "thresholds.json": ""}
        loaded = self._store(remote).load_or_bootstrap()

        self.assertEqual(
            self._pointer()["remote_etags"],
            {"model.onnx": None, "thresholds.json": None},
        )
        no_network = Mock(side_effect=AssertionError("reopen must use pointer"))
        reopened = self._store(no_network).load_or_bootstrap()
        self.assertEqual(reopened.generation, loaded.generation)
        no_network.assert_not_called()

    def test_missing_etags_still_downloads_and_commits_verified_pair(self) -> None:
        remote = _Remote(
            model=b"downloaded-model",
            thresholds=b"{}",
            model_etag=None,
            thresholds_etag=None,
        )
        loaded = self._store(remote).load_or_bootstrap()

        self.assertEqual(self._pointer()["generation"], loaded.generation)
        self.assertEqual(
            [method for method, _, _ in remote.calls],
            ["HEAD", "HEAD", "GET", "GET", "HEAD", "HEAD"],
        )
        self.assertEqual(
            self._pointer()["remote_etags"],
            {"model.onnx": None, "thresholds.json": None},
        )

    def test_current_etag_pair_does_not_download(self) -> None:
        remote, store, initial = self._initial_store()

        result = store.refresh(initial.generation)

        self.assertEqual(result.outcome, PonyChartRefreshOutcome.CURRENT)
        self.assertIsNone(result.loaded)
        self.assertEqual([call[0] for call in remote.calls], ["HEAD", "HEAD"])

    def test_current_fast_path_rejects_corrupt_target_without_mutation(self) -> None:
        remote, store, initial = self._initial_store()
        pointer_before = (self.root / "current.json").read_bytes()
        generation = self.root / "generations" / initial.generation
        model_path = generation / "model.onnx"
        model_path.write_bytes(b"tampered")

        with self.assertRaises(PonyChartArtifactError):
            store.refresh(initial.generation)

        self.assertEqual((self.root / "current.json").read_bytes(), pointer_before)
        self.assertEqual(model_path.read_bytes(), b"tampered")
        self.assertTrue(generation.is_dir())
        self.assertEqual(remote.calls, [])

    def test_changed_stable_pair_commits_new_generation(self) -> None:
        remote, store, initial = self._initial_store()
        remote.set_bundle(
            model=b"model-v2",
            thresholds=b'{"Rarity":0.7}',
            model_etag='"model-v2"',
            thresholds_etag='"thresholds-v2"',
        )

        result = store.refresh(initial.generation)

        self.assertEqual(result.outcome, PonyChartRefreshOutcome.UPDATED)
        self.assertIsNotNone(result.loaded)
        assert result.loaded is not None
        self.assertNotEqual(result.loaded.generation, initial.generation)
        self.assertEqual(self._pointer()["generation"], result.loaded.generation)
        self.assertTrue((self.root / "generations" / initial.generation).is_dir())

    def test_new_etags_with_identical_bytes_update_pointer_only_once(self) -> None:
        remote, store, initial = self._initial_store()
        remote.set_bundle(
            model=b"model-v1",
            thresholds=b'{"Twilight Sparkle":0.5}',
            model_etag='"model-repacked"',
            thresholds_etag='"thresholds-repacked"',
        )

        result = store.refresh(initial.generation)
        first_call_count = len(remote.calls)
        second = store.refresh(initial.generation)

        self.assertEqual(result.outcome, PonyChartRefreshOutcome.CURRENT)
        self.assertEqual(second.outcome, PonyChartRefreshOutcome.CURRENT)
        self.assertEqual(self._pointer()["generation"], initial.generation)
        remote_etags = self._pointer()["remote_etags"]
        self.assertEqual(remote_etags["model.onnx"], '"model-repacked"')  # type: ignore[index]
        self.assertEqual(len(remote.calls) - first_call_count, 2)

    def test_partial_download_failure_keeps_pointer_and_old_generation(self) -> None:
        remote, store, initial = self._initial_store()
        remote.set_bundle(
            model=b"model-v2",
            thresholds=b'{"Rarity":0.7}',
            model_etag='"model-v2"',
            thresholds_etag='"thresholds-v2"',
        )
        old_pointer = (self.root / "current.json").read_bytes()
        remote.fail_get = "thresholds.json"

        with self.assertRaises(PonyChartArtifactError):
            store.refresh(initial.generation)

        self.assertEqual((self.root / "current.json").read_bytes(), old_pointer)
        self.assertTrue((self.root / "generations" / initial.generation).is_dir())
        self.assertFalse(
            any(
                path.name.startswith(".staging-")
                for path in (self.root / "generations").iterdir()
            )
        )

    def test_corrupt_candidate_keeps_current_pointer(self) -> None:
        remote, store, initial = self._initial_store()
        remote.set_bundle(
            model=b"corrupt-model",
            thresholds=b"{}",
            model_etag='"bad-model"',
            thresholds_etag='"bad-thresholds"',
        )
        old_pointer = (self.root / "current.json").read_bytes()

        with self.assertRaisesRegex(RuntimeError, "candidate rejected"):
            store.refresh(initial.generation)

        self.assertEqual((self.root / "current.json").read_bytes(), old_pointer)

    def test_unstable_etag_pair_is_rejected_before_commit(self) -> None:
        remote, store, initial = self._initial_store()
        remote.set_bundle(
            model=b"model-v2",
            thresholds=b"{}",
            model_etag='"model-v2"',
            thresholds_etag='"thresholds-v2"',
        )
        remote.head_etags["model.onnx"] = ['"model-v2"', '"model-v3"']
        old_pointer = (self.root / "current.json").read_bytes()

        with self.assertRaisesRegex(PonyChartArtifactError, "changed"):
            store.refresh(initial.generation)

        self.assertEqual((self.root / "current.json").read_bytes(), old_pointer)

    def test_pointer_replace_failure_never_exposes_new_generation(self) -> None:
        remote, store, initial = self._initial_store()
        remote.set_bundle(
            model=b"model-v2",
            thresholds=b"{}",
            model_etag='"model-v2"',
            thresholds_etag='"thresholds-v2"',
        )
        old_pointer = (self.root / "current.json").read_bytes()

        with (
            patch(
                "hvbattle.ponychart_model_store.os.replace",
                side_effect=OSError("simulated crash before pointer commit"),
            ),
            self.assertRaisesRegex(OSError, "simulated crash"),
        ):
            store.refresh(initial.generation)

        self.assertEqual((self.root / "current.json").read_bytes(), old_pointer)
        self.assertEqual(self._pointer()["generation"], initial.generation)
        self.assertTrue((self.root / "generations" / initial.generation).is_dir())

    def test_all_requests_use_verified_tls_and_finite_timeout(self) -> None:
        remote = _Remote(
            model=b"model",
            thresholds=b"{}",
            model_etag='"model"',
            thresholds_etag='"thresholds"',
        )
        self._store(remote, timeout=7.25).load_or_bootstrap()

        self.assertGreater(len(remote.calls), 0)
        for _, _, kwargs in remote.calls:
            context = kwargs["context"]
            self.assertIsInstance(context, ssl.SSLContext)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(kwargs["timeout"], 7.25)

    def test_https_redirect_downgrade_is_rejected_for_head_and_get(self) -> None:
        for method in ("HEAD", "GET"):
            with self.subTest(method=method):
                remote = _Remote(
                    model=b"model",
                    thresholds=b"{}",
                    model_etag='"model"',
                    thresholds_etag='"thresholds"',
                )
                final_urls = (
                    remote.head_final_urls
                    if method == "HEAD"
                    else remote.get_final_urls
                )
                final_urls["model.onnx"] = "http://models.invalid/model.onnx"
                isolated_root = self.base / f"redirect-{method.lower()}"
                store = PonyChartGenerationStore(
                    root=isolated_root,
                    base_url="https://models.invalid/ponychart",
                    candidate_factory=self.factory,
                    urlopen=remote,
                )

                with self.assertRaisesRegex(PonyChartArtifactError, "left HTTPS"):
                    store.load_or_bootstrap()

                self.assertFalse((isolated_root / "current.json").exists())

    def test_truncated_content_length_never_commits_pointer(self) -> None:
        remote = _Remote(
            model=b"short",
            thresholds=b"{}",
            model_etag='"model"',
            thresholds_etag='"thresholds"',
        )
        remote.content_lengths["model.onnx"] = "99"

        with self.assertRaisesRegex(PonyChartArtifactError, "length mismatch"):
            self._store(remote).load_or_bootstrap()

        self.assertFalse((self.root / "current.json").exists())

    def test_missing_length_or_chunked_body_is_rejected(self) -> None:
        for framing in ("missing", "chunked"):
            with self.subTest(framing=framing):
                remote = _Remote(
                    model=b"model",
                    thresholds=b"{}",
                    model_etag='"model"',
                    thresholds_etag='"thresholds"',
                )
                isolated_root = self.base / f"framing-{framing}"
                if framing == "missing":
                    remote.content_lengths["model.onnx"] = None
                else:
                    remote.extra_get_headers["model.onnx"] = {
                        "Transfer-Encoding": "chunked"
                    }
                store = PonyChartGenerationStore(
                    root=isolated_root,
                    base_url="https://models.invalid/ponychart",
                    candidate_factory=self.factory,
                    urlopen=remote,
                )

                with self.assertRaises(PonyChartArtifactError):
                    store.load_or_bootstrap()

                self.assertFalse((isolated_root / "current.json").exists())

    def test_artifact_size_limit_never_commits_pointer(self) -> None:
        remote = _Remote(
            model=b"too-large",
            thresholds=b"{}",
            model_etag='"model"',
            thresholds_etag='"thresholds"',
        )

        with self.assertRaisesRegex(PonyChartArtifactError, "size limit"):
            self._store(remote, max_model_bytes=3).load_or_bootstrap()

        self.assertFalse((self.root / "current.json").exists())

    def test_bundle_transfer_deadline_never_commits_pointer(self) -> None:
        remote = _Remote(
            model=b"model",
            thresholds=b"{}",
            model_etag='"model"',
            thresholds_etag='"thresholds"',
        )
        store = self._store(remote, transfer_timeout=1.0)

        with (
            patch.object(
                store_module.time,
                "monotonic",
                side_effect=(0.0, 0.0, 2.0),
            ),
            self.assertRaises(PonyChartArtifactError),
        ):
            store.load_or_bootstrap()

        self.assertFalse((self.root / "current.json").exists())

    def test_directory_fsync_ignores_only_unsupported_platform_errors(self) -> None:
        with patch.object(
            store_module.os,
            "open",
            side_effect=OSError(errno.EINVAL, "unsupported"),
        ):
            store_module._fsync_directory(self.root)

        with (
            patch.object(
                store_module.os,
                "open",
                side_effect=OSError(errno.EPERM, "denied"),
            ),
            self.assertRaises(PermissionError),
        ):
            store_module._fsync_directory(self.root)

    def test_windows_file_lock_retries_until_acquired(self) -> None:
        lock_path = self.base / "windows-lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        calls = 0

        def locking(_descriptor: int, _mode: int, _length: int) -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise OSError(errno.EACCES, "busy")

        fake_msvcrt = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking)
        try:
            with (
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                patch.object(store_module.time, "sleep") as sleep,
            ):
                store_module._acquire_windows_file_lock(descriptor)
        finally:
            os.close(descriptor)

        self.assertEqual(calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_refresh_adopts_peer_pointer_without_network(self) -> None:
        peer_remote, first_store, initial = self._initial_store()
        peer_remote.set_bundle(
            model=b"model-v2",
            thresholds=b"{}",
            model_etag='"model-v2"',
            thresholds_etag='"thresholds-v2"',
        )
        peer = first_store.refresh(initial.generation)
        assert peer.loaded is not None

        no_network = Mock(side_effect=AssertionError("peer adoption must be local"))
        adopter = self._store(no_network)
        adopted = adopter.refresh(initial.generation)

        self.assertEqual(adopted.outcome, PonyChartRefreshOutcome.UPDATED)
        self.assertEqual(adopted.loaded.generation, peer.loaded.generation)  # type: ignore[union-attr]
        no_network.assert_not_called()

    def test_process_lock_prevents_late_writer_pointer_regression(self) -> None:
        first_remote, first_store, initial = self._initial_store()
        first_remote.set_bundle(
            model=b"model-v2",
            thresholds=b"{}",
            model_etag='"model-v2"',
            thresholds_etag='"thresholds-v2"',
        )
        reached_commit = threading.Event()
        release_commit = threading.Event()
        original_commit = first_store._commit_pointer

        def blocked_commit(pointer: object) -> None:
            reached_commit.set()
            if not release_commit.wait(timeout=2):
                raise TimeoutError("test did not release first pointer commit")
            original_commit(pointer)  # type: ignore[arg-type]

        first_store._commit_pointer = blocked_commit  # type: ignore[method-assign]

        no_network = Mock(side_effect=AssertionError("second refresh redownloaded"))
        second_store = self._store(no_network)
        attempted_second_lock = threading.Event()
        original_process_lock = second_store._process_lock

        @contextmanager
        def observed_process_lock() -> Iterator[None]:
            attempted_second_lock.set()
            with original_process_lock():
                yield

        second_store._process_lock = observed_process_lock  # type: ignore[method-assign]

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first_store.refresh, initial.generation)
            self.assertTrue(reached_commit.wait(timeout=1))
            second_future = pool.submit(second_store.refresh, initial.generation)
            self.assertTrue(attempted_second_lock.wait(timeout=1))
            self.assertFalse(second_future.done())
            release_commit.set()
            first = first_future.result(timeout=2)
            second = second_future.result(timeout=2)

        self.assertEqual(first.outcome, PonyChartRefreshOutcome.UPDATED)
        self.assertEqual(second.outcome, PonyChartRefreshOutcome.UPDATED)
        self.assertEqual(second.loaded.generation, first.loaded.generation)  # type: ignore[union-attr]
        self.assertEqual(self._pointer()["generation"], first.loaded.generation)  # type: ignore[union-attr]
        no_network.assert_not_called()

    def test_lock_inode_serializes_a_separate_process(self) -> None:
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe()
        process = context.Process(
            target=_hold_store_process_lock,
            args=(str(self.root), child_connection),
        )
        process.start()
        child_connection.close()
        attempted = threading.Event()
        entered = threading.Event()
        store = self._store(Mock())

        def acquire_in_parent() -> None:
            attempted.set()
            with store._process_lock():
                entered.set()

        try:
            self.assertTrue(parent_connection.poll(5))
            self.assertEqual(parent_connection.recv(), "locked")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(acquire_in_parent)
                self.assertTrue(attempted.wait(timeout=1))
                self.assertFalse(entered.wait(timeout=0.1))
                parent_connection.send("release")
                future.result(timeout=5)
            self.assertTrue(entered.is_set())
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        finally:
            parent_connection.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    def test_concurrent_same_generation_rename_adopts_valid_winner(self) -> None:
        remote = _Remote(
            model=b"model",
            thresholds=b"{}",
            model_etag='"model"',
            thresholds_etag='"thresholds"',
        )
        store = self._store(remote)
        remote_before = store._remote_metadata()
        prepared, _ = store._prepare_download_stage(remote_before)
        destination = self.root / "generations" / prepared.generation
        original_rename = Path.rename

        def lose_race(path: Path, target: Path) -> Path:
            if path == prepared.path:
                shutil.copytree(path, target)
                raise OSError(errno.ENOTEMPTY, "peer won")
            return original_rename(path, target)

        with patch.object(Path, "rename", new=lose_race):
            loaded = store._install_prepared(prepared)

        self.assertEqual(loaded.generation, prepared.generation)
        self.assertTrue(destination.is_dir())
        self.assertFalse(prepared.path.exists())


class PonyChartPublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_predictor = ponychart_module._predict
        self.original_generation = ponychart_module._generation_id
        self.original_store = ponychart_module._model_store

    def tearDown(self) -> None:
        with ponychart_module._publication_lock:
            ponychart_module._predict = self.original_predictor
            ponychart_module._generation_id = self.original_generation
        ponychart_module._model_store = self.original_store

    def test_preload_publishes_generation_once(self) -> None:
        predictor = Mock()
        store = Mock()
        store.load_or_bootstrap.return_value = LoadedPonyChartGeneration(
            "a" * 64,
            predictor,
        )
        with ponychart_module._publication_lock:
            ponychart_module._predict = None
            ponychart_module._generation_id = None
        ponychart_module._model_store = store

        ponychart_module.preload_ponychart_classifier()
        ponychart_module.preload_ponychart_classifier()

        store.load_or_bootstrap.assert_called_once_with()
        self.assertEqual(
            ponychart_module._published_snapshot(),
            (predictor, "a" * 64),
        )

    def test_refresh_failure_keeps_predictor_generation_pair(self) -> None:
        old_predictor = Mock()
        ponychart_module._publish(old_predictor, "a" * 64)
        store = Mock()
        store.refresh.side_effect = PonyChartArtifactError("unreachable")
        ponychart_module._model_store = store

        with self.assertRaises(PonyChartArtifactError):
            ponychart_module.refresh_ponychart_classifier()

        self.assertEqual(
            ponychart_module._published_snapshot(),
            (old_predictor, "a" * 64),
        )

    async def test_inflight_prediction_keeps_old_atomic_snapshot(self) -> None:
        old_started = threading.Event()
        release_old = threading.Event()

        def predict_old(_path: str) -> object:
            old_started.set()
            if not release_old.wait(timeout=2):
                raise TimeoutError("old prediction was not released")
            return SimpleNamespace(labels=frozenset({"Old Snapshot"}))

        predict_new = Mock(
            return_value=SimpleNamespace(labels=frozenset({"New Snapshot"}))
        )
        ponychart_module._publish(predict_old, "a" * 64)
        store = Mock()
        store.refresh.return_value = PonyChartStoreRefresh(
            PonyChartRefreshOutcome.UPDATED,
            LoadedPonyChartGeneration("b" * 64, predict_new),
        )
        ponychart_module._model_store = store

        old_label = Mock(text="Old Snapshot")
        old_label.click = AsyncMock()
        new_label = Mock(text="New Snapshot")
        new_label.click = AsyncMock()
        driver = Mock()
        driver.page = Mock()
        driver.page.select_all = AsyncMock(return_value=[old_label, new_label])
        challenge = PonyChart(driver)

        old_answer = asyncio.create_task(challenge._auto_answer("old.png"))
        self.assertTrue(await asyncio.to_thread(old_started.wait, 1))
        outcome = await asyncio.to_thread(ponychart_module.refresh_ponychart_classifier)
        release_old.set()

        self.assertEqual(outcome, PonyChartRefreshOutcome.UPDATED)
        self.assertEqual(await old_answer, frozenset({"Old Snapshot"}))
        self.assertEqual(
            await challenge._auto_answer("new.png"),
            frozenset({"New Snapshot"}),
        )
        old_label.click.assert_awaited_once_with()
        new_label.click.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()

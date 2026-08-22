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


def _missing_ski_error(
    *,
    verify_code: int = 86,
    verify_message: str = "Missing Subject Key Identifier",
) -> URLError:
    reason = ssl.SSLCertVerificationError(
        1,
        f"certificate verify failed: {verify_message}",
    )
    reason.verify_code = verify_code
    reason.verify_message = verify_message
    return URLError(reason)


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
        self.read_idle_timeouts: list[float] = []
        self.fp = SimpleNamespace(
            raw=SimpleNamespace(
                _sock=SimpleNamespace(settimeout=self.read_idle_timeouts.append)
            )
        )
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
        self.responses: list[_Response] = []
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
            response = _Response(
                etag=etag,
                final_url=self.head_final_urls.get(filename, requested_url),
            )
            self.responses.append(response)
            return response
        if filename == self.fail_get:
            raise URLError("offline failure")
        response = _Response(
            self.content[filename],
            etag=self.get_etags[filename],
            final_url=self.get_final_urls.get(filename, requested_url),
            content_length=self.content_lengths[filename],
        )
        response.headers.update(self.extra_get_headers.get(filename, {}))
        self.responses.append(response)
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
        self.responses.clear()


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

    def predict_bytes(self, _image: bytes) -> object:
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
        base_url: str = "https://models.invalid/ponychart",
        connect_timeout: float = 4.5,
        read_idle_timeout: float = 4.5,
        refresh_timeout: float = 40.0,
        max_model_bytes: int = 1024 * 1024,
        max_thresholds_bytes: int = 1024 * 1024,
    ) -> PonyChartGenerationStore:
        return PonyChartGenerationStore(
            root=self.root,
            base_url=base_url,
            connect_timeout=connect_timeout,
            read_idle_timeout=read_idle_timeout,
            refresh_timeout=refresh_timeout,
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
        self.assertEqual(loaded.model_path, generation / "model.onnx")
        self.assertEqual(loaded.thresholds_path, generation / "thresholds.json")
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
        self.assertEqual(reloaded.model_path, generation / "model.onnx")
        self.assertEqual(reloaded.thresholds_path, generation / "thresholds.json")
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

    def test_requests_split_connect_idle_and_refresh_timeouts(self) -> None:
        remote = _Remote(
            model=b"model",
            thresholds=b"{}",
            model_etag='"model"',
            thresholds_etag='"thresholds"',
        )
        self._store(
            remote,
            connect_timeout=4.25,
            read_idle_timeout=4.75,
        ).load_or_bootstrap()

        self.assertGreater(len(remote.calls), 0)
        for _, _, kwargs in remote.calls:
            context = kwargs["context"]
            self.assertIsInstance(context, ssl.SSLContext)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.verify_flags & ssl.VERIFY_X509_STRICT)
            self.assertLessEqual(kwargs["timeout"], 4.25)
        get_responses = [
            response
            for (method, _, _), response in zip(remote.calls, remote.responses)
            if method == "GET"
        ]
        self.assertGreater(len(get_responses), 0)
        self.assertTrue(
            all(
                response.read_idle_timeouts and max(response.read_idle_timeouts) <= 4.75
                for response in get_responses
            )
        )

    def test_connect_timeout_cannot_exceed_protocol_sized_watchdog(self) -> None:
        for invalid in (5.01, True):
            with self.subTest(timeout=invalid):
                with self.assertRaisesRegex(ValueError, "connect_timeout"):
                    self._store(Mock(), connect_timeout=invalid)

    def test_read_idle_timeout_cannot_exceed_protocol_sized_watchdog(self) -> None:
        for invalid in (5.01, True):
            with self.subTest(timeout=invalid):
                with self.assertRaisesRegex(ValueError, "read_idle_timeout"):
                    self._store(Mock(), read_idle_timeout=invalid)

    def test_refresh_timeout_cannot_exceed_refresh_lifecycle_budget(self) -> None:
        for invalid in (120.01, True):
            with self.subTest(timeout=invalid):
                with self.assertRaisesRegex(ValueError, "refresh_timeout"):
                    self._store(Mock(), refresh_timeout=invalid)

    def test_metadata_pair_shares_one_five_second_snapshot_deadline(self) -> None:
        base_url = "https://models.invalid/ponychart"
        clock = [0.0]
        timeouts: list[float] = []

        def urlopen(request: object, **kwargs: object) -> _Response:
            timeouts.append(float(kwargs["timeout"]))
            if len(timeouts) == 1:
                clock[0] = 4.25
            requested_url = request.full_url  # type: ignore[attr-defined]
            return _Response(etag='"etag"', final_url=requested_url)

        store = self._store(Mock(), base_url=base_url)
        store._urlopen = urlopen
        with patch.object(store_module.time, "monotonic", side_effect=lambda: clock[0]):
            metadata = store._remote_metadata(deadline=120.0)

        self.assertTrue(metadata.is_complete)
        self.assertEqual(timeouts, [4.5, 0.75])

    def test_metadata_does_not_start_second_head_after_snapshot_expiry(self) -> None:
        clock = [0.0]
        calls = 0

        def urlopen(request: object, **_kwargs: object) -> _Response:
            nonlocal calls
            calls += 1
            clock[0] = 5.0
            requested_url = request.full_url  # type: ignore[attr-defined]
            return _Response(etag='"model"', final_url=requested_url)

        store = self._store(Mock())
        store._urlopen = urlopen
        with patch.object(store_module.time, "monotonic", side_effect=lambda: clock[0]):
            metadata = store._remote_metadata(deadline=120.0)

        self.assertEqual(calls, 1)
        self.assertEqual(metadata.model_etag, '"model"')
        self.assertIsNone(metadata.thresholds_etag)

    def test_tls_context_adds_certifi_to_system_roots(self) -> None:
        context = Mock(
            check_hostname=True,
            verify_mode=ssl.CERT_REQUIRED,
            verify_flags=ssl.VERIFY_X509_PARTIAL_CHAIN,
        )

        with (
            patch.object(
                store_module.ssl,
                "create_default_context",
                return_value=context,
            ) as create_default_context,
            patch("certifi.where", return_value="/certifi/roots.pem") as certifi_where,
        ):
            result = store_module._verified_context(strict=True)

        self.assertIs(result, context)
        create_default_context.assert_called_once_with()
        certifi_where.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(
            cafile="/certifi/roots.pem"
        )
        self.assertTrue(context.verify_flags & ssl.VERIFY_X509_STRICT)

    def test_compatibility_context_only_removes_strict_verification(self) -> None:
        store = self._store(Mock())
        strict = store._strict_ssl_context
        compatibility = store._compatibility_ssl_context

        self.assertTrue(strict.check_hostname)
        self.assertTrue(compatibility.check_hostname)
        self.assertEqual(strict.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(compatibility.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(strict.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertFalse(compatibility.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertEqual(
            strict.verify_flags & ~ssl.VERIFY_X509_STRICT,
            compatibility.verify_flags,
        )

    def test_head_retries_only_exact_missing_ski_then_remembers_compatibility(
        self,
    ) -> None:
        base_url = "https://www.csie.ntu.edu.tw/~d06922002/ponychart_classifier"
        model_url = f"{base_url}/model.onnx"
        thresholds_url = f"{base_url}/thresholds.json"
        urlopen = Mock(
            side_effect=(
                _missing_ski_error(),
                _Response(etag='"model"', final_url=model_url),
                _Response(etag='"thresholds"', final_url=thresholds_url),
            )
        )
        store = self._store(urlopen, base_url=base_url, connect_timeout=4.5)

        self.assertEqual(store._remote_etag("model.onnx"), '"model"')
        self.assertEqual(store._remote_etag("thresholds.json"), '"thresholds"')

        self.assertEqual(urlopen.call_count, 3)
        strict_call, compatibility_retry, next_request = urlopen.call_args_list
        self.assertIs(
            strict_call.kwargs["context"],
            store._strict_ssl_context,
        )
        self.assertIs(
            compatibility_retry.kwargs["context"],
            store._compatibility_ssl_context,
        )
        self.assertIs(
            next_request.kwargs["context"],
            store._compatibility_ssl_context,
        )
        self.assertEqual(strict_call.kwargs["timeout"], 4.5)
        self.assertEqual(compatibility_retry.kwargs["timeout"], 4.5)
        self.assertEqual(next_request.kwargs["timeout"], 4.5)

    def test_missing_ski_fallback_uses_only_snapshot_remaining(self) -> None:
        base_url = "https://www.csie.ntu.edu.tw/~d06922002/ponychart_classifier"
        requested_url = f"{base_url}/model.onnx"
        clock = [0.0]
        timeouts: list[float] = []

        def urlopen(_request: object, **kwargs: object) -> _Response:
            timeouts.append(float(kwargs["timeout"]))
            if len(timeouts) == 1:
                clock[0] = 4.25
                raise _missing_ski_error()
            return _Response(etag='"model"', final_url=requested_url)

        store = self._store(Mock(), base_url=base_url)
        store._urlopen = urlopen
        with patch.object(store_module.time, "monotonic", side_effect=lambda: clock[0]):
            etag = store._remote_etag("model.onnx", deadline=120.0)

        self.assertEqual(etag, '"model"')
        self.assertEqual(timeouts, [4.5, 0.75])

    def test_missing_ski_fallback_is_not_started_after_snapshot_expiry(self) -> None:
        base_url = "https://www.csie.ntu.edu.tw/~d06922002/ponychart_classifier"
        clock = [0.0]
        calls = 0

        def urlopen(_request: object, **_kwargs: object) -> _Response:
            nonlocal calls
            calls += 1
            clock[0] = 5.0
            raise _missing_ski_error()

        store = self._store(Mock(), base_url=base_url)
        store._urlopen = urlopen
        with patch.object(store_module.time, "monotonic", side_effect=lambda: clock[0]):
            etag = store._remote_etag("model.onnx", deadline=120.0)

        self.assertIsNone(etag)
        self.assertEqual(calls, 1)

    def test_head_does_not_retry_near_match_certificate_errors(self) -> None:
        base_url = "https://www.csie.ntu.edu.tw/~d06922002/ponychart_classifier"
        errors = (
            _missing_ski_error(verify_code=85),
            _missing_ski_error(verify_message="Missing Subject Key Identifier "),
            URLError("unrelated TLS failure"),
        )
        for index, error in enumerate(errors):
            with self.subTest(index=index):
                urlopen = Mock(side_effect=error)
                store = self._store(urlopen, base_url=base_url)

                self.assertIsNone(store._remote_etag("model.onnx"))

                urlopen.assert_called_once()
                self.assertIs(
                    urlopen.call_args.kwargs["context"],
                    store._strict_ssl_context,
                )

    def test_custom_origin_never_uses_missing_ski_compatibility(self) -> None:
        urlopen = Mock(side_effect=_missing_ski_error())
        store = self._store(urlopen)

        self.assertIsNone(store._remote_etag("model.onnx"))
        with self.assertRaisesRegex(PonyChartArtifactError, "URLError"):
            store._download(
                "model.onnx",
                self.base / "custom-origin-model.onnx",
                deadline=store_module.time.monotonic() + 10,
            )

        self.assertEqual(urlopen.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["context"] is store._strict_ssl_context
                for call in urlopen.call_args_list
            )
        )
        self.assertIs(store._active_ssl_context, store._strict_ssl_context)

    def test_head_transport_failures_are_advisory(self) -> None:
        for error in (
            URLError("offline"),
            TimeoutError("timed out"),
            OSError("socket failed"),
        ):
            with self.subTest(error_type=type(error).__name__):
                urlopen = Mock(side_effect=error)
                store = self._store(urlopen)

                self.assertIsNone(store._remote_etag("model.onnx"))

                urlopen.assert_called_once()

    def test_get_retries_exact_missing_ski_and_stays_fail_closed(self) -> None:
        base_url = "https://www.csie.ntu.edu.tw/~d06922002/ponychart_classifier"
        requested_url = f"{base_url}/model.onnx"
        response = _Response(
            b"model",
            etag='"model"',
            final_url=requested_url,
            content_length="5",
        )
        urlopen = Mock(side_effect=(_missing_ski_error(), response))
        store = self._store(urlopen, base_url=base_url)
        destination = self.base / "downloaded-model.onnx"

        etag = store._download(
            "model.onnx",
            destination,
            deadline=store_module.time.monotonic() + 10,
        )

        self.assertEqual(etag, '"model"')
        self.assertEqual(destination.read_bytes(), b"model")
        self.assertEqual(urlopen.call_count, 2)
        self.assertIs(
            urlopen.call_args_list[0].kwargs["context"],
            store._strict_ssl_context,
        )
        self.assertIs(
            urlopen.call_args_list[1].kwargs["context"],
            store._compatibility_ssl_context,
        )

        failing_urlopen = Mock(
            side_effect=(_missing_ski_error(), URLError("fallback failed"))
        )
        failing_store = self._store(failing_urlopen, base_url=base_url)
        with self.assertRaisesRegex(PonyChartArtifactError, "URLError"):
            failing_store._download(
                "model.onnx",
                self.base / "failed-model.onnx",
                deadline=store_module.time.monotonic() + 10,
            )
        self.assertEqual(failing_urlopen.call_count, 2)

    def test_get_does_not_retry_near_match_certificate_errors(self) -> None:
        base_url = "https://www.csie.ntu.edu.tw/~d06922002/ponychart_classifier"
        errors = (
            _missing_ski_error(verify_code=85),
            _missing_ski_error(verify_message="Missing Subject Key Identifier "),
        )
        for index, error in enumerate(errors):
            with self.subTest(index=index):
                urlopen = Mock(side_effect=error)
                store = self._store(urlopen, base_url=base_url)

                with self.assertRaisesRegex(PonyChartArtifactError, "URLError"):
                    store._download(
                        "model.onnx",
                        self.base / f"near-match-{index}.onnx",
                        deadline=store_module.time.monotonic() + 10,
                    )

                urlopen.assert_called_once()
                self.assertIs(
                    urlopen.call_args.kwargs["context"],
                    store._strict_ssl_context,
                )

    def test_get_unrelated_transport_error_does_not_retry(self) -> None:
        urlopen = Mock(side_effect=URLError("offline"))
        store = self._store(urlopen)

        with self.assertRaisesRegex(PonyChartArtifactError, "URLError"):
            store._download(
                "model.onnx",
                self.base / "failed-model.onnx",
                deadline=store_module.time.monotonic() + 10,
            )

        urlopen.assert_called_once()
        self.assertIs(
            urlopen.call_args.kwargs["context"],
            store._strict_ssl_context,
        )

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
        store = self._store(remote, refresh_timeout=1.0)
        clock = [0.0]

        def expiring_remote(request: object, **kwargs: object) -> _Response:
            response = remote(request, **kwargs)
            if request.get_method() == "GET":  # type: ignore[attr-defined]
                clock[0] = 2.0
            return response

        store._urlopen = expiring_remote

        with (
            patch.object(store_module.time, "monotonic", side_effect=lambda: clock[0]),
            self.assertRaises(PonyChartArtifactError),
        ):
            store.load_or_bootstrap()

        self.assertFalse((self.root / "current.json").exists())
        self.assertEqual([call[0] for call in remote.calls], ["HEAD", "HEAD", "GET"])

    def test_candidate_overrun_is_detected_before_pointer_commit(self) -> None:
        remote = _Remote(
            model=b"model",
            thresholds=b"{}",
            model_etag='"model"',
            thresholds_etag='"thresholds"',
        )
        store = self._store(remote, refresh_timeout=1.0)
        clock = [0.0]
        original_factory = store._candidate_factory

        def expiring_factory(model_path: Path, thresholds_path: Path) -> _Candidate:
            candidate = original_factory(model_path, thresholds_path)
            original_load = candidate.load

            def expiring_load() -> None:
                original_load()
                clock[0] = 2.0

            candidate.load = expiring_load  # type: ignore[method-assign]
            return candidate

        store._candidate_factory = expiring_factory
        with (
            patch.object(store_module.time, "monotonic", side_effect=lambda: clock[0]),
            self.assertRaises(TimeoutError),
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
                store_module._acquire_windows_file_lock(
                    descriptor,
                    deadline=store_module.time.monotonic() + 1.0,
                )
        finally:
            os.close(descriptor)

        self.assertEqual(calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_process_lock_deadline_bounds_the_critical_section(self) -> None:
        store = self._store(Mock())
        clock = [0.0]

        with (
            patch.object(store_module.time, "monotonic", side_effect=lambda: clock[0]),
            self.assertRaisesRegex(TimeoutError, "while held"),
        ):
            with store._process_lock() as lock_deadline:
                self.assertEqual(lock_deadline, 5.0)
                clock[0] = 6.0

        with store._process_lock(
            deadline=store_module.time.monotonic() + 1.0
        ) as lock_deadline:
            self.assertGreater(lock_deadline, store_module.time.monotonic())

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

        def blocked_commit(pointer: object, *, deadline: float) -> None:
            reached_commit.set()
            if not release_commit.wait(timeout=2):
                raise TimeoutError("test did not release first pointer commit")
            original_commit(pointer, deadline=deadline)  # type: ignore[arg-type]

        first_store._commit_pointer = blocked_commit  # type: ignore[method-assign]

        no_network = Mock(side_effect=AssertionError("second refresh redownloaded"))
        second_store = self._store(no_network)
        attempted_second_lock = threading.Event()
        original_process_lock = second_store._process_lock

        @contextmanager
        def observed_process_lock(
            *,
            deadline: float | None = None,
        ) -> Iterator[float]:
            attempted_second_lock.set()
            with original_process_lock(deadline=deadline) as lock_deadline:
                yield lock_deadline

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

    def test_process_lock_contention_honors_caller_deadline(self) -> None:
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe()
        process = context.Process(
            target=_hold_store_process_lock,
            args=(str(self.root), child_connection),
        )
        process.start()
        child_connection.close()
        store = self._store(Mock())

        try:
            self.assertTrue(parent_connection.poll(5))
            self.assertEqual(parent_connection.recv(), "locked")
            started = store_module.time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "lock deadline"):
                with store._process_lock(deadline=started + 0.2):
                    self.fail("contended process lock must not be entered")
            self.assertLess(store_module.time.monotonic() - started, 1.0)
            self.assertTrue(process.is_alive())
            parent_connection.send("release")
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        finally:
            parent_connection.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    def test_network_and_candidate_validation_run_outside_pointer_lock(self) -> None:
        remote = _Remote(
            model=b"model",
            thresholds=b"{}",
            model_etag='"model"',
            thresholds_etag='"thresholds"',
        )
        lock_depth = 0

        def guarded_remote(request: object, **kwargs: object) -> _Response:
            self.assertEqual(lock_depth, 0)
            return remote(request, **kwargs)

        store = self._store(Mock())
        store._urlopen = guarded_remote
        original_process_lock = store._process_lock
        original_factory = store._candidate_factory

        @contextmanager
        def observed_process_lock(
            *,
            deadline: float | None = None,
        ) -> Iterator[float]:
            nonlocal lock_depth
            with original_process_lock(deadline=deadline) as lock_deadline:
                lock_depth += 1
                try:
                    yield lock_deadline
                finally:
                    lock_depth -= 1

        def guarded_factory(model_path: Path, thresholds_path: Path) -> _Candidate:
            self.assertEqual(lock_depth, 0)
            candidate = original_factory(model_path, thresholds_path)
            original_load = candidate.load

            def guarded_load() -> None:
                self.assertEqual(lock_depth, 0)
                original_load()

            candidate.load = guarded_load  # type: ignore[method-assign]
            return candidate

        store._process_lock = observed_process_lock  # type: ignore[method-assign]
        store._candidate_factory = guarded_factory

        loaded = store.load_or_bootstrap()

        self.assertEqual(self._pointer()["generation"], loaded.generation)
        self.assertEqual(lock_depth, 0)

    def test_concurrent_same_generation_rename_adopts_valid_winner(self) -> None:
        remote = _Remote(
            model=b"model",
            thresholds=b"{}",
            model_etag='"model"',
            thresholds_etag='"thresholds"',
        )
        store = self._store(remote)
        remote_before = store._remote_metadata()
        deadline = store_module.time.monotonic() + 10.0
        prepared, _ = store._prepare_download_stage(
            remote_before,
            deadline=deadline,
        )
        destination = self.root / "generations" / prepared.generation
        original_rename = Path.rename

        def lose_race(path: Path, target: Path) -> Path:
            if path == prepared.path:
                shutil.copytree(path, target)
                raise OSError(errno.ENOTEMPTY, "peer won")
            return original_rename(path, target)

        with patch.object(Path, "rename", new=lose_race):
            loaded = store._install_prepared(prepared, deadline=deadline)

        self.assertEqual(loaded.generation, prepared.generation)
        self.assertTrue(destination.is_dir())
        self.assertFalse(prepared.path.exists())


class PonyChartPublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_descriptor = ponychart_module._generation_descriptor
        self.original_store_owner = ponychart_module._store_owner
        self.original_inference_owner = ponychart_module._inference_owner
        self.original_retention_owner = ponychart_module._retention_owner
        self.original_lifecycle_lock = ponychart_module._lifecycle_lock
        self.original_lifecycle_lock_loop = ponychart_module._lifecycle_lock_loop
        ponychart_module._store_owner = Mock()
        ponychart_module._inference_owner = Mock()
        ponychart_module._inference_owner.prepare_async = AsyncMock()
        ponychart_module._inference_owner.retire_superseded_async = AsyncMock()
        ponychart_module._inference_owner.activate.return_value = ()
        ponychart_module._retention_owner = Mock()
        ponychart_module._retention_owner.prepare_async = AsyncMock()
        ponychart_module._lifecycle_lock = None
        ponychart_module._lifecycle_lock_loop = None

    def tearDown(self) -> None:
        with ponychart_module._publication_lock:
            ponychart_module._generation_descriptor = self.original_descriptor
        ponychart_module._store_owner = self.original_store_owner
        ponychart_module._inference_owner = self.original_inference_owner
        ponychart_module._retention_owner = self.original_retention_owner
        ponychart_module._lifecycle_lock = self.original_lifecycle_lock
        ponychart_module._lifecycle_lock_loop = self.original_lifecycle_lock_loop

    async def test_preload_publishes_descriptor_once(self) -> None:
        store_owner = ponychart_module._store_owner
        store_owner.load_or_bootstrap = AsyncMock(
            return_value=LoadedPonyChartGeneration(
                "a" * 64,
                Path("/models/a/model.onnx"),
                Path("/models/a/thresholds.json"),
            )
        )
        with ponychart_module._publication_lock:
            ponychart_module._generation_descriptor = None

        await ponychart_module.preload_ponychart_classifier()
        await ponychart_module.preload_ponychart_classifier()

        store_owner.load_or_bootstrap.assert_awaited_once()
        descriptor = ponychart_module._published_descriptor()
        assert descriptor is not None
        self.assertEqual(descriptor.generation, "a" * 64)
        self.assertEqual(descriptor.model_path, Path("/models/a/model.onnx"))
        self.assertEqual(
            descriptor.thresholds_path,
            Path("/models/a/thresholds.json"),
        )
        self.assertEqual(
            ponychart_module._inference_owner.prepare_async.await_count,
            2,
        )
        self.assertEqual(
            ponychart_module._retention_owner.prepare_async.await_count,
            2,
        )

    async def test_refresh_failure_keeps_published_descriptor(self) -> None:
        old = ponychart_module.PonyChartGenerationDescriptor(
            "a" * 64,
            Path("/models/a/model.onnx"),
            Path("/models/a/thresholds.json"),
        )
        with ponychart_module._publication_lock:
            ponychart_module._generation_descriptor = old
        ponychart_module._store_owner.refresh = AsyncMock(
            side_effect=PonyChartArtifactError("unreachable")
        )

        with self.assertRaises(PonyChartArtifactError):
            await ponychart_module.refresh_ponychart_classifier()

        self.assertIs(ponychart_module._published_descriptor(), old)

    async def test_refresh_passes_one_absolute_lifecycle_deadline_to_all_owners(
        self,
    ) -> None:
        loaded = LoadedPonyChartGeneration(
            "b" * 64,
            Path("/models/b/model.onnx"),
            Path("/models/b/thresholds.json"),
        )
        ponychart_module._store_owner.refresh = AsyncMock(
            return_value=PonyChartStoreRefresh(
                PonyChartRefreshOutcome.UPDATED,
                loaded,
            )
        )
        retired = Mock()
        ponychart_module._inference_owner.activate.return_value = (retired,)
        outer_deadlines: list[float] = []
        original_phase_deadline = ponychart_module._lifecycle_phase_deadline

        def record_phase_deadline(
            expires_at: float,
            *,
            maximum: float,
            reserve: float = 0.0,
            operation: str,
        ) -> float:
            outer_deadlines.append(expires_at)
            return original_phase_deadline(
                expires_at,
                maximum=maximum,
                reserve=reserve,
                operation=operation,
            )

        with patch.object(
            ponychart_module,
            "_lifecycle_phase_deadline",
            record_phase_deadline,
        ):
            outcome = await ponychart_module.refresh_ponychart_classifier()

        self.assertEqual(outcome, PonyChartRefreshOutcome.UPDATED)
        self.assertTrue(outer_deadlines)
        self.assertTrue(
            all(deadline == outer_deadlines[0] for deadline in outer_deadlines)
        )
        refresh_kwargs = ponychart_module._store_owner.refresh.await_args.kwargs
        prepare_kwargs = (
            ponychart_module._inference_owner.prepare_async.await_args.kwargs
        )
        retire_kwargs = (
            ponychart_module._inference_owner.retire_superseded_async.await_args.kwargs
        )
        self.assertIn("expires_at", refresh_kwargs)
        self.assertNotIn("timeout", refresh_kwargs)
        self.assertIn("expires_at", prepare_kwargs)
        self.assertNotIn("timeout", prepare_kwargs)
        self.assertIn("expires_at", retire_kwargs)
        self.assertNotIn("timeout", retire_kwargs)
        self.assertLess(refresh_kwargs["expires_at"], outer_deadlines[0])
        self.assertLess(prepare_kwargs["expires_at"], outer_deadlines[0])
        self.assertLessEqual(retire_kwargs["expires_at"], outer_deadlines[0])

    async def test_inflight_prediction_keeps_old_atomic_snapshot(self) -> None:
        old_started = threading.Event()
        release_old = threading.Event()

        async def predict_reserved(
            descriptor: object,
            _image: bytes,
            *,
            timeout: float,
        ) -> tuple[str, ...]:
            self.assertEqual(timeout, 5.0)
            generation = descriptor.generation  # type: ignore[attr-defined]
            if generation == "a" * 64:
                old_started.set()
                released = await asyncio.to_thread(release_old.wait, 2)
                if not released:
                    raise TimeoutError("old prediction was not released")
                return ("Old Snapshot",)
            return ("New Snapshot",)

        inference_owner = Mock()
        inference_owner.reserve.side_effect = lambda descriptor: descriptor
        inference_owner.predict_reserved = AsyncMock(side_effect=predict_reserved)
        inference_owner.prepare_async = AsyncMock()
        inference_owner.retire_superseded_async = AsyncMock()
        inference_owner.activate.return_value = ()
        ponychart_module._inference_owner = inference_owner
        old = ponychart_module.PonyChartGenerationDescriptor(
            "a" * 64,
            Path("/models/a/model.onnx"),
            Path("/models/a/thresholds.json"),
        )
        with ponychart_module._publication_lock:
            ponychart_module._generation_descriptor = old
        ponychart_module._store_owner.refresh = AsyncMock(
            return_value=PonyChartStoreRefresh(
                PonyChartRefreshOutcome.UPDATED,
                LoadedPonyChartGeneration(
                    "b" * 64,
                    Path("/models/b/model.onnx"),
                    Path("/models/b/thresholds.json"),
                ),
            )
        )

        driver = Mock()
        driver.page = Mock()
        driver.page.evaluate = AsyncMock()
        challenge = PonyChart(driver)

        old_answer = asyncio.create_task(challenge._predict_labels(b"old"))
        self.assertTrue(await asyncio.to_thread(old_started.wait, 1))
        outcome = await ponychart_module.refresh_ponychart_classifier()
        release_old.set()

        self.assertEqual(outcome, PonyChartRefreshOutcome.UPDATED)
        self.assertEqual(await old_answer, ("Old Snapshot",))
        self.assertEqual(
            await challenge._predict_labels(b"new"),
            ("New Snapshot",),
        )
        driver.page.evaluate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

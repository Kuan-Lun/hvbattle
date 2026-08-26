"""Crash-safe immutable storage for PonyChart classifier artifacts."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import ssl
import tempfile
import time
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

_MODEL_FILENAME = "model.onnx"
_THRESHOLDS_FILENAME = "thresholds.json"
_MANIFEST_FILENAME = "manifest.json"
_CURRENT_FILENAME = "current.json"
_LOCK_FILENAME = ".update.lock"
_DEFAULT_BASE_URL = "https://www.csie.ntu.edu.tw/~d06922002/ponychart_classifier"
_COMPATIBILITY_ORIGIN = ("https", "www.csie.ntu.edu.tw", 443)
_GENERATION_SCHEMA = 1
_POINTER_SCHEMA = 1
_COPY_BUFFER_SIZE = 1024 * 1024
_DEFAULT_CONNECT_TIMEOUT = 5.0
_DEFAULT_READ_IDLE_TIMEOUT = 5.0
_DEFAULT_REFRESH_TIMEOUT = 120.0
_METADATA_SNAPSHOT_TIMEOUT = 5.0
_PROCESS_LOCK_TIMEOUT = 5.0
_PROCESS_LOCK_RETRY_INTERVAL = 0.05
_DEFAULT_MAX_MODEL_BYTES = 512 * 1024 * 1024
_DEFAULT_MAX_THRESHOLDS_BYTES = 4 * 1024 * 1024
_ETAG_PATTERN = re.compile(r'^(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"$')


class PonyChartRefreshOutcome(StrEnum):
    """Typed outcome of a successful PonyChart artifact refresh check."""

    UPDATED = "updated"
    CURRENT = "current"


class PonyChartArtifactError(RuntimeError):
    """Raised when a model generation cannot be downloaded or committed safely."""


class _ClassifierCandidate(Protocol):
    def load(self) -> None: ...


type _CandidateFactory = Callable[[Path, Path], _ClassifierCandidate]
type _UrlOpen = Callable[..., AbstractContextManager[Any]]


@dataclass(frozen=True, slots=True)
class _RemoteMetadata:
    model_etag: str | None
    thresholds_etag: str | None

    @property
    def is_complete(self) -> bool:
        return self.model_etag is not None and self.thresholds_etag is not None

    def as_json(self) -> dict[str, str | None]:
        return {
            _MODEL_FILENAME: self.model_etag,
            _THRESHOLDS_FILENAME: self.thresholds_etag,
        }


@dataclass(frozen=True, slots=True)
class _CurrentPointer:
    generation: str
    remote: _RemoteMetadata


@dataclass(frozen=True, slots=True)
class LoadedPonyChartGeneration:
    """Pickle-safe descriptor for a fully validated immutable generation."""

    generation: str
    model_path: Path
    thresholds_path: Path


@dataclass(frozen=True, slots=True)
class PonyChartStoreRefresh:
    """Store-level refresh result and optional validated descriptor."""

    outcome: PonyChartRefreshOutcome
    loaded: LoadedPonyChartGeneration | None


@dataclass(frozen=True, slots=True)
class PonyChartStoreConfig:
    """Pickle-safe construction inputs for the isolated artifact store."""

    root: Path | None = None
    base_url: str = _DEFAULT_BASE_URL
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT
    read_idle_timeout: float = _DEFAULT_READ_IDLE_TIMEOUT
    refresh_timeout: float = _DEFAULT_REFRESH_TIMEOUT
    max_model_bytes: int = _DEFAULT_MAX_MODEL_BYTES
    max_thresholds_bytes: int = _DEFAULT_MAX_THRESHOLDS_BYTES


@dataclass(frozen=True, slots=True)
class _PreparedStage:
    path: Path
    generation: str


def _default_candidate_factory(
    model_path: Path,
    thresholds_path: Path,
) -> _ClassifierCandidate:
    from ponychart_classifier import PonyChartClassifier

    return PonyChartClassifier(
        model_path=model_path,
        thresholds_path=thresholds_path,
    )


def _verified_context(*, strict: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        pass
    else:
        # Keep the platform trust store and add Mozilla's roots. Passing
        # ``cafile`` to create_default_context() would replace, rather than
        # augment, the system roots.
        context.load_verify_locations(cafile=certifi.where())

    if strict:
        context.verify_flags |= ssl.VERIFY_X509_STRICT
    else:
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise RuntimeError("PonyChart artifact TLS context is not verified")
    return context


def _is_missing_subject_key_identifier(error: URLError) -> bool:
    reason = error.reason
    return (
        isinstance(reason, ssl.SSLCertVerificationError)
        and getattr(reason, "verify_code", None) == 86
        and getattr(reason, "verify_message", None) == "Missing Subject Key Identifier"
    )


def _fsync_directory(path: Path, *, deadline: float | None = None) -> None:
    if deadline is not None:
        _remaining(deadline, message="PonyChart filesystem deadline expired")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if _directory_fsync_is_unsupported(error):
            return
        raise
    try:
        try:
            if deadline is not None:
                _remaining(
                    deadline,
                    message="PonyChart filesystem deadline expired before fsync",
                )
            os.fsync(descriptor)
            if deadline is not None:
                _remaining(
                    deadline,
                    message="PonyChart filesystem deadline expired during fsync",
                )
        except OSError as error:
            if not _directory_fsync_is_unsupported(error):
                raise
    finally:
        os.close(descriptor)


def _directory_fsync_is_unsupported(error: OSError) -> bool:
    unsupported = {
        errno.EBADF,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    if os.name == "nt":
        unsupported.add(errno.EACCES)
    return error.errno in unsupported


def _remaining(deadline: float, *, message: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(message)
    return remaining


def _acquire_windows_file_lock(descriptor: int, *, deadline: float) -> None:
    import msvcrt

    retryable = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
    while True:
        _remaining(deadline, message="PonyChart update lock deadline expired")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(  # type: ignore[attr-defined]
                descriptor,
                msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                1,
            )
            return
        except OSError as error:
            if error.errno not in retryable:
                raise
            remaining = _remaining(
                deadline,
                message="PonyChart update lock deadline expired",
            )
            time.sleep(min(_PROCESS_LOCK_RETRY_INTERVAL, remaining))


def _acquire_posix_file_lock(descriptor: int, *, deadline: float) -> None:
    import fcntl

    retryable = {errno.EACCES, errno.EAGAIN}
    while True:
        _remaining(deadline, message="PonyChart update lock deadline expired")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in retryable:
                raise
            remaining = _remaining(
                deadline,
                message="PonyChart update lock deadline expired",
            )
            time.sleep(min(_PROCESS_LOCK_RETRY_INTERVAL, remaining))


def _release_windows_file_lock(descriptor: int) -> None:
    import msvcrt

    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(  # type: ignore[attr-defined]
        descriptor,
        msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
        1,
    )


def _write_bytes_fsynced(
    path: Path,
    content: bytes,
    *,
    deadline: float | None = None,
) -> None:
    if deadline is not None:
        _remaining(deadline, message="PonyChart filesystem deadline expired")
    with path.open("wb") as stream:
        stream.write(content)
        stream.flush()
        if deadline is not None:
            _remaining(
                deadline,
                message="PonyChart filesystem deadline expired before fsync",
            )
        os.fsync(stream.fileno())
    if deadline is not None:
        _remaining(
            deadline,
            message="PonyChart filesystem deadline expired during fsync",
        )


def _write_json_fsynced(
    path: Path,
    content: Mapping[str, object],
    *,
    deadline: float | None = None,
) -> None:
    encoded = (
        json.dumps(content, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    _write_bytes_fsynced(path, encoded, deadline=deadline)


def _hash_file(path: Path, *, deadline: float | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            if deadline is not None:
                _remaining(
                    deadline,
                    message="PonyChart validation deadline expired while hashing",
                )
            chunk = stream.read(_COPY_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    if deadline is not None:
        _remaining(
            deadline,
            message="PonyChart validation deadline expired while hashing",
        )
    return digest.hexdigest(), size


def _generation_id(model_sha256: str, thresholds_sha256: str) -> str:
    digest = hashlib.sha256()
    for filename, artifact_hash in (
        (_MODEL_FILENAME, model_sha256),
        (_THRESHOLDS_FILENAME, thresholds_sha256),
    ):
        digest.update(filename.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(artifact_hash))
        digest.update(b"\0")
    return digest.hexdigest()


def _valid_generation_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise PonyChartArtifactError("current.json contains an invalid generation id")
    try:
        bytes.fromhex(value)
    except ValueError as error:
        raise PonyChartArtifactError(
            "current.json contains an invalid generation id"
        ) from error
    return value


def _optional_etag(value: object, *, filename: str) -> str | None:
    if value is None:
        return None
    normalized = _normalize_etag(value)
    if normalized is None:
        raise PonyChartArtifactError(
            f"current.json contains an invalid ETag for {filename}"
        )
    return normalized


def _normalize_etag(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not _ETAG_PATTERN.fullmatch(normalized):
        return None
    return normalized


class PonyChartGenerationStore:
    """Manage immutable artifact generations behind one atomic pointer."""

    def __init__(
        self,
        *,
        root: Path,
        base_url: str = _DEFAULT_BASE_URL,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        read_idle_timeout: float = _DEFAULT_READ_IDLE_TIMEOUT,
        refresh_timeout: float = _DEFAULT_REFRESH_TIMEOUT,
        max_model_bytes: int = _DEFAULT_MAX_MODEL_BYTES,
        max_thresholds_bytes: int = _DEFAULT_MAX_THRESHOLDS_BYTES,
        candidate_factory: _CandidateFactory = _default_candidate_factory,
        urlopen: _UrlOpen = urllib.request.urlopen,
    ) -> None:
        if (
            isinstance(connect_timeout, bool)
            or not isinstance(connect_timeout, int | float)
            or not math.isfinite(connect_timeout)
            or connect_timeout <= 0
            or connect_timeout > _DEFAULT_CONNECT_TIMEOUT
        ):
            raise ValueError("connect_timeout must be finite and in (0, 5]")
        if (
            isinstance(read_idle_timeout, bool)
            or not isinstance(read_idle_timeout, int | float)
            or not math.isfinite(read_idle_timeout)
            or read_idle_timeout <= 0
            or read_idle_timeout > _DEFAULT_READ_IDLE_TIMEOUT
        ):
            raise ValueError("read_idle_timeout must be finite and in (0, 5]")
        if (
            isinstance(refresh_timeout, bool)
            or not isinstance(refresh_timeout, int | float)
            or not math.isfinite(refresh_timeout)
            or refresh_timeout <= 0
            or refresh_timeout > _DEFAULT_REFRESH_TIMEOUT
        ):
            raise ValueError("refresh_timeout must be finite and in (0, 120]")
        if max_model_bytes <= 0 or max_thresholds_bytes <= 0:
            raise ValueError("artifact size limits must be positive")
        parsed_base_url = urlsplit(base_url)
        if parsed_base_url.scheme.lower() != "https":
            raise ValueError("PonyChart artifact base URL must use HTTPS")
        self._root = root
        self._generations = root / "generations"
        self._current_path = root / _CURRENT_FILENAME
        self._base_url = base_url.rstrip("/")
        self._connect_timeout = connect_timeout
        self._read_idle_timeout = read_idle_timeout
        self._refresh_timeout = refresh_timeout
        self._max_artifact_bytes = {
            _MODEL_FILENAME: max_model_bytes,
            _THRESHOLDS_FILENAME: max_thresholds_bytes,
        }
        self._candidate_factory = candidate_factory
        self._urlopen = urlopen
        self._allows_missing_ski_compatibility = (
            parsed_base_url.scheme.lower(),
            parsed_base_url.hostname,
            parsed_base_url.port or 443,
        ) == _COMPATIBILITY_ORIGIN
        self._strict_ssl_context = _verified_context(strict=True)
        self._compatibility_ssl_context = _verified_context(strict=False)
        self._active_ssl_context = self._strict_ssl_context

    @classmethod
    def default(cls) -> PonyChartGenerationStore:
        """Build the store beside ponychart-classifier's canonical cache files."""

        return cls.from_config(cls.default_config())

    @classmethod
    def default_config(cls) -> PonyChartStoreConfig:
        """Describe the production store without importing the ML package."""

        return PonyChartStoreConfig()

    @classmethod
    def from_config(cls, config: PonyChartStoreConfig) -> PonyChartGenerationStore:
        """Construct a store from data that can cross a spawn boundary."""

        root = config.root
        if root is None:
            from ponychart_classifier.inference import artifacts

            root = artifacts.DEFAULT_ARTIFACT_DIR
        return cls(
            root=root,
            base_url=config.base_url,
            connect_timeout=config.connect_timeout,
            read_idle_timeout=config.read_idle_timeout,
            refresh_timeout=config.refresh_timeout,
            max_model_bytes=config.max_model_bytes,
            max_thresholds_bytes=config.max_thresholds_bytes,
        )

    def load_or_bootstrap(
        self,
        *,
        deadline: float | None = None,
    ) -> LoadedPonyChartGeneration:
        """Load the committed pointer or safely create the first generation."""

        deadline = self._bounded_operation_deadline(deadline)
        pointer = self._read_pointer_serialized(deadline=deadline)
        if pointer is not None:
            return self._load_pointer(pointer, deadline=deadline)

        return self._bootstrap(deadline=deadline)

    def _bootstrap(self, *, deadline: float) -> LoadedPonyChartGeneration:
        remote_before = self._remote_metadata(deadline=deadline)
        self._require_refresh_budget(
            deadline,
            "PonyChart bootstrap deadline expired after metadata snapshot",
        )
        prepared, effective_remote = self._prepare_download_stage(
            remote_before,
            deadline=deadline,
        )
        _, loaded = self._publish_prepared(
            prepared,
            effective_remote,
            expected_pointer=None,
            deadline=deadline,
        )
        return loaded

    def refresh(
        self,
        published_generation: str | None,
        *,
        deadline: float | None = None,
    ) -> PonyChartStoreRefresh:
        """Adopt a peer commit or safely check and install a remote generation."""

        deadline = self._bounded_operation_deadline(deadline)
        pointer = self._read_pointer_serialized(deadline=deadline)
        if pointer is None:
            loaded = self._bootstrap(deadline=deadline)
            return PonyChartStoreRefresh(PonyChartRefreshOutcome.UPDATED, loaded)
        if pointer.generation != published_generation:
            return PonyChartStoreRefresh(
                PonyChartRefreshOutcome.UPDATED,
                self._load_pointer(pointer, deadline=deadline),
            )

        self._validate_generation(
            self._generations / pointer.generation,
            pointer.generation,
            deadline=deadline,
        )
        remote_before = self._remote_metadata(deadline=deadline)
        self._require_refresh_budget(
            deadline,
            "PonyChart refresh deadline expired after metadata snapshot",
        )

        # A peer may have committed while this process checked the remote. This
        # lock protects only the pointer decision; no network, hashing, or model
        # loading occurs while it is held.
        current = self._read_pointer_serialized(deadline=deadline)
        if current is None:
            raise PonyChartArtifactError(
                "PonyChart current pointer disappeared during refresh"
            )
        if current != pointer:
            return self._adopt_pointer(
                current,
                published_generation=published_generation,
                deadline=deadline,
            )
        if (
            pointer.remote.is_complete
            and remote_before.is_complete
            and pointer.remote == remote_before
        ):
            return PonyChartStoreRefresh(PonyChartRefreshOutcome.CURRENT, None)

        prepared, effective_remote = self._prepare_download_stage(
            remote_before,
            deadline=deadline,
        )
        committed_pointer, loaded = self._publish_prepared(
            prepared,
            effective_remote,
            expected_pointer=pointer,
            deadline=deadline,
        )
        if committed_pointer.generation == published_generation:
            return PonyChartStoreRefresh(PonyChartRefreshOutcome.CURRENT, None)
        return PonyChartStoreRefresh(PonyChartRefreshOutcome.UPDATED, loaded)

    def _bounded_operation_deadline(self, deadline: float | None) -> float:
        local_deadline = time.monotonic() + self._refresh_timeout
        if deadline is None:
            return local_deadline
        if not math.isfinite(deadline):
            raise ValueError("PonyChart operation deadline must be finite")
        bounded = min(deadline, local_deadline)
        self._require_refresh_budget(
            bounded,
            "PonyChart artifact operation deadline expired before it started",
        )
        return bounded

    def _adopt_pointer(
        self,
        pointer: _CurrentPointer,
        *,
        published_generation: str | None,
        deadline: float,
    ) -> PonyChartStoreRefresh:
        if pointer.generation == published_generation:
            return PonyChartStoreRefresh(PonyChartRefreshOutcome.CURRENT, None)
        loaded = self._load_pointer(pointer, deadline=deadline)
        return PonyChartStoreRefresh(PonyChartRefreshOutcome.UPDATED, loaded)

    def _publish_prepared(
        self,
        prepared: _PreparedStage,
        remote: _RemoteMetadata,
        *,
        expected_pointer: _CurrentPointer | None,
        deadline: float,
    ) -> tuple[_CurrentPointer, LoadedPonyChartGeneration]:
        """Publish a stage without holding the process lock during validation."""

        try:
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired before generation promotion",
            )
            # Atomic rename makes this directory immutable. Promotion, directory
            # fsync, expensive hashes, and candidate loading therefore all happen
            # before the short compare-and-commit pointer lock.
            self._promote_prepared(prepared, deadline=deadline)
            loaded = self._load_generation(prepared.generation, deadline=deadline)
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired before pointer commit",
            )
            pointer = _CurrentPointer(generation=loaded.generation, remote=remote)
            with self._process_lock(deadline=deadline) as lock_deadline:
                current = self._read_pointer()
                if current == expected_pointer:
                    self._require_refresh_budget(
                        lock_deadline,
                        "PonyChart refresh deadline expired before pointer commit",
                    )
                    self._commit_pointer(pointer, deadline=lock_deadline)
                    peer_pointer = None
                else:
                    peer_pointer = current

            if peer_pointer is not None:
                peer_loaded = self._load_pointer(peer_pointer, deadline=deadline)
                return peer_pointer, peer_loaded
            if current != expected_pointer:
                raise PonyChartArtifactError(
                    "PonyChart current pointer disappeared during commit"
                )
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired during pointer commit",
            )
            return pointer, loaded
        finally:
            if prepared.path.exists():
                self._discard_stage(prepared.path)

    def _read_pointer_serialized(
        self,
        *,
        deadline: float | None = None,
    ) -> _CurrentPointer | None:
        with self._process_lock(deadline=deadline):
            return self._read_pointer()

    @staticmethod
    def _require_refresh_budget(deadline: float, message: str) -> float:
        return _remaining(deadline, message=message)

    @contextmanager
    def _process_lock(self, *, deadline: float | None = None) -> Iterator[float]:
        """Yield a deadline bounding setup, acquisition, and the critical section."""

        now = time.monotonic()
        lock_deadline = now + _PROCESS_LOCK_TIMEOUT
        if deadline is not None:
            if not math.isfinite(deadline):
                raise ValueError("process lock deadline must be finite")
            lock_deadline = min(lock_deadline, deadline)
        _remaining(
            lock_deadline,
            message="PonyChart update lock deadline expired",
        )
        self._prepare_directories(deadline=lock_deadline)
        _remaining(
            lock_deadline,
            message="PonyChart update lock deadline expired",
        )
        lock_path = self._root / _LOCK_FILENAME
        created = not lock_path.exists()
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                _remaining(
                    lock_deadline,
                    message="PonyChart update lock deadline expired before fsync",
                )
                os.fsync(descriptor)
                _remaining(
                    lock_deadline,
                    message="PonyChart update lock deadline expired during fsync",
                )
            if created:
                _fsync_directory(self._root, deadline=lock_deadline)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                _acquire_windows_file_lock(descriptor, deadline=lock_deadline)
            else:
                _acquire_posix_file_lock(descriptor, deadline=lock_deadline)
            try:
                _remaining(
                    lock_deadline,
                    message="PonyChart update lock deadline expired after acquisition",
                )
                yield lock_deadline
                _remaining(
                    lock_deadline,
                    message="PonyChart update lock deadline expired while held",
                )
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    _release_windows_file_lock(descriptor)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _prepare_directories(self, *, deadline: float | None = None) -> None:
        if deadline is not None:
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired before directory preparation",
            )
        root_existed = self._root.exists()
        generations_existed = self._generations.exists()
        self._root.mkdir(parents=True, exist_ok=True)
        if not root_existed:
            _fsync_directory(self._root.parent, deadline=deadline)
        self._generations.mkdir(exist_ok=True)
        if not generations_existed:
            _fsync_directory(self._root, deadline=deadline)

    def _new_stage(self, *, deadline: float) -> Path:
        self._prepare_directories(deadline=deadline)
        stage = self._generations / f".staging-{uuid.uuid4().hex}"
        stage.mkdir(mode=0o700)
        _fsync_directory(self._generations, deadline=deadline)
        return stage

    def _prepare_download_stage(
        self,
        remote_before: _RemoteMetadata,
        *,
        deadline: float,
    ) -> tuple[_PreparedStage, _RemoteMetadata]:
        self._require_refresh_budget(
            deadline,
            "PonyChart refresh deadline expired before staging",
        )
        stage = self._new_stage(deadline=deadline)
        try:
            model_response_etag = self._download(
                _MODEL_FILENAME,
                stage / _MODEL_FILENAME,
                deadline=deadline,
            )
            thresholds_response_etag = self._download(
                _THRESHOLDS_FILENAME,
                stage / _THRESHOLDS_FILENAME,
                deadline=deadline,
            )
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired before final metadata snapshot",
            )
            remote_after = self._remote_metadata(deadline=deadline)
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired after final metadata snapshot",
            )
            self._assert_stable_etags(
                remote_before.model_etag,
                model_response_etag,
                remote_after.model_etag,
                filename=_MODEL_FILENAME,
            )
            self._assert_stable_etags(
                remote_before.thresholds_etag,
                thresholds_response_etag,
                remote_after.thresholds_etag,
                filename=_THRESHOLDS_FILENAME,
            )
            effective_remote = _RemoteMetadata(
                # Only the GET ETag is response-associated with the bytes we
                # validated. HEAD-only metadata must never make those bytes
                # look authoritative on the next refresh.
                model_etag=model_response_etag,
                thresholds_etag=thresholds_response_etag,
            )
            prepared = self._validate_stage(stage, deadline=deadline)
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired during stage validation",
            )
            return prepared, effective_remote
        except BaseException:
            self._discard_stage(stage)
            raise

    def _download(
        self,
        filename: str,
        destination: Path,
        *,
        deadline: float,
    ) -> str | None:
        requested_url = f"{self._base_url}/{filename}"
        request = urllib.request.Request(requested_url)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("artifact bundle transfer deadline expired")
            with self._open_verified(
                request,
                deadline=deadline,
            ) as response:
                self._assert_https_response(response, requested_url=requested_url)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("artifact bundle transfer deadline expired")
                self._set_response_read_idle_timeout(
                    response,
                    min(self._read_idle_timeout, remaining),
                )
                expected_size = self._content_length(response, filename=filename)
                size_limit = self._max_artifact_bytes[filename]
                if expected_size > size_limit:
                    raise PonyChartArtifactError(
                        f"PonyChart artifact exceeds size limit: {filename}"
                    )
                read_one = getattr(response, "read1", None)
                if not callable(read_one):
                    raise PonyChartArtifactError(
                        f"PonyChart artifact response is not stream-bounded: {filename}"
                    )
                received = 0
                with destination.open("wb") as stream:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                "artifact bundle transfer deadline expired"
                            )
                        self._set_response_read_idle_timeout(
                            response,
                            min(self._read_idle_timeout, remaining),
                        )
                        chunk = read_one(_COPY_BUFFER_SIZE)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > size_limit:
                            raise PonyChartArtifactError(
                                f"PonyChart artifact exceeds size limit: {filename}"
                            )
                        if received > expected_size:
                            raise PonyChartArtifactError(
                                f"PonyChart artifact body length mismatch: {filename}"
                            )
                        stream.write(chunk)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("artifact bundle transfer deadline expired")
                    if received != expected_size:
                        raise PonyChartArtifactError(
                            f"PonyChart artifact body length mismatch: {filename}"
                        )
                    stream.flush()
                    self._require_refresh_budget(
                        deadline,
                        "PonyChart transfer deadline expired before artifact fsync",
                    )
                    os.fsync(stream.fileno())
                    self._require_refresh_budget(
                        deadline,
                        "PonyChart transfer deadline expired during artifact fsync",
                    )
                return _normalize_etag(response.headers.get("ETag"))
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise PonyChartArtifactError(
                f"Failed to download PonyChart artifact {filename}: "
                f"{type(error).__name__}"
            ) from error

    def _remote_metadata(self, *, deadline: float | None = None) -> _RemoteMetadata:
        now = time.monotonic()
        snapshot_deadline = now + _METADATA_SNAPSHOT_TIMEOUT
        if deadline is not None:
            snapshot_deadline = min(snapshot_deadline, deadline)
        return _RemoteMetadata(
            model_etag=self._remote_etag(
                _MODEL_FILENAME,
                deadline=snapshot_deadline,
            ),
            thresholds_etag=self._remote_etag(
                _THRESHOLDS_FILENAME,
                deadline=snapshot_deadline,
            ),
        )

    def _remote_etag(
        self,
        filename: str,
        *,
        deadline: float | None = None,
    ) -> str | None:
        now = time.monotonic()
        operation_deadline = now + _METADATA_SNAPSHOT_TIMEOUT
        if deadline is not None:
            operation_deadline = min(operation_deadline, deadline)
        requested_url = f"{self._base_url}/{filename}"
        request = urllib.request.Request(requested_url, method="HEAD")
        try:
            with self._open_verified(
                request,
                deadline=operation_deadline,
            ) as response:
                self._assert_https_response(response, requested_url=requested_url)
                return _normalize_etag(response.headers.get("ETag"))
        except HTTPError, URLError, TimeoutError, OSError:
            # HEAD metadata is only a refresh optimization. The GET remains
            # authoritative and is still required to succeed fail-closed.
            return None

    def _open_verified(
        self,
        request: urllib.request.Request,
        *,
        deadline: float,
    ) -> AbstractContextManager[Any]:
        context = self._active_ssl_context
        try:
            return self._urlopen(
                request,
                context=context,
                timeout=min(
                    self._connect_timeout,
                    _remaining(
                        deadline,
                        message="PonyChart network phase deadline expired",
                    ),
                ),
            )
        except URLError as error:
            if (
                context is not self._strict_ssl_context
                or not self._allows_missing_ski_compatibility
                or not _is_missing_subject_key_identifier(error)
            ):
                raise
        # Python 3.14 enables OpenSSL's strict RFC 5280 checks. The public
        # artifact host chains to a legacy trust anchor without an SKI. Retry
        # only that exact verification error while retaining CA and hostname
        # verification. Remember the proven host compatibility requirement so
        # later requests do not repeat a strict handshake that cannot succeed.
        self._active_ssl_context = self._compatibility_ssl_context
        return self._urlopen(
            request,
            context=self._active_ssl_context,
            timeout=min(
                self._connect_timeout,
                _remaining(
                    deadline,
                    message="PonyChart network phase deadline expired",
                ),
            ),
        )

    @staticmethod
    def _set_response_read_idle_timeout(response: Any, timeout: float) -> None:
        """Apply the idle watchdog to urllib's connected socket when exposed."""
        fp = getattr(response, "fp", None)
        raw = getattr(fp, "raw", None)
        socket = getattr(raw, "_sock", None)
        for candidate in (socket, raw, fp, response):
            setter = getattr(candidate, "settimeout", None)
            if callable(setter):
                setter(timeout)
                return

    @staticmethod
    def _assert_https_response(response: Any, *, requested_url: str) -> None:
        try:
            final_url = response.geturl()
        except (AttributeError, TypeError) as error:
            raise PonyChartArtifactError(
                f"Artifact response has no final URL: {requested_url}"
            ) from error
        if (
            not isinstance(final_url, str)
            or urlsplit(final_url).scheme.lower() != "https"
        ):
            raise PonyChartArtifactError(
                f"PonyChart artifact redirect left HTTPS: {requested_url}"
            )

    @staticmethod
    def _content_length(response: Any, *, filename: str) -> int:
        transfer_encoding = response.headers.get("Transfer-Encoding")
        if (
            isinstance(transfer_encoding, str)
            and "chunked" in transfer_encoding.lower()
        ):
            raise PonyChartArtifactError(
                f"Chunked PonyChart artifact response is unsupported: {filename}"
            )
        raw = response.headers.get("Content-Length")
        if raw is None:
            raise PonyChartArtifactError(
                f"PonyChart artifact response has no Content-Length: {filename}"
            )
        try:
            length = int(raw)
        except (TypeError, ValueError) as error:
            raise PonyChartArtifactError(
                f"Invalid Content-Length for PonyChart artifact: {filename}"
            ) from error
        if length < 0:
            raise PonyChartArtifactError(
                f"Invalid Content-Length for PonyChart artifact: {filename}"
            )
        return length

    @staticmethod
    def _assert_stable_etags(
        before: str | None,
        during: str | None,
        after: str | None,
        *,
        filename: str,
    ) -> None:
        observed = {etag for etag in (before, during, after) if etag is not None}
        if len(observed) > 1:
            raise PonyChartArtifactError(
                f"Remote PonyChart artifact changed during download: {filename}"
            )

    def _validate_stage(self, stage: Path, *, deadline: float) -> _PreparedStage:
        model_path = stage / _MODEL_FILENAME
        thresholds_path = stage / _THRESHOLDS_FILENAME
        self._require_refresh_budget(
            deadline,
            "PonyChart refresh deadline expired before candidate validation",
        )
        candidate = self._candidate_factory(model_path, thresholds_path)
        candidate.load()
        self._require_refresh_budget(
            deadline,
            "PonyChart refresh deadline expired during candidate validation",
        )
        model_hash, model_size = _hash_file(model_path, deadline=deadline)
        thresholds_hash, thresholds_size = _hash_file(
            thresholds_path,
            deadline=deadline,
        )
        generation = _generation_id(model_hash, thresholds_hash)
        manifest: dict[str, object] = {
            "schema": _GENERATION_SCHEMA,
            "generation": generation,
            "artifacts": {
                _MODEL_FILENAME: {
                    "sha256": model_hash,
                    "size": model_size,
                },
                _THRESHOLDS_FILENAME: {
                    "sha256": thresholds_hash,
                    "size": thresholds_size,
                },
            },
        }
        _write_json_fsynced(
            stage / _MANIFEST_FILENAME,
            manifest,
            deadline=deadline,
        )
        _fsync_directory(stage, deadline=deadline)
        return _PreparedStage(path=stage, generation=generation)

    def _promote_prepared(
        self,
        prepared: _PreparedStage,
        *,
        deadline: float,
    ) -> Path:
        """Atomically expose validated bytes without loading under the lock."""

        self._require_refresh_budget(
            deadline,
            "PonyChart refresh deadline expired before generation promotion",
        )
        destination = self._generations / prepared.generation
        if not destination.exists():
            try:
                prepared.path.rename(destination)
            except OSError:
                if not destination.exists():
                    raise
            _fsync_directory(self._generations, deadline=deadline)
        return destination

    def _install_prepared(
        self,
        prepared: _PreparedStage,
        *,
        deadline: float,
    ) -> LoadedPonyChartGeneration:
        """Promote and load a stage for focused storage-level tests."""

        try:
            self._promote_prepared(prepared, deadline=deadline)
            return self._load_generation(prepared.generation, deadline=deadline)
        finally:
            if prepared.path.exists():
                self._discard_stage(prepared.path)

    def _load_generation(
        self,
        generation: str,
        *,
        deadline: float | None = None,
    ) -> LoadedPonyChartGeneration:
        destination = self._generations / generation
        self._validate_generation(destination, generation, deadline=deadline)
        if deadline is not None:
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired before canonical model load",
            )
        candidate = self._candidate_factory(
            destination / _MODEL_FILENAME,
            destination / _THRESHOLDS_FILENAME,
        )
        candidate.load()
        if deadline is not None:
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired during canonical model load",
            )
        return LoadedPonyChartGeneration(
            generation=generation,
            model_path=destination / _MODEL_FILENAME,
            thresholds_path=destination / _THRESHOLDS_FILENAME,
        )

    def _load_pointer(
        self,
        pointer: _CurrentPointer,
        *,
        deadline: float | None = None,
    ) -> LoadedPonyChartGeneration:
        return self._load_generation(pointer.generation, deadline=deadline)

    def _validate_generation(
        self,
        path: Path,
        expected_generation: str,
        *,
        deadline: float | None = None,
    ) -> None:
        if deadline is not None:
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired before generation validation",
            )
        if not path.is_dir():
            raise PonyChartArtifactError(
                f"Committed PonyChart generation is missing: {expected_generation}"
            )
        try:
            manifest_raw = json.loads(
                (path / _MANIFEST_FILENAME).read_text(encoding="utf-8")
            )
            if not isinstance(manifest_raw, dict):
                raise TypeError
            if manifest_raw.get("schema") != _GENERATION_SCHEMA:
                raise ValueError
            if manifest_raw.get("generation") != expected_generation:
                raise ValueError
            artifacts = manifest_raw.get("artifacts")
            if not isinstance(artifacts, dict):
                raise TypeError
            model_manifest = artifacts[_MODEL_FILENAME]
            thresholds_manifest = artifacts[_THRESHOLDS_FILENAME]
            if not isinstance(model_manifest, dict) or not isinstance(
                thresholds_manifest, dict
            ):
                raise TypeError
            model_hash, model_size = _hash_file(
                path / _MODEL_FILENAME,
                deadline=deadline,
            )
            thresholds_hash, thresholds_size = _hash_file(
                path / _THRESHOLDS_FILENAME,
                deadline=deadline,
            )
            if model_manifest != {"sha256": model_hash, "size": model_size}:
                raise ValueError
            if thresholds_manifest != {
                "sha256": thresholds_hash,
                "size": thresholds_size,
            }:
                raise ValueError
            if _generation_id(model_hash, thresholds_hash) != expected_generation:
                raise ValueError
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise PonyChartArtifactError(
                f"Committed PonyChart generation is corrupt: {expected_generation}"
            ) from error
        if deadline is not None:
            self._require_refresh_budget(
                deadline,
                "PonyChart refresh deadline expired during generation validation",
            )

    def _read_pointer(self) -> _CurrentPointer | None:
        try:
            content = self._current_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise PonyChartArtifactError("Unable to read current.json") from error
        try:
            raw = json.loads(content)
            if not isinstance(raw, dict) or raw.get("schema") != _POINTER_SCHEMA:
                raise ValueError
            generation = _valid_generation_id(raw.get("generation"))
            remote = raw.get("remote_etags")
            if not isinstance(remote, dict):
                raise TypeError
            metadata = _RemoteMetadata(
                model_etag=_optional_etag(
                    remote.get(_MODEL_FILENAME),
                    filename=_MODEL_FILENAME,
                ),
                thresholds_etag=_optional_etag(
                    remote.get(_THRESHOLDS_FILENAME),
                    filename=_THRESHOLDS_FILENAME,
                ),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise PonyChartArtifactError("current.json is invalid") from error
        return _CurrentPointer(generation, metadata)

    def _commit_pointer(self, pointer: _CurrentPointer, *, deadline: float) -> None:
        self._require_refresh_budget(
            deadline,
            "PonyChart refresh deadline expired before pointer staging",
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._root,
            prefix=".current-",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                encoded = (
                    json.dumps(
                        {
                            "schema": _POINTER_SCHEMA,
                            "generation": pointer.generation,
                            "remote_etags": pointer.remote.as_json(),
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
                stream.write(encoded)
                stream.flush()
                self._require_refresh_budget(
                    deadline,
                    "PonyChart refresh deadline expired before pointer fsync",
                )
                os.fsync(stream.fileno())
                self._require_refresh_budget(
                    deadline,
                    "PonyChart refresh deadline expired during pointer fsync",
                )
            os.replace(temporary, self._current_path)
            _fsync_directory(self._root, deadline=deadline)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _discard_stage(self, stage: Path) -> None:
        if stage.parent != self._generations or not stage.name.startswith(".staging-"):
            raise RuntimeError(f"Refusing to remove non-staging path: {stage}")
        shutil.rmtree(stage, ignore_errors=True)
        _fsync_directory(self._generations)


__all__ = [
    "LoadedPonyChartGeneration",
    "PonyChartArtifactError",
    "PonyChartGenerationStore",
    "PonyChartRefreshOutcome",
    "PonyChartStoreRefresh",
]

import asyncio
import base64
import binascii
import math
import struct
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from hvbrowser import HVDriver
from hvbrowser.runtime import (
    ZendriverOperationTimeout,
    is_browser_generation_error,
    notify,
    setup_logger,
    wait_for_zendriver,
)
from zendriver import cdp

from .contracts import BattleInterruptedError
from .ponychart_model_store import (
    PonyChartGenerationStore,
    PonyChartRefreshOutcome,
)

logger = setup_logger(__name__)

_PONYCHART_DOM_CAPTURE_TIMEOUT_SECONDS = 4.0
_PONYCHART_SCREENSHOT_TIMEOUT_SECONDS = 10.0
_PONYCHART_MUTATION_TIMEOUT_SECONDS = 15.0
_SELECTOR_OUTER_TIMEOUT_MARGIN_SECONDS = 2.0
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CANVAS_CAPTURE_JS = r"""
(() => {
    const container = document.getElementById("riddleimage");
    const image = container && container.querySelector("img");
    if (!image || !image.complete) {
        return {status: "error", errorName: "ImageNotReady"};
    }
    const width = image.naturalWidth;
    const height = image.naturalHeight;
    if (!Number.isFinite(width) || width <= 0
            || !Number.isFinite(height) || height <= 0) {
        return {status: "error", errorName: "InvalidImageGeometry"};
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
    const container = document.getElementById("riddleimage");
    const image = container && container.querySelector("img");
    if (!image || !image.complete) {
        return {status: "error", errorName: "ImageNotReady"};
    }
    const rect = image.getBoundingClientRect();
    return {
        status: "ok",
        x: rect.left + window.scrollX,
        y: rect.top + window.scrollY,
        width: rect.width,
        height: rect.height,
    };
})()
"""


class PonyChartResolutionError(RuntimeError):
    """Raised when a detected timed challenge remains on screen."""


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


class _PredictionResult(Protocol):
    @property
    def labels(self) -> frozenset[str]: ...


_predict: Callable[[bytes], _PredictionResult] | None = None
_generation_id: str | None = None
_publication_lock = threading.Lock()
_lifecycle_lock = threading.Lock()
_store_lock = threading.Lock()
_model_store: PonyChartGenerationStore | None = None


def _get_model_store() -> PonyChartGenerationStore:
    """Construct the default store lazily so package import stays lightweight."""

    global _model_store
    if _model_store is not None:
        return _model_store
    with _store_lock:
        if _model_store is None:
            _model_store = PonyChartGenerationStore.default()
        return _model_store


def _published_snapshot() -> tuple[
    Callable[[bytes], _PredictionResult] | None,
    str | None,
]:
    with _publication_lock:
        return _predict, _generation_id


def _publish(
    predictor: Callable[[bytes], _PredictionResult],
    generation: str,
) -> None:
    global _generation_id, _predict
    with _publication_lock:
        _predict = predictor
        _generation_id = generation


def preload_ponychart_classifier() -> None:
    """Load the classifier and ONNX model exactly once per process.

    The synchronous entry point is intentionally safe to call from multiple
    ``asyncio.to_thread`` jobs.  A failed attempt leaves the classifier
    unprepared so a later startup can retry instead of publishing partial
    state.
    """
    predictor, _ = _published_snapshot()
    if predictor is not None:
        return

    with _lifecycle_lock:
        predictor, _ = _published_snapshot()
        if predictor is not None:
            return

        loaded = _get_model_store().load_or_bootstrap()
        _publish(loaded.predictor, loaded.generation)


def refresh_ponychart_classifier() -> PonyChartRefreshOutcome:
    """Refresh and atomically publish one immutable classifier generation.

    ``CURRENT`` is returned only after a successful remote metadata check (or
    a byte-identical generation commit). Transport, validation, and commit
    failures raise and leave the published predictor-generation pair intact.
    """

    with _lifecycle_lock:
        _, published_generation = _published_snapshot()
        result = _get_model_store().refresh(published_generation)
        if result.loaded is not None:
            _publish(result.loaded.predictor, result.loaded.generation)
        return result.outcome


class PonyChart:
    def __init__(
        self,
        driver: HVDriver,
        *,
        image_directory: Path | None = None,
    ) -> None:
        self.hvdriver = driver
        self._image_directory = image_directory

    @property
    def page(self) -> Any:
        return self.hvdriver.page

    async def _wait_for_image_loaded(
        self, timeout: float = 10.0, stable_duration: float = 1.0
    ) -> None:
        """等待 PonyChart 圖片完全下載並穩定。

        PonyChart 頁面會先載入小 placeholder 再換成真正的圖片，
        因此必須等 ``src`` 和自然尺寸持續一段時間都沒有變化。尺寸只需是
        有限正數；來源圖片可以採用任何實際尺寸。
        """
        get_state_js = (
            "(() => {"
            " const div = document.getElementById('riddleimage');"
            " if (!div) return null;"
            " const img = div.querySelector('img');"
            " if (!img || !img.complete) return null;"
            " return {"
            "  src: img.currentSrc || img.src,"
            "  w: img.naturalWidth,"
            "  h: img.naturalHeight"
            " };"
            "})()"
        )
        deadline = asyncio.get_event_loop().time() + timeout
        last_state: tuple[object, float, float] | None = None
        stable_since: float | None = None
        while asyncio.get_event_loop().time() < deadline:
            remaining = max(0.05, deadline - asyncio.get_event_loop().time())
            state = await wait_for_zendriver(
                self.page.evaluate(get_state_js),
                timeout=min(2.0, remaining),
                owner=self.page,
            )
            try:
                source = state.get("src")
                if not isinstance(source, str) or not source:
                    raise ValueError("PonyChart image source is missing")
                current_state = (
                    source,
                    _positive_finite_number(state.get("w")),
                    _positive_finite_number(state.get("h")),
                )
            except AttributeError, ValueError:
                current_state = None
            if current_state is not None:
                if current_state == last_state:
                    if (
                        stable_since is not None
                        and asyncio.get_event_loop().time() - stable_since
                        >= stable_duration
                    ):
                        return
                else:
                    last_state = current_state
                    stable_since = asyncio.get_event_loop().time()
            else:
                last_state = None
                stable_since = None
            await asyncio.sleep(0.1)
        raise TimeoutError("PonyChart image did not finish loading in time")

    async def _capture_pony_chart_image(self) -> bytes:
        """Capture and validate one challenge entirely in memory."""
        await self._wait_for_image_loaded()

        canvas_result = await wait_for_zendriver(
            self.page.evaluate(_CANVAS_CAPTURE_JS),
            timeout=_PONYCHART_DOM_CAPTURE_TIMEOUT_SECONDS,
            owner=self.page,
        )
        if not isinstance(canvas_result, dict):
            raise ValueError("PonyChart canvas returned an invalid payload")

        status = canvas_result.get("status")
        if status == "ok":
            reported_width = _positive_finite_number(canvas_result.get("width"))
            reported_height = _positive_finite_number(canvas_result.get("height"))
            image, png_dimensions = _decode_png_data_url(canvas_result.get("dataUrl"))
            if png_dimensions != (reported_width, reported_height):
                raise ValueError(
                    "PonyChart canvas dimensions did not match the PNG header"
                )
        elif status == "security-error":
            image = await self._capture_pony_chart_screenshot()
        else:
            error_name = canvas_result.get("errorName")
            raise ValueError(f"PonyChart canvas capture failed: {error_name!r}")

        return image

    async def _capture_pony_chart_screenshot(self) -> bytes:
        """Fallback for a canvas tainted synchronously by cross-origin pixels."""

        rect = await wait_for_zendriver(
            self.page.evaluate(_PONYCHART_IMAGE_RECT_JS),
            timeout=_PONYCHART_DOM_CAPTURE_TIMEOUT_SECONDS,
            owner=self.page,
        )
        if not isinstance(rect, dict) or rect.get("status") != "ok":
            raise ValueError("PonyChart image did not expose a capturable rectangle")
        x = _finite_number(rect.get("x"))
        y = _finite_number(rect.get("y"))
        width = _positive_finite_number(rect.get("width"))
        height = _positive_finite_number(rect.get("height"))
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
            timeout=_PONYCHART_SCREENSHOT_TIMEOUT_SECONDS,
            owner=self.page,
        )
        image, _ = _decode_png_base64(encoded)
        return image

    @staticmethod
    def _write_retained_capture(image: bytes, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        destination: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f"pony_chart_{timestamp}_",
                suffix=".png",
                dir=directory,
                delete=False,
            ) as temporary:
                destination = Path(temporary.name)
                temporary.write(image)
        except BaseException:
            if destination is not None:
                destination.unlink(missing_ok=True)
            raise

    async def _retain_pony_chart_image(self, image: bytes) -> None:
        """Best-effort, non-blocking persistence of the captured challenge.

        Runs after answering so a slow or unavailable retention directory
        never delays or blocks the answer flow. Failures are logged and
        swallowed rather than propagated.
        """
        directory = self._image_directory
        if directory is None:
            return
        try:
            await asyncio.to_thread(self._write_retained_capture, image, directory)
        except OSError as error:
            logger.warning(
                "PonyChart image retention write failed error_type=%s",
                type(error).__name__,
            )

    async def _auto_answer(self, image: bytes) -> frozenset[str] | None:
        """模型推論後依角色名稱比對 label 文字並點擊。"""
        predictor, _ = _published_snapshot()
        if predictor is None:
            raise RuntimeError(
                "PonyChart classifier was not preloaded before battle startup"
            )
        result = await asyncio.to_thread(predictor, image)
        labels: frozenset[str] = result.labels
        ordered_labels = tuple(
            sorted(labels, key=lambda label: (label.casefold(), label))
        )
        label_elements = await wait_for_zendriver(
            self.page.select_all("label.lc", timeout=2),
            timeout=2.0 + _SELECTOR_OUTER_TIMEOUT_MARGIN_SECONDS,
            owner=self.page,
        )
        norm_map = {}
        for lab in label_elements:
            txt = lab.text.strip()
            if txt:
                norm_map[txt.lower()] = lab
        clicked: list[str] = []
        for name in ordered_labels:
            _lab = norm_map.get(name.lower().strip())
            if _lab is None:
                logger.warning(
                    "PonyChart predicted label was not clicked "
                    "label=%r reason=not-found",
                    name,
                )
                continue
            try:
                await wait_for_zendriver(
                    _lab.click(),
                    timeout=_PONYCHART_MUTATION_TIMEOUT_SECONDS,
                    owner=_lab,
                )
                clicked.append(name)
            except ZendriverOperationTimeout:
                # The label may have been selected after our local watchdog
                # expired.  Do not issue any more challenge mutations while
                # that click is still live on the old browser generation.
                raise
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                raise BattleInterruptedError(
                    "PonyChart label click outcome is unknown",
                    diagnostic_code="battle.ponychart.label-click-outcome-unknown",
                ) from error
        logger.debug(
            "PonyChart prediction labels=%s clicked_labels=%s",
            ordered_labels,
            tuple(sorted(clicked, key=lambda label: (label.casefold(), label))),
        )
        return labels

    async def _check(self) -> bool:
        elements = await wait_for_zendriver(
            self.page.query_selector_all("#riddlesubmit"),
            timeout=3.0,
            owner=self.page,
        )
        return bool(elements)

    async def is_present(self) -> bool:
        """Inspect challenge presence without answering or clicking it."""
        return await self._check()

    async def check(self) -> bool:
        isponychart: bool = await self._check()
        if not isponychart:
            return isponychart

        image = await self._capture_pony_chart_image()

        try:
            if not self.hvdriver.headless:
                notify("PonyChart", "PonyChart detected")

            try:
                await self._auto_answer(image)
            except BattleInterruptedError:
                raise
            except ZendriverOperationTimeout:
                # A timed-out label click has an unknown remote outcome and
                # must be contained by restarting this browser generation.
                raise
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                logger.warning(
                    "PonyChart auto-answer failed; challenge handling will continue "
                    "error_type=%s image_bytes=%d",
                    type(error).__name__,
                    len(image),
                )
                logger.debug(
                    "PonyChart auto-answer error detail",
                    exc_info=True,
                )

            wait_seconds = 10
            waitlimit = wait_seconds
            while waitlimit > 0 and await self._check():
                await asyncio.sleep(1)
                waitlimit -= 1

            if waitlimit <= 1 and await self._check():
                logger.warning(
                    "PonyChart remained present after %ds; "
                    "attempting fallback submission",
                    wait_seconds,
                )
                clicked = False
                xpath_error_type: str | None = None
                selector_error_type: str | None = None
                try:
                    submit_elements = await wait_for_zendriver(
                        self.hvdriver.page.xpath(
                            "//input[@type='submit' and @value='Submit Answer']",
                            timeout=2,
                        ),
                        timeout=2.0 + _SELECTOR_OUTER_TIMEOUT_MARGIN_SECONDS,
                        owner=self.page,
                    )
                except Exception as error:
                    if is_browser_generation_error(error):
                        raise
                    submit_elements = ()
                    xpath_error_type = type(error).__name__
                    logger.debug(
                        "PonyChart XPath fallback lookup error detail",
                        exc_info=True,
                    )

                if submit_elements:
                    submit = submit_elements[0]
                    try:
                        await wait_for_zendriver(
                            submit.click(),
                            timeout=_PONYCHART_MUTATION_TIMEOUT_SECONDS,
                            owner=submit,
                        )
                    except Exception as error:
                        if is_browser_generation_error(error):
                            raise
                        raise BattleInterruptedError(
                            "PonyChart submit click outcome is unknown",
                            diagnostic_code="battle.ponychart.submit-outcome-unknown",
                        ) from error
                    clicked = True

                if not clicked:
                    try:
                        riddle_submit = await wait_for_zendriver(
                            self.hvdriver.page.select("#riddlesubmit", timeout=2),
                            timeout=2.0 + _SELECTOR_OUTER_TIMEOUT_MARGIN_SECONDS,
                            owner=self.page,
                        )
                    except Exception as error:
                        if is_browser_generation_error(error):
                            raise
                        selector_error_type = type(error).__name__
                        logger.debug(
                            "PonyChart selector fallback lookup error detail",
                            exc_info=True,
                        )
                    else:
                        try:
                            await wait_for_zendriver(
                                riddle_submit.click(),
                                timeout=_PONYCHART_MUTATION_TIMEOUT_SECONDS,
                                owner=riddle_submit,
                            )
                        except Exception as error:
                            if is_browser_generation_error(error):
                                raise
                            raise BattleInterruptedError(
                                "PonyChart submit click outcome is unknown",
                                diagnostic_code=(
                                    "battle.ponychart.submit-outcome-unknown"
                                ),
                            ) from error
                        clicked = True

                await asyncio.sleep(1)
                if await self._check():
                    logger.warning(
                        "PonyChart remained present after fallback submission "
                        "attempt clicked=%s xpath_error_type=%s "
                        "selector_error_type=%s image_bytes=%d",
                        clicked,
                        xpath_error_type or "none",
                        selector_error_type or "none",
                        len(image),
                    )
                    raise PonyChartResolutionError(
                        "PonyChart remained present after fallback submission"
                    )

                if xpath_error_type is not None or selector_error_type is not None:
                    logger.warning(
                        "PonyChart fallback submission recovered after lookup error "
                        "clicked=%s xpath_error_type=%s selector_error_type=%s",
                        clicked,
                        xpath_error_type or "none",
                        selector_error_type or "none",
                    )
                else:
                    logger.debug(
                        "PonyChart challenge absent after fallback submission "
                        "attempt clicked=%s",
                        clicked,
                    )
                return isponychart

            await asyncio.sleep(1)

            return isponychart
        finally:
            await self._retain_pony_chart_image(image)

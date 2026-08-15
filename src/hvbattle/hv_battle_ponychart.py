import asyncio
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from hvbrowser import HVDriver
from hvbrowser.runtime import is_connection_error, notify, setup_logger

from .ponychart_model_store import (
    PonyChartGenerationStore,
    PonyChartRefreshOutcome,
)

logger = setup_logger(__name__)


class PonyChartResolutionError(RuntimeError):
    """Raised when a detected timed challenge remains on screen."""


class _PredictionResult(Protocol):
    @property
    def labels(self) -> frozenset[str]: ...


_predict: Callable[[str], _PredictionResult] | None = None
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
    Callable[[str], _PredictionResult] | None,
    str | None,
]:
    with _publication_lock:
        return _predict, _generation_id


def _publish(
    predictor: Callable[[str], _PredictionResult],
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
        self, timeout: float = 10.0, min_size: int = 50, stable_checks: int = 3
    ) -> None:
        """等待 PonyChart 圖片完全下載並穩定。

        PonyChart 頁面會先載入小 placeholder 再換成真正的圖片，
        所以光檢查 naturalWidth > 0 不夠，必須：
        1. 尺寸超過 min_size（避開 4x4 之類的 placeholder）
        2. src 連續 stable_checks 次都沒變（避開換圖瞬間）
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
        last_src: str | None = None
        stable_count = 0
        while asyncio.get_event_loop().time() < deadline:
            state = await self.page.evaluate(get_state_js)
            if (
                state
                and state.get("w", 0) >= min_size
                and state.get("h", 0) >= min_size
            ):
                src = state.get("src")
                if src == last_src:
                    stable_count += 1
                    if stable_count >= stable_checks:
                        return
                else:
                    last_src = src
                    stable_count = 1
            else:
                last_src = None
                stable_count = 0
            await asyncio.sleep(0.1)
        raise TimeoutError("PonyChart image did not finish loading in time")

    async def _save_pony_chart_image(self) -> str:
        """Capture one challenge for classification and optional retention."""
        await self._wait_for_image_loaded()

        riddleimage_div = await self.page.select("#riddleimage")
        img_element = await riddleimage_div.query_selector("img")
        img_src = await img_element.apply("(el) => el.src || ''")

        if not img_src:
            raise ValueError("無法獲取圖片 src")

        directory = self._image_directory
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        with tempfile.NamedTemporaryFile(
            prefix=(
                f"pony_chart_{timestamp}_"
                if directory is not None
                else "hvbattle-ponychart-"
            ),
            suffix=".png",
            dir=directory,
            delete=False,
        ) as temporary:
            filepath = Path(temporary.name)
        try:
            await img_element.save_screenshot(str(filepath))
        except BaseException:
            filepath.unlink(missing_ok=True)
            raise
        return str(filepath)

    async def _auto_answer(self, img_path: str) -> frozenset[str] | None:
        """模型推論後依角色名稱比對 label 文字並點擊。"""
        predictor, _ = _published_snapshot()
        if predictor is None:
            raise RuntimeError(
                "PonyChart classifier was not preloaded before battle startup"
            )
        result = await asyncio.to_thread(predictor, img_path)
        labels: frozenset[str] = result.labels
        ordered_labels = tuple(
            sorted(labels, key=lambda label: (label.casefold(), label))
        )
        label_elements = await self.page.select_all("label.lc", timeout=2)
        norm_map = {}
        for lab in label_elements:
            txt = lab.text.strip()
            if txt:
                norm_map[txt.lower()] = lab
        clicked: list[str] = []
        for name in labels:
            _lab = norm_map.get(name.lower().strip())
            if _lab is None:
                logger.warning(
                    "PonyChart predicted label was not clicked "
                    "label=%r reason=not-found",
                    name,
                )
                continue
            try:
                await _lab.click()
                clicked.append(name)
            except Exception as error:
                if is_connection_error(error):
                    raise
                logger.warning(
                    "PonyChart predicted label was not clicked "
                    "label=%r reason=click-error error_type=%s",
                    name,
                    type(error).__name__,
                )
                logger.debug(
                    "PonyChart label click error detail: label=%r",
                    name,
                    exc_info=True,
                )
        logger.debug(
            "PonyChart prediction labels=%s clicked_labels=%s",
            ordered_labels,
            tuple(sorted(clicked, key=lambda label: (label.casefold(), label))),
        )
        return labels

    async def _check(self) -> bool:
        elements = await self.page.query_selector_all("#riddlesubmit")
        return bool(elements)

    async def is_present(self) -> bool:
        """Inspect challenge presence without answering or clicking it."""
        return await self._check()

    async def check(self) -> bool:
        isponychart: bool = await self._check()
        if not isponychart:
            return isponychart

        img_path = await self._save_pony_chart_image()

        try:
            if not self.hvdriver.headless:
                notify("PonyChart", "PonyChart detected")

            try:
                await self._auto_answer(img_path)
            except Exception as error:
                if is_connection_error(error):
                    raise
                logger.warning(
                    "PonyChart auto-answer failed; challenge handling will continue "
                    "error_type=%s image=%s",
                    type(error).__name__,
                    img_path if self._image_directory is not None else None,
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
                    submit_elements = await self.hvdriver.page.xpath(
                        "//input[@type='submit' and @value='Submit Answer']",
                        timeout=2,
                    )
                    if submit_elements:
                        await submit_elements[0].click()
                        clicked = True
                except Exception as error:
                    if is_connection_error(error):
                        raise
                    xpath_error_type = type(error).__name__
                    logger.debug(
                        "PonyChart XPath fallback click error detail",
                        exc_info=True,
                    )

                if not clicked:
                    try:
                        riddle_submit = await self.hvdriver.page.select("#riddlesubmit")
                        await riddle_submit.click()
                        clicked = True
                    except Exception as error:
                        if is_connection_error(error):
                            raise
                        selector_error_type = type(error).__name__
                        logger.debug(
                            "PonyChart selector fallback click error detail",
                            exc_info=True,
                        )

                await asyncio.sleep(1)
                if await self._check():
                    logger.warning(
                        "PonyChart remained present after fallback submission "
                        "attempt clicked=%s xpath_error_type=%s "
                        "selector_error_type=%s image=%s",
                        clicked,
                        xpath_error_type or "none",
                        selector_error_type or "none",
                        img_path if self._image_directory is not None else None,
                    )
                    raise PonyChartResolutionError(
                        "PonyChart remained present after fallback submission"
                    )

                if xpath_error_type is not None or selector_error_type is not None:
                    logger.warning(
                        "PonyChart fallback submission recovered after click error "
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
            if self._image_directory is None:
                Path(img_path).unlink(missing_ok=True)

import asyncio
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from hvbrowser import HVDriver
from hvbrowser.runtime import is_connection_error, notify, setup_logger

logger = setup_logger(__name__)


class PonyChartResolutionError(RuntimeError):
    """Raised when a detected timed challenge remains on screen."""


class _PredictionResult(Protocol):
    @property
    def labels(self) -> frozenset[str]: ...


_predict: Callable[[str], _PredictionResult] | None = None


def preload_ponychart_classifier() -> None:
    """Load the classifier and ONNX model before a timed challenge appears."""
    global _predict
    if _predict is not None:
        return

    from ponychart_classifier import predict, preload

    preload()

    def predict_default(img_path: str) -> _PredictionResult:
        return predict(img_path)

    _predict = predict_default


class PonyChart:
    def __init__(
        self,
        driver: HVDriver,
        *,
        diagnostic_directory: Path | None = None,
        diagnostic_file_limit: int = 20,
    ) -> None:
        if diagnostic_file_limit < 1:
            raise ValueError("diagnostic_file_limit must be at least 1")
        self.hvdriver = driver
        self._diagnostic_directory = diagnostic_directory
        self._diagnostic_file_limit = diagnostic_file_limit

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
        """Capture one challenge in a temporary file for classifier input."""
        await self._wait_for_image_loaded()

        riddleimage_div = await self.page.select("#riddleimage")
        img_element = await riddleimage_div.query_selector("img")
        img_src = await img_element.apply("(el) => el.src || ''")

        if not img_src:
            raise ValueError("無法獲取圖片 src")

        with tempfile.NamedTemporaryFile(
            prefix="hvbattle-ponychart-",
            suffix=".png",
            delete=False,
        ) as temporary:
            filepath = Path(temporary.name)
        try:
            await img_element.save_screenshot(str(filepath))
        except BaseException:
            filepath.unlink(missing_ok=True)
            raise
        return str(filepath)

    def _retain_diagnostic(self, img_path: str) -> Path | None:
        """Retain a bounded failure artifact only when explicitly configured."""
        if self._diagnostic_directory is None:
            return None

        try:
            directory = self._diagnostic_directory
            directory.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            destination = directory / f"pony_chart_failure_{timestamp}.png"
            shutil.copyfile(img_path, destination)

            diagnostics = sorted(
                (
                    candidate
                    for candidate in directory.glob("pony_chart_failure_*.png")
                    if candidate.is_file()
                ),
                key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name),
            )
            for obsolete in diagnostics[: -self._diagnostic_file_limit]:
                obsolete.unlink()
        except OSError as error:
            logger.warning("Unable to retain PonyChart diagnostic: %r", error)
            return None
        return destination

    async def _auto_answer(self, img_path: str) -> frozenset[str] | None:
        """模型推論後依角色名稱比對 label 文字並點擊。"""
        if _predict is None:
            raise RuntimeError(
                "PonyChart classifier was not preloaded before battle startup"
            )
        result = _predict(img_path)
        labels: frozenset[str] = result.labels
        label_elements = await self.page.select_all("label.lc", timeout=2)
        norm_map = {}
        for lab in label_elements:
            txt = lab.text.strip()
            if txt:
                norm_map[txt.lower()] = lab
        clicked = []
        for name in labels:
            _lab = norm_map.get(name.lower().strip())
            if _lab is None:
                continue
            try:
                await _lab.click()
                clicked.append(name)
            except Exception as e:
                if is_connection_error(e):
                    raise
        logger.info(f"[PonyChart][ML] Prediction: {labels} -> Clicked text: {clicked}")
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
        diagnostic_retained: Path | None = None
        diagnostic_attempted = False

        def retain_diagnostic_once() -> Path | None:
            nonlocal diagnostic_attempted, diagnostic_retained
            if not diagnostic_attempted:
                diagnostic_attempted = True
                diagnostic_retained = self._retain_diagnostic(img_path)
            return diagnostic_retained

        try:
            if not self.hvdriver.headless:
                notify("PonyChart", "PonyChart detected")

            try:
                await self._auto_answer(img_path)
            except Exception as e:
                if is_connection_error(e):
                    raise
                retained = retain_diagnostic_once()
                logger.error(
                    "[PonyChart] Auto-check failed: %r diagnostic=%s",
                    e,
                    retained,
                )

            wait_seconds = 10
            waitlimit = wait_seconds
            while waitlimit > 0 and await self._check():
                await asyncio.sleep(1)
                waitlimit -= 1

            if waitlimit <= 1 and await self._check():
                logger.info(
                    "[PonyChart] Auto-answer did not trigger submission within "
                    "%ds, attempting fallback submit",
                    wait_seconds,
                )
                clicked = False
                try:
                    submit_elements = await self.hvdriver.page.xpath(
                        "//input[@type='submit' and @value='Submit Answer']",
                        timeout=2,
                    )
                    if submit_elements:
                        await submit_elements[0].click()
                        clicked = True
                except Exception as e:
                    if is_connection_error(e):
                        raise

                if not clicked:
                    try:
                        riddle_submit = await self.hvdriver.page.select("#riddlesubmit")
                        await riddle_submit.click()
                        clicked = True
                    except Exception as e2:
                        if is_connection_error(e2):
                            raise

                await asyncio.sleep(1)
                if await self._check():
                    retained = retain_diagnostic_once()
                    logger.warning(
                        "[PonyChart] Fallback submit click did not dismiss riddle "
                        "(clicked=%s); likely auto-failed by game timeout; "
                        "diagnostic=%s",
                        clicked,
                        retained,
                    )
                    raise PonyChartResolutionError(
                        "PonyChart remained present after fallback submission"
                    )

                logger.info(
                    "[PonyChart] Fallback submit click dismissed riddle "
                    "(clicked=%s)",
                    clicked,
                )
                return isponychart

            await asyncio.sleep(1)

            return isponychart
        finally:
            Path(img_path).unlink(missing_ok=True)

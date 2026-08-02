import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import hvbattle.hv_battle_ponychart as ponychart_module
from hvbattle.hv_battle_ponychart import PonyChart


class PonyChartArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_resolution_removes_classifier_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "challenge.png"
            image.write_bytes(b"challenge")
            driver = Mock(headless=True)
            challenge = PonyChart(driver)
            challenge._check = AsyncMock(side_effect=[True, False])
            challenge._save_pony_chart_image = AsyncMock(return_value=str(image))
            challenge._auto_answer = AsyncMock(return_value=frozenset({"Twilight"}))

            with patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()):
                detected = await challenge.check()

            self.assertTrue(detected)
            self.assertFalse(image.exists())

    async def test_prediction_failure_retains_only_explicit_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "challenge.png"
            image.write_bytes(b"challenge")
            diagnostics = root / "diagnostics"
            driver = Mock(headless=True)
            challenge = PonyChart(driver, diagnostic_directory=diagnostics)
            challenge._check = AsyncMock(side_effect=[True, False])
            challenge._save_pony_chart_image = AsyncMock(return_value=str(image))
            challenge._auto_answer = AsyncMock(side_effect=ValueError("bad model"))

            with patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()):
                detected = await challenge.check()

            self.assertTrue(detected)
            self.assertFalse(image.exists())
            retained = tuple(diagnostics.glob("pony_chart_failure_*.png"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), b"challenge")

    async def test_capture_failure_removes_partial_temporary_file(self) -> None:
        driver = Mock(headless=True)
        driver.page = Mock()
        container = Mock()
        image_element = Mock()
        selected_path: Path | None = None

        async def fail_capture(path: str) -> None:
            nonlocal selected_path
            selected_path = Path(path)
            selected_path.write_bytes(b"partial")
            raise RuntimeError("capture failed")

        image_element.apply = AsyncMock(return_value="https://example.test/pony.png")
        image_element.save_screenshot = AsyncMock(side_effect=fail_capture)
        container.query_selector = AsyncMock(return_value=image_element)
        driver.page.select = AsyncMock(return_value=container)
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            await challenge._save_pony_chart_image()

        self.assertIsNotNone(selected_path)
        assert selected_path is not None
        self.assertFalse(selected_path.exists())


if __name__ == "__main__":
    unittest.main()

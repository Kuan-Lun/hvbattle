import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

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
            answer_error = ValueError("bad model")
            challenge._auto_answer = AsyncMock(side_effect=answer_error)

            with (
                patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
                patch.object(ponychart_module, "logger") as ponychart_logger,
            ):
                detected = await challenge.check()

            self.assertTrue(detected)
            self.assertFalse(image.exists())
            retained = tuple(diagnostics.glob("pony_chart_failure_*.png"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), b"challenge")
            ponychart_logger.warning.assert_called_once_with(
                "PonyChart auto-answer failed; challenge handling will continue "
                "error_type=%s diagnostic=%s",
                "ValueError",
                retained[0],
            )
            ponychart_logger.debug.assert_called_once_with(
                "PonyChart auto-answer error detail",
                exc_info=True,
            )
            ponychart_logger.error.assert_not_called()
            ponychart_logger.info.assert_not_called()

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


class PonyChartLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_prediction_details_are_sorted_debug_fields(self) -> None:
        driver = Mock()
        driver.page = Mock()
        alpha = Mock(text="alpha")
        alpha.click = AsyncMock()
        zeta = Mock(text="Zeta")
        zeta.click = AsyncMock()
        driver.page.select_all = AsyncMock(return_value=[zeta, alpha])
        predictor = Mock(return_value=Mock(labels=frozenset({"Zeta", "alpha"})))
        challenge = PonyChart(driver)

        with (
            patch.object(ponychart_module, "_predict", predictor),
            patch.object(ponychart_module, "logger") as ponychart_logger,
        ):
            labels = await challenge._auto_answer("challenge.png")

        self.assertEqual(labels, frozenset({"Zeta", "alpha"}))
        alpha.click.assert_awaited_once_with()
        zeta.click.assert_awaited_once_with()
        ponychart_logger.debug.assert_called_once_with(
            "PonyChart prediction labels=%s clicked_labels=%s",
            ("alpha", "Zeta"),
            ("alpha", "Zeta"),
        )
        ponychart_logger.info.assert_not_called()
        ponychart_logger.warning.assert_not_called()

    async def test_predicted_label_not_found_warns(self) -> None:
        driver = Mock()
        driver.page = Mock()
        twilight = Mock(text="Twilight Sparkle")
        twilight.click = AsyncMock()
        driver.page.select_all = AsyncMock(return_value=[twilight])
        predictor = Mock(
            return_value=Mock(labels=frozenset({"Twilight Sparkle", "Rainbow Dash"}))
        )
        challenge = PonyChart(driver)

        with (
            patch.object(ponychart_module, "_predict", predictor),
            patch.object(ponychart_module, "logger") as ponychart_logger,
        ):
            await challenge._auto_answer("challenge.png")

        twilight.click.assert_awaited_once_with()
        ponychart_logger.warning.assert_called_once_with(
            "PonyChart predicted label was not clicked " "label=%r reason=not-found",
            "Rainbow Dash",
        )
        ponychart_logger.debug.assert_called_once_with(
            "PonyChart prediction labels=%s clicked_labels=%s",
            ("Rainbow Dash", "Twilight Sparkle"),
            ("Twilight Sparkle",),
        )

    async def test_predicted_label_click_error_warns(self) -> None:
        driver = Mock()
        driver.page = Mock()
        click_error = RuntimeError("label detached")
        twilight = Mock(text="Twilight Sparkle")
        twilight.click = AsyncMock(side_effect=click_error)
        driver.page.select_all = AsyncMock(return_value=[twilight])
        predictor = Mock(return_value=Mock(labels=frozenset({"Twilight Sparkle"})))
        challenge = PonyChart(driver)

        with (
            patch.object(ponychart_module, "_predict", predictor),
            patch.object(ponychart_module, "is_connection_error", return_value=False),
            patch.object(ponychart_module, "logger") as ponychart_logger,
        ):
            await challenge._auto_answer("challenge.png")

        ponychart_logger.warning.assert_called_once_with(
            "PonyChart predicted label was not clicked "
            "label=%r reason=click-error error_type=%s",
            "Twilight Sparkle",
            "RuntimeError",
        )
        self.assertEqual(
            ponychart_logger.debug.call_args_list,
            [
                call(
                    "PonyChart label click error detail: label=%r",
                    "Twilight Sparkle",
                    exc_info=True,
                ),
                call(
                    "PonyChart prediction labels=%s clicked_labels=%s",
                    ("Twilight Sparkle",),
                    (),
                ),
            ],
        )

    async def test_fallback_start_warns_and_success_detail_is_debug(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "challenge.png"
            image.write_bytes(b"challenge")
            driver = Mock(headless=True)
            driver.page = Mock()
            submit = Mock()
            submit.click = AsyncMock()
            driver.page.xpath = AsyncMock(return_value=[submit])
            challenge = PonyChart(driver)
            challenge._check = AsyncMock(side_effect=[True] * 12 + [False])
            challenge._save_pony_chart_image = AsyncMock(return_value=str(image))
            challenge._auto_answer = AsyncMock(
                return_value=frozenset({"Twilight Sparkle"})
            )

            with (
                patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
                patch.object(ponychart_module, "logger") as ponychart_logger,
            ):
                detected = await challenge.check()

            self.assertTrue(detected)
            self.assertFalse(image.exists())
            submit.click.assert_awaited_once_with()
            ponychart_logger.warning.assert_called_once_with(
                "PonyChart remained present after %ds; "
                "attempting fallback submission",
                10,
            )
            ponychart_logger.debug.assert_called_once_with(
                "PonyChart challenge absent after fallback submission "
                "attempt clicked=%s",
                True,
            )
            ponychart_logger.info.assert_not_called()

    async def test_fallback_click_error_recovery_warns_once_with_safe_type(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "challenge.png"
            image.write_bytes(b"challenge")
            driver = Mock(headless=True)
            driver.page = Mock()
            fallback_submit = Mock()
            fallback_submit.click = AsyncMock()
            driver.page.xpath = AsyncMock(
                side_effect=RuntimeError("private click detail\nsecond line")
            )
            driver.page.select = AsyncMock(return_value=fallback_submit)
            challenge = PonyChart(driver)
            challenge._check = AsyncMock(side_effect=[True] * 12 + [False])
            challenge._save_pony_chart_image = AsyncMock(return_value=str(image))
            challenge._auto_answer = AsyncMock(
                return_value=frozenset({"Twilight Sparkle"})
            )

            with (
                patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
                patch.object(
                    ponychart_module,
                    "is_connection_error",
                    return_value=False,
                ),
                patch.object(ponychart_module, "logger") as ponychart_logger,
            ):
                detected = await challenge.check()

            self.assertTrue(detected)
            fallback_submit.click.assert_awaited_once_with()
            self.assertEqual(
                ponychart_logger.warning.call_args_list,
                [
                    call(
                        "PonyChart remained present after %ds; "
                        "attempting fallback submission",
                        10,
                    ),
                    call(
                        "PonyChart fallback submission recovered after click error "
                        "clicked=%s xpath_error_type=%s selector_error_type=%s",
                        True,
                        "RuntimeError",
                        "none",
                    ),
                ],
            )
            self.assertNotIn(
                "private click detail",
                " ".join(str(call_args) for call_args in ponychart_logger.mock_calls),
            )
            ponychart_logger.info.assert_not_called()


if __name__ == "__main__":
    unittest.main()

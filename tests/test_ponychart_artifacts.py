import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

import hvbattle.hv_battle_ponychart as ponychart_module
from hvbattle.hv_battle_ponychart import PonyChart


class PonyChartArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_successful_resolution_removes_classifier_input(
        self,
    ) -> None:
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

    async def test_configured_directory_retains_successful_classifier_input(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "challenge.png"
            image.write_bytes(b"challenge")
            images = root / "pony_chart"
            driver = Mock(headless=True)
            challenge = PonyChart(driver, image_directory=images)
            challenge._check = AsyncMock(side_effect=[True, False])
            challenge._save_pony_chart_image = AsyncMock(return_value=str(image))
            challenge._auto_answer = AsyncMock(return_value=frozenset({"Twilight"}))

            with patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()):
                detected = await challenge.check()

            self.assertTrue(detected)
            # The scratch capture is local-only; retention persists a copy
            # under the configured directory after answering, then the
            # scratch file is removed.
            self.assertFalse(image.exists())
            retained = list(images.iterdir())
            self.assertEqual(len(retained), 1)
            self.assertTrue(retained[0].name.startswith("pony_chart_"))
            self.assertEqual(retained[0].suffix, ".png")
            self.assertEqual(retained[0].read_bytes(), b"challenge")

    async def test_configured_directory_retains_image_after_prediction_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "challenge.png"
            image.write_bytes(b"challenge")
            images = root / "pony_chart"
            driver = Mock(headless=True)
            challenge = PonyChart(driver, image_directory=images)
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
            retained = list(images.iterdir())
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), b"challenge")
            ponychart_logger.warning.assert_called_once_with(
                "PonyChart auto-answer failed; challenge handling will continue "
                "error_type=%s image=%s",
                "ValueError",
                str(image),
            )
            ponychart_logger.debug.assert_called_once_with(
                "PonyChart auto-answer error detail",
                exc_info=True,
            )
            ponychart_logger.error.assert_not_called()
            ponychart_logger.info.assert_not_called()

    async def test_capture_uses_local_scratch_file_not_retention_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_directory = Path(directory) / "nested" / "pony_chart"
            driver = Mock(headless=True)
            driver.page = Mock()
            container = Mock()
            image_element = Mock()

            async def capture(path: str) -> None:
                Path(path).write_bytes(b"challenge")

            image_element.apply = AsyncMock(
                return_value="https://example.test/pony.png"
            )
            image_element.save_screenshot = AsyncMock(side_effect=capture)
            container.query_selector = AsyncMock(return_value=image_element)
            driver.page.select = AsyncMock(return_value=container)
            challenge = PonyChart(driver, image_directory=image_directory)
            challenge._wait_for_image_loaded = AsyncMock()

            first = Path(await challenge._save_pony_chart_image())
            second = Path(await challenge._save_pony_chart_image())
            try:
                # Capture must never touch the (possibly slow or unavailable)
                # retention directory: the bounded CDP screenshot call always
                # targets local scratch storage. With no explicit
                # scratch_directory, that falls back to the process default.
                self.assertFalse(image_directory.exists())
                self.assertNotEqual(first.parent, image_directory)
                self.assertEqual(first.parent, Path(tempfile.gettempdir()))
                self.assertNotEqual(first, second)
                self.assertTrue(first.name.startswith("hvbattle-ponychart-"))
                self.assertTrue(second.name.startswith("hvbattle-ponychart-"))
                self.assertEqual(first.suffix, ".png")
                self.assertEqual(second.suffix, ".png")
                self.assertEqual(first.read_bytes(), b"challenge")
                self.assertEqual(second.read_bytes(), b"challenge")
            finally:
                first.unlink(missing_ok=True)
                second.unlink(missing_ok=True)

    async def test_capture_honors_explicit_scratch_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scratch_directory = Path(directory) / "scratch"
            scratch_directory.mkdir()
            driver = Mock(headless=True)
            driver.page = Mock()
            container = Mock()
            image_element = Mock()

            async def capture(path: str) -> None:
                Path(path).write_bytes(b"challenge")

            image_element.apply = AsyncMock(
                return_value="https://example.test/pony.png"
            )
            image_element.save_screenshot = AsyncMock(side_effect=capture)
            container.query_selector = AsyncMock(return_value=image_element)
            driver.page.select = AsyncMock(return_value=container)
            challenge = PonyChart(driver, scratch_directory=scratch_directory)
            challenge._wait_for_image_loaded = AsyncMock()

            captured = Path(await challenge._save_pony_chart_image())

            # A deployment that cannot trust the process default temp
            # directory (e.g. its TMPDIR happens to point somewhere slow or
            # remote) can redirect capture explicitly instead.
            self.assertEqual(captured.parent, scratch_directory)
            self.assertNotEqual(captured.parent, Path(tempfile.gettempdir()))
            self.assertEqual(captured.read_bytes(), b"challenge")

    async def test_retain_pony_chart_image_creates_directory_and_unique_copies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            source.write_bytes(b"challenge")
            image_directory = root / "nested" / "pony_chart"
            driver = Mock(headless=True)
            challenge = PonyChart(driver, image_directory=image_directory)

            await challenge._retain_pony_chart_image(str(source))
            await challenge._retain_pony_chart_image(str(source))

            retained = sorted(image_directory.iterdir())
            self.assertEqual(len(retained), 2)
            self.assertNotEqual(retained[0], retained[1])
            for path in retained:
                self.assertTrue(path.name.startswith("pony_chart_"))
                self.assertEqual(path.suffix, ".png")
                self.assertEqual(path.read_bytes(), b"challenge")
            # Retention copies the source; it never deletes it (deletion of
            # the scratch capture is ``check()``'s responsibility).
            self.assertTrue(source.exists())

    async def test_retain_pony_chart_image_without_directory_is_noop(
        self,
    ) -> None:
        driver = Mock(headless=True)
        challenge = PonyChart(driver)

        await challenge._retain_pony_chart_image("/nonexistent/does-not-matter.png")

    async def test_retain_pony_chart_image_swallows_copy_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            source.write_bytes(b"challenge")
            image_directory = root / "pony_chart"
            driver = Mock(headless=True)
            challenge = PonyChart(driver, image_directory=image_directory)
            copy_error = OSError("NAS unreachable")

            with (
                patch.object(ponychart_module.shutil, "copy2", side_effect=copy_error),
                patch.object(ponychart_module, "logger") as ponychart_logger,
            ):
                await challenge._retain_pony_chart_image(str(source))

            ponychart_logger.warning.assert_called_once_with(
                "PonyChart image retention copy failed error_type=%s",
                "OSError",
            )

    async def test_capture_failure_removes_partial_scratch_file(self) -> None:
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
        with tempfile.TemporaryDirectory() as directory:
            challenge = PonyChart(driver, image_directory=Path(directory))
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

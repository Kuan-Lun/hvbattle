import asyncio
import base64
import binascii
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

from hvbrowser.runtime import ZendriverOperationTimeout

import hvbattle.hv_battle_ponychart as ponychart_module
from hvbattle.contracts import BattleInterruptedError
from hvbattle.hv_battle_ponychart import PonyChart


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)
    )


def _png_bytes(width: int, height: int) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00\x00\x00\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )


def _canvas_payload(image: bytes, width: int, height: int) -> dict[str, object]:
    encoded = base64.b64encode(image).decode("ascii")
    return {
        "status": "ok",
        "width": width,
        "height": height,
        "dataUrl": f"data:image/png;base64,{encoded}",
    }


class PonyChartArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_loading_accepts_any_stable_positive_dimensions(self) -> None:
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            return_value={"src": "challenge", "w": 1, "h": 2}
        )
        challenge = PonyChart(driver)

        with patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()):
            await challenge._wait_for_image_loaded(timeout=1.0, stable_duration=0.0)

        self.assertEqual(driver.page.evaluate.await_count, 2)

    async def test_image_loading_rejects_null_or_empty_source_until_valid(
        self,
    ) -> None:
        for invalid_source in (None, ""):
            with self.subTest(invalid_source=invalid_source):
                driver = Mock(headless=True)
                driver.page = Mock()
                invalid = {"src": invalid_source, "w": 1, "h": 2}
                valid = {"src": "challenge", "w": 1, "h": 2}
                driver.page.evaluate = AsyncMock(
                    side_effect=[invalid, invalid, valid, valid]
                )
                challenge = PonyChart(driver)

                with patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()):
                    await challenge._wait_for_image_loaded(
                        timeout=1.0,
                        stable_duration=0.0,
                    )

                self.assertEqual(driver.page.evaluate.await_count, 4)

    async def test_canvas_capture_passes_page_as_timeout_owner(self) -> None:
        image = _png_bytes(3, 4)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(return_value=_canvas_payload(image, 3, 4))
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock()
        observed_owners: list[object] = []
        original_wait = ponychart_module.wait_for_zendriver

        async def observe_owner(
            awaitable: object,
            *,
            timeout: float,
            owner: object,
        ) -> object:
            observed_owners.append(owner)
            return await original_wait(awaitable, timeout=timeout, owner=owner)

        with patch.object(
            ponychart_module,
            "wait_for_zendriver",
            side_effect=observe_owner,
        ):
            captured = await challenge._capture_pony_chart_image()

        self.assertEqual(captured, image)
        self.assertEqual(observed_owners, [driver.page])

    async def test_unconfigured_successful_resolution_stays_in_memory(
        self,
    ) -> None:
        image = b"challenge"
        driver = Mock(headless=True)
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(side_effect=[True, False])
        challenge._capture_pony_chart_image = AsyncMock(return_value=image)
        challenge._auto_answer = AsyncMock(return_value=frozenset({"Twilight"}))

        with (
            patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
            patch.object(ponychart_module.tempfile, "NamedTemporaryFile") as temporary,
        ):
            detected = await challenge.check()

        self.assertTrue(detected)
        challenge._auto_answer.assert_awaited_once_with(image)
        temporary.assert_not_called()

    async def test_configured_directory_retains_successful_classifier_input(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = b"challenge"
            images = root / "pony_chart"
            driver = Mock(headless=True)
            challenge = PonyChart(driver, image_directory=images)
            challenge._check = AsyncMock(side_effect=[True, False])
            challenge._capture_pony_chart_image = AsyncMock(return_value=image)
            challenge._auto_answer = AsyncMock(return_value=frozenset({"Twilight"}))

            with patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()):
                detected = await challenge.check()

            self.assertTrue(detected)
            challenge._auto_answer.assert_awaited_once_with(image)
            retained = list(images.iterdir())
            self.assertEqual(len(retained), 1)
            self.assertTrue(retained[0].name.startswith("pony_chart_"))
            self.assertEqual(retained[0].suffix, ".png")
            self.assertEqual(retained[0].read_bytes(), image)

    async def test_configured_directory_retains_image_after_prediction_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = b"challenge"
            images = root / "pony_chart"
            driver = Mock(headless=True)
            challenge = PonyChart(driver, image_directory=images)
            challenge._check = AsyncMock(side_effect=[True, False])
            challenge._capture_pony_chart_image = AsyncMock(return_value=image)
            answer_error = ValueError("bad model")
            challenge._auto_answer = AsyncMock(side_effect=answer_error)

            with (
                patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
                patch.object(ponychart_module, "logger") as ponychart_logger,
            ):
                detected = await challenge.check()

            self.assertTrue(detected)
            retained = list(images.iterdir())
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), image)
            ponychart_logger.warning.assert_called_once_with(
                "PonyChart auto-answer failed; challenge handling will continue "
                "error_type=%s image_bytes=%d",
                "ValueError",
                len(image),
            )
            ponychart_logger.debug.assert_called_once_with(
                "PonyChart auto-answer error detail",
                exc_info=True,
            )
            ponychart_logger.error.assert_not_called()
            ponychart_logger.info.assert_not_called()

    async def test_capture_returns_bytes_without_touching_retention_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_directory = Path(directory) / "nested" / "pony_chart"
            image = _png_bytes(13, 7)
            driver = Mock(headless=True)
            driver.page = Mock()
            driver.page.evaluate = AsyncMock(return_value=_canvas_payload(image, 13, 7))
            driver.page.send = AsyncMock()
            challenge = PonyChart(driver, image_directory=image_directory)
            challenge._wait_for_image_loaded = AsyncMock()

            with patch.object(
                ponychart_module.tempfile,
                "NamedTemporaryFile",
            ) as temporary:
                first = await challenge._capture_pony_chart_image()
                second = await challenge._capture_pony_chart_image()

            self.assertFalse(image_directory.exists())
            self.assertEqual(first, image)
            self.assertEqual(second, image)
            self.assertEqual(driver.page.evaluate.await_count, 2)
            driver.page.send.assert_not_awaited()
            temporary.assert_not_called()

    async def test_retain_pony_chart_image_creates_unique_direct_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = b"challenge"
            image_directory = root / "nested" / "pony_chart"
            driver = Mock(headless=True)
            challenge = PonyChart(driver, image_directory=image_directory)

            await challenge._retain_pony_chart_image(image)
            await challenge._retain_pony_chart_image(image)

            retained = sorted(image_directory.iterdir())
            self.assertEqual(len(retained), 2)
            self.assertNotEqual(retained[0], retained[1])
            for path in retained:
                self.assertTrue(path.name.startswith("pony_chart_"))
                self.assertEqual(path.suffix, ".png")
                self.assertEqual(path.read_bytes(), image)

    async def test_retain_pony_chart_image_without_directory_is_noop(
        self,
    ) -> None:
        driver = Mock(headless=True)
        challenge = PonyChart(driver)

        with patch.object(challenge, "_write_retained_capture") as write:
            await challenge._retain_pony_chart_image(b"challenge")

        write.assert_not_called()

    async def test_retain_pony_chart_image_swallows_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = b"challenge"
            image_directory = root / "pony_chart"
            driver = Mock(headless=True)
            challenge = PonyChart(driver, image_directory=image_directory)
            write_error = OSError("NAS unreachable")

            with (
                patch.object(
                    challenge,
                    "_write_retained_capture",
                    side_effect=write_error,
                ),
                patch.object(ponychart_module, "logger") as ponychart_logger,
            ):
                await challenge._retain_pony_chart_image(image)

            ponychart_logger.warning.assert_called_once_with(
                "PonyChart image retention write failed error_type=%s",
                "OSError",
            )

    async def test_canvas_security_error_uses_one_shot_screenshot_fallback(
        self,
    ) -> None:
        image = _png_bytes(17, 5)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            side_effect=[
                {"status": "security-error", "errorName": "SecurityError"},
                {
                    "status": "ok",
                    "x": 0.0,
                    "y": 12.5,
                    "width": 987.25,
                    "height": 543.75,
                },
            ]
        )
        driver.page.send = AsyncMock(
            return_value=base64.b64encode(image).decode("ascii")
        )
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock()

        captured = await challenge._capture_pony_chart_image()

        self.assertEqual(captured, image)
        self.assertEqual(driver.page.evaluate.await_count, 2)
        driver.page.send.assert_awaited_once()

    async def test_invalid_canvas_base64_or_png_is_rejected_in_memory(
        self,
    ) -> None:
        header = struct.pack(">IIBBBBB", 3, 4, 8, 6, 0, 0, 0)
        header_only_png = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
        bad_checksum_png = bytearray(_png_bytes(3, 4))
        bad_checksum_png[-1] ^= 1
        cases = {
            "base64": {
                "status": "ok",
                "width": 3,
                "height": 4,
                "dataUrl": "data:image/png;base64,not%%%base64",
            },
            "png": {
                "status": "ok",
                "width": 3,
                "height": 4,
                "dataUrl": (
                    "data:image/png;base64,"
                    + base64.b64encode(b"not a PNG").decode("ascii")
                ),
            },
            "truncated-png": _canvas_payload(header_only_png, 3, 4),
            "bad-png-checksum": _canvas_payload(bytes(bad_checksum_png), 3, 4),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                driver = Mock(headless=True)
                driver.page = Mock()
                driver.page.evaluate = AsyncMock(return_value=payload)
                driver.page.send = AsyncMock()
                challenge = PonyChart(driver)
                challenge._wait_for_image_loaded = AsyncMock()

                with self.assertRaises(ValueError):
                    await challenge._capture_pony_chart_image()

                driver.page.send.assert_not_awaited()

    async def test_canvas_header_dimension_mismatch_is_rejected_in_memory(
        self,
    ) -> None:
        image = _png_bytes(11, 9)
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(return_value=_canvas_payload(image, 11, 10))
        driver.page.send = AsyncMock()
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock()

        with self.assertRaisesRegex(ValueError, "dimensions did not match"):
            await challenge._capture_pony_chart_image()

        driver.page.send.assert_not_awaited()

    async def test_canvas_timeout_does_not_fallback_or_retry(self) -> None:
        driver = Mock(headless=True)
        driver.page = Mock()

        async def hang(_script: str) -> object:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        driver.page.evaluate = AsyncMock(side_effect=hang)
        driver.page.send = AsyncMock()
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock()

        with (
            patch.object(
                ponychart_module,
                "_PONYCHART_DOM_CAPTURE_TIMEOUT_SECONDS",
                0.01,
            ),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await challenge._capture_pony_chart_image()

        driver.page.evaluate.assert_awaited_once()
        driver.page.send.assert_not_awaited()

    async def test_screenshot_timeout_does_not_retry(self) -> None:
        driver = Mock(headless=True)
        driver.page = Mock()
        driver.page.evaluate = AsyncMock(
            side_effect=[
                {"status": "security-error", "errorName": "SecurityError"},
                {
                    "status": "ok",
                    "x": 0,
                    "y": 0,
                    "width": 9,
                    "height": 8,
                },
            ]
        )

        async def hang(_command: object) -> object:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        driver.page.send = AsyncMock(side_effect=hang)
        challenge = PonyChart(driver)
        challenge._wait_for_image_loaded = AsyncMock()

        with (
            patch.object(
                ponychart_module,
                "_PONYCHART_SCREENSHOT_TIMEOUT_SECONDS",
                0.01,
            ),
            self.assertRaises(ZendriverOperationTimeout),
        ):
            await challenge._capture_pony_chart_image()

        self.assertEqual(driver.page.evaluate.await_count, 2)
        driver.page.send.assert_awaited_once()


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
            labels = await challenge._auto_answer(b"challenge")

        self.assertEqual(labels, frozenset({"Zeta", "alpha"}))
        predictor.assert_called_once_with(b"challenge")
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
            await challenge._auto_answer(b"challenge")

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

    async def test_predicted_label_click_error_stops_all_challenge_mutations(
        self,
    ) -> None:
        driver = Mock()
        driver.page = Mock()
        click_error = RuntimeError("label detached")
        applejack = Mock(text="Applejack")
        applejack.click = AsyncMock(side_effect=click_error)
        twilight = Mock(text="Twilight Sparkle")
        twilight.click = AsyncMock()
        driver.page.select_all = AsyncMock(return_value=[applejack, twilight])
        predictor = Mock(
            return_value=Mock(labels=frozenset({"Applejack", "Twilight Sparkle"}))
        )
        challenge = PonyChart(driver)

        with (
            patch.object(ponychart_module, "_predict", predictor),
            patch.object(
                ponychart_module,
                "is_browser_generation_error",
                return_value=False,
            ),
            self.assertRaises(BattleInterruptedError) as raised,
        ):
            await challenge._auto_answer(b"challenge")

        self.assertEqual(
            raised.exception.diagnostic_code,
            "battle.ponychart.label-click-outcome-unknown",
        )
        self.assertIs(raised.exception.__cause__, click_error)
        applejack.click.assert_awaited_once_with()
        twilight.click.assert_not_awaited()

    async def test_predicted_label_click_timeout_is_not_swallowed(self) -> None:
        driver = Mock()
        driver.page = Mock()
        timeout = ZendriverOperationTimeout(timeout_seconds=3.0)
        twilight = Mock(text="Twilight Sparkle")
        twilight.click = AsyncMock(side_effect=timeout)
        driver.page.select_all = AsyncMock(return_value=[twilight])
        predictor = Mock(return_value=Mock(labels=frozenset({"Twilight Sparkle"})))
        challenge = PonyChart(driver)

        with (
            patch.object(ponychart_module, "_predict", predictor),
            self.assertRaises(ZendriverOperationTimeout) as raised,
        ):
            await challenge._auto_answer(b"challenge")

        self.assertIs(raised.exception, timeout)
        twilight.click.assert_awaited_once_with()

    async def test_auto_answer_timeout_stops_challenge_handling(self) -> None:
        image = b"challenge"
        driver = Mock(headless=True)
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)
        challenge._capture_pony_chart_image = AsyncMock(return_value=image)
        timeout = ZendriverOperationTimeout(timeout_seconds=3.0)
        challenge._auto_answer = AsyncMock(side_effect=timeout)

        with self.assertRaises(ZendriverOperationTimeout) as raised:
            await challenge.check()

        self.assertIs(raised.exception, timeout)
        challenge._check.assert_awaited_once_with()
        challenge._auto_answer.assert_awaited_once_with(image)

    async def test_fallback_start_warns_and_success_detail_is_debug(self) -> None:
        image = b"challenge"
        driver = Mock(headless=True)
        driver.page = Mock()
        submit = Mock()
        submit.click = AsyncMock()
        driver.page.xpath = AsyncMock(return_value=[submit])
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(side_effect=[True] * 12 + [False])
        challenge._capture_pony_chart_image = AsyncMock(return_value=image)
        challenge._auto_answer = AsyncMock(return_value=frozenset({"Twilight Sparkle"}))

        with (
            patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
            patch.object(ponychart_module, "logger") as ponychart_logger,
        ):
            detected = await challenge.check()

        self.assertTrue(detected)
        submit.click.assert_awaited_once_with()
        ponychart_logger.warning.assert_called_once_with(
            "PonyChart remained present after %ds; " "attempting fallback submission",
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
        image = b"challenge"
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
        challenge._capture_pony_chart_image = AsyncMock(return_value=image)
        challenge._auto_answer = AsyncMock(return_value=frozenset({"Twilight Sparkle"}))

        with (
            patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
            patch.object(
                ponychart_module,
                "is_browser_generation_error",
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
                    "PonyChart fallback submission recovered after lookup error "
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

    async def test_fallback_submit_timeout_is_not_replayed(self) -> None:
        image = b"challenge"
        driver = Mock(headless=True)
        driver.page = Mock()
        timeout = ZendriverOperationTimeout(timeout_seconds=3.0)
        submit = Mock()
        submit.click = AsyncMock(side_effect=timeout)
        driver.page.xpath = AsyncMock(return_value=[submit])
        driver.page.select = AsyncMock()
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)
        challenge._capture_pony_chart_image = AsyncMock(return_value=image)
        challenge._auto_answer = AsyncMock(return_value=frozenset({"Twilight Sparkle"}))

        with (
            patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
            self.assertRaises(ZendriverOperationTimeout) as raised,
        ):
            await challenge.check()

        self.assertIs(raised.exception, timeout)
        submit.click.assert_awaited_once_with()
        driver.page.select.assert_not_awaited()

    async def test_fallback_submit_generic_error_is_not_replayed(self) -> None:
        image = b"challenge"
        driver = Mock(headless=True)
        driver.page = Mock()
        click_error = RuntimeError("submit detached after dispatch")
        submit = Mock()
        submit.click = AsyncMock(side_effect=click_error)
        driver.page.xpath = AsyncMock(return_value=[submit])
        driver.page.select = AsyncMock()
        challenge = PonyChart(driver)
        challenge._check = AsyncMock(return_value=True)
        challenge._capture_pony_chart_image = AsyncMock(return_value=image)
        challenge._auto_answer = AsyncMock(return_value=frozenset({"Twilight Sparkle"}))

        with (
            patch.object(ponychart_module.asyncio, "sleep", new=AsyncMock()),
            self.assertRaises(BattleInterruptedError) as raised,
        ):
            await challenge.check()

        self.assertEqual(
            raised.exception.diagnostic_code,
            "battle.ponychart.submit-outcome-unknown",
        )
        self.assertIs(raised.exception.__cause__, click_error)
        submit.click.assert_awaited_once_with()
        driver.page.select.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

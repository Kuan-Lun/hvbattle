import unittest
from unittest.mock import AsyncMock, Mock, patch

from hvbattle import (
    ArenaOption,
    GrindfestOption,
    RingOfBloodOption,
    RingOfBloodSnapshot,
    RingOfBloodStartOutcome,
)
from hvbattle.battle_launcher import BattleLauncher


class BattleLauncherLoggingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _launcher() -> tuple[BattleLauncher, Mock]:
        client = Mock()
        client.page = Mock()
        launcher = BattleLauncher(client, Mock())
        launcher._path_prefix = AsyncMock(return_value="")
        return launcher, client.page

    @staticmethod
    def _ring_values() -> (
        tuple[RingOfBloodOption, RingOfBloodSnapshot, dict[str, object]]
    ):
        option = RingOfBloodOption(112, "Triple Trio and the Tree", 1.0, 10)
        snapshot = RingOfBloodSnapshot(20, (option,))
        payload: dict[str, object] = {
            "tokenText": "You have 20 tokens of blood.",
            "rows": [
                {
                    "onclick": "init_battle(112,10)",
                    "challengeName": "Triple Trio and the Tree",
                    "expText": "X1.0",
                    "entryCostText": "10 Tokens",
                }
            ],
        }
        return option, snapshot, payload

    async def test_wrong_page_logs_expected_and_current_at_debug(self) -> None:
        cases = (
            (
                "arena",
                "start_arena",
                ArenaOption(12, "secret-arena-token"),
                "https://hentaiverse.org/?s=Battle&ss=ar",
            ),
            (
                "grindfest",
                "start_grindfest",
                GrindfestOption(34),
                "https://hentaiverse.org/?s=Battle&ss=gr",
            ),
        )
        current_url = "https://hentaiverse.org/"

        for kind, method_name, option, expected_url in cases:
            with self.subTest(kind=kind):
                launcher, page = self._launcher()
                page.evaluate = AsyncMock(return_value=current_url)

                with patch("hvbattle.battle_launcher.logger") as launcher_logger:
                    submitted = await getattr(launcher, method_name)(option)

                self.assertFalse(submitted)
                page.evaluate.assert_awaited_once_with("window.location.href")
                launcher_logger.debug.assert_called_once_with(
                    f"Battle form submission skipped kind={kind} id=%s "
                    "reason=unexpected-page expected=%s current=%s",
                    option.battle_id,
                    expected_url,
                    current_url,
                )
                launcher_logger.info.assert_not_called()
                launcher_logger.warning.assert_not_called()
                self.assertNotIn(
                    "secret-arena-token", repr(launcher_logger.method_calls)
                )

    async def test_false_submission_warns_with_kind_and_id(self) -> None:
        cases = (
            (
                "arena",
                "start_arena",
                ArenaOption(12, "secret-arena-token"),
                "https://hentaiverse.org/?s=Battle&ss=ar",
            ),
            (
                "grindfest",
                "start_grindfest",
                GrindfestOption(34),
                "https://hentaiverse.org/?s=Battle&ss=gr",
            ),
        )

        for kind, method_name, option, expected_url in cases:
            with self.subTest(kind=kind):
                launcher, page = self._launcher()
                page.evaluate = AsyncMock(side_effect=[expected_url, False])

                with patch("hvbattle.battle_launcher.logger") as launcher_logger:
                    submitted = await getattr(launcher, method_name)(option)

                self.assertFalse(submitted)
                launcher_logger.warning.assert_called_once_with(
                    f"Battle form was not submitted kind={kind} id=%s",
                    option.battle_id,
                )
                launcher_logger.info.assert_not_called()
                self.assertNotIn(
                    "secret-arena-token", repr(launcher_logger.method_calls)
                )

    async def test_submission_exception_logs_safe_error_context(self) -> None:
        cases = (
            (
                "arena",
                "start_arena",
                ArenaOption(12, "secret-arena-token"),
                "https://hentaiverse.org/?s=Battle&ss=ar",
            ),
            (
                "grindfest",
                "start_grindfest",
                GrindfestOption(34),
                "https://hentaiverse.org/?s=Battle&ss=gr",
            ),
        )

        for kind, method_name, option, expected_url in cases:
            with self.subTest(kind=kind):
                launcher, page = self._launcher()
                submission_error = RuntimeError(
                    "submission failed with secret-arena-token"
                )
                page.evaluate = AsyncMock(side_effect=[expected_url, submission_error])

                with (
                    patch("hvbattle.battle_launcher.logger") as launcher_logger,
                    self.assertRaises(RuntimeError) as raised,
                ):
                    await getattr(launcher, method_name)(option)

                self.assertIs(raised.exception, submission_error)
                launcher_logger.error.assert_called_once_with(
                    "Battle form submission outcome is unknown "
                    f"kind={kind} id=%s error_type=%s",
                    option.battle_id,
                    "RuntimeError",
                )
                launcher_logger.exception.assert_not_called()
                launcher_logger.info.assert_not_called()
                self.assertNotIn(
                    "secret-arena-token", repr(launcher_logger.method_calls)
                )

    async def test_success_reports_only_form_submission(self) -> None:
        cases = (
            (
                "start_arena",
                ArenaOption(12, "secret-arena-token"),
                "https://hentaiverse.org/?s=Battle&ss=ar",
                "Submitted Arena battle form id=%s",
            ),
            (
                "start_grindfest",
                GrindfestOption(34),
                "https://hentaiverse.org/?s=Battle&ss=gr",
                "Submitted GrindFest battle form id=%s",
            ),
        )

        for method_name, option, expected_url, message in cases:
            with self.subTest(method=method_name):
                launcher, page = self._launcher()
                page.evaluate = AsyncMock(side_effect=[expected_url, True])

                with patch("hvbattle.battle_launcher.logger") as launcher_logger:
                    submitted = await getattr(launcher, method_name)(option)

                self.assertTrue(submitted)
                launcher_logger.info.assert_called_once_with(
                    message,
                    option.battle_id,
                )
                launcher_logger.warning.assert_not_called()
                launcher_logger.error.assert_not_called()
                self.assertNotIn("Started", repr(launcher_logger.method_calls))
                self.assertNotIn(
                    "secret-arena-token", repr(launcher_logger.method_calls)
                )

    async def test_ring_wrong_page_logs_only_safe_context(self) -> None:
        launcher, page = self._launcher()
        option, snapshot, _payload = self._ring_values()
        secret = "secret-current-url-detail"
        current_url = f"https://hentaiverse.org/?private={secret}"
        expected_url = "https://hentaiverse.org/?s=Battle&ss=rb"
        page.evaluate = AsyncMock(return_value=current_url)

        with patch("hvbattle.battle_launcher.logger") as launcher_logger:
            outcome = await launcher.start_ring_of_blood(
                option,
                expected_before=snapshot,
            )

        self.assertIs(outcome, RingOfBloodStartOutcome.OPTION_UNAVAILABLE)
        launcher_logger.debug.assert_called_once_with(
            "Battle form submission skipped kind=ring-of-blood id=%s "
            "reason=unexpected-page expected=%s",
            option.battle_id,
            expected_url,
        )
        launcher_logger.info.assert_not_called()
        launcher_logger.warning.assert_not_called()
        self.assertNotIn(secret, repr(launcher_logger.method_calls))

    async def test_ring_submission_exception_logs_only_error_type(self) -> None:
        launcher, page = self._launcher()
        option, snapshot, payload = self._ring_values()
        secret_detail = "server rejected private response detail"
        submission_error = RuntimeError(secret_detail)
        page.evaluate = AsyncMock(
            side_effect=[
                "https://hentaiverse.org/?s=Battle&ss=rb",
                payload,
                submission_error,
            ]
        )

        with (
            patch("hvbattle.battle_launcher.logger") as launcher_logger,
            self.assertRaises(RuntimeError) as raised,
        ):
            await launcher.start_ring_of_blood(
                option,
                expected_before=snapshot,
            )

        self.assertIs(raised.exception, submission_error)
        launcher_logger.error.assert_called_once_with(
            "Battle form submission outcome is unknown "
            "kind=ring-of-blood id=%s error_type=%s",
            option.battle_id,
            "RuntimeError",
        )
        launcher_logger.exception.assert_not_called()
        launcher_logger.info.assert_not_called()
        self.assertNotIn(secret_detail, repr(launcher_logger.method_calls))


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import AsyncMock, Mock, call, patch

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
        launcher_logger.info.assert_called_once_with(
            "Ring of Blood pre-submit check id=%s reason=unexpected-page",
            option.battle_id,
        )
        launcher_logger.debug.assert_not_called()
        launcher_logger.warning.assert_not_called()
        self.assertNotIn(expected_url, repr(launcher_logger.method_calls))
        self.assertNotIn(secret, repr(launcher_logger.method_calls))

    async def test_ring_inspection_logs_only_parsed_start_action_details(self) -> None:
        launcher, page = self._launcher()
        option, snapshot, payload = self._ring_values()
        page.evaluate = AsyncMock(return_value=payload)

        with patch("hvbattle.battle_launcher.logger") as launcher_logger:
            inspected = await launcher.inspect_ring_of_blood()

        self.assertEqual(inspected, snapshot)
        self.assertEqual(
            launcher_logger.info.call_args_list,
            [
                call(
                    "Ring of Blood inspection complete tokens=%s start_actions=%s",
                    20,
                    1,
                ),
                call(
                    "Ring of Blood start action found "
                    "challenge=%r id=%s entry_cost=%s",
                    option.challenge_name,
                    option.battle_id,
                    option.entry_cost,
                ),
            ],
        )
        logged = repr(launcher_logger.method_calls)
        self.assertNotIn("init_battle", logged)
        self.assertNotIn("tokenText", logged)

    async def test_ring_success_logs_precheck_and_atomic_submission(self) -> None:
        launcher, page = self._launcher()
        option, snapshot, payload = self._ring_values()
        page.evaluate = AsyncMock(
            side_effect=[
                "https://hentaiverse.org/?s=Battle&ss=rb",
                payload,
                "submitted",
            ]
        )

        with patch("hvbattle.battle_launcher.logger") as launcher_logger:
            outcome = await launcher.start_ring_of_blood(
                option,
                expected_before=snapshot,
            )

        self.assertIs(outcome, RingOfBloodStartOutcome.SUBMITTED)
        self.assertEqual(
            launcher_logger.info.call_args_list,
            [
                call(
                    "Ring of Blood inspection complete tokens=%s start_actions=%s",
                    20,
                    1,
                ),
                call(
                    "Ring of Blood start action found "
                    "challenge=%r id=%s entry_cost=%s",
                    option.challenge_name,
                    option.battle_id,
                    option.entry_cost,
                ),
                call(
                    "Ring of Blood pre-submit check id=%s action_present=%s "
                    "snapshot_matches=%s required=%s available=%s",
                    option.battle_id,
                    True,
                    True,
                    option.entry_cost,
                    snapshot.tokens_of_blood,
                ),
                call(
                    "Ring of Blood atomic submission check id=%s result=%s",
                    option.battle_id,
                    "submitted",
                ),
                call(
                    "Submitted Ring of Blood battle form id=%s",
                    option.battle_id,
                ),
            ],
        )
        launcher_logger.warning.assert_not_called()

    async def test_ring_precheck_logs_when_target_action_disappears(self) -> None:
        launcher, page = self._launcher()
        option, snapshot, payload = self._ring_values()
        payload["rows"] = []
        page.evaluate = AsyncMock(
            side_effect=[
                "https://hentaiverse.org/?s=Battle&ss=rb",
                payload,
            ]
        )

        with patch("hvbattle.battle_launcher.logger") as launcher_logger:
            outcome = await launcher.start_ring_of_blood(
                option,
                expected_before=snapshot,
            )

        self.assertIs(outcome, RingOfBloodStartOutcome.OPTION_UNAVAILABLE)
        launcher_logger.info.assert_any_call(
            "Ring of Blood pre-submit check id=%s action_present=%s "
            "snapshot_matches=%s required=%s available=%s",
            option.battle_id,
            False,
            False,
            option.entry_cost,
            snapshot.tokens_of_blood,
        )
        launcher_logger.info.assert_any_call(
            "Ring of Blood option is unavailable id=%s",
            option.battle_id,
        )

    async def test_ring_atomic_failures_log_exact_closed_reason(self) -> None:
        results = (
            "unexpected-page",
            "missing-table",
            "missing-initid",
            "missing-initform",
            "missing-exact-action",
            "unexpected-result-from-page",
            {"unexpected": "payload"},
        )
        for atomic_result in results:
            with self.subTest(atomic_result=atomic_result):
                launcher, page = self._launcher()
                option, snapshot, payload = self._ring_values()
                page.evaluate = AsyncMock(
                    side_effect=[
                        "https://hentaiverse.org/?s=Battle&ss=rb",
                        payload,
                        atomic_result,
                    ]
                )

                with patch("hvbattle.battle_launcher.logger") as launcher_logger:
                    outcome = await launcher.start_ring_of_blood(
                        option,
                        expected_before=snapshot,
                    )

                expected_reason = (
                    atomic_result
                    if isinstance(atomic_result, str)
                    and atomic_result
                    in {
                        "unexpected-page",
                        "missing-table",
                        "missing-initid",
                        "missing-initform",
                        "missing-exact-action",
                    }
                    else "unexpected-result"
                )
                self.assertIs(
                    outcome,
                    RingOfBloodStartOutcome.OPTION_UNAVAILABLE,
                )
                launcher_logger.info.assert_any_call(
                    "Ring of Blood atomic submission check id=%s result=%s",
                    option.battle_id,
                    expected_reason,
                )
                launcher_logger.warning.assert_called_once_with(
                    "Battle form was not submitted kind=ring-of-blood id=%s "
                    "reason=%s",
                    option.battle_id,
                    expected_reason,
                )
                if expected_reason == "unexpected-result":
                    self.assertNotIn(
                        repr(atomic_result),
                        repr(launcher_logger.method_calls),
                    )

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
        self.assertEqual(
            launcher_logger.info.call_args_list,
            [
                call(
                    "Ring of Blood inspection complete tokens=%s start_actions=%s",
                    20,
                    1,
                ),
                call(
                    "Ring of Blood start action found "
                    "challenge=%r id=%s entry_cost=%s",
                    option.challenge_name,
                    option.battle_id,
                    option.entry_cost,
                ),
                call(
                    "Ring of Blood pre-submit check id=%s action_present=%s "
                    "snapshot_matches=%s required=%s available=%s",
                    option.battle_id,
                    True,
                    True,
                    option.entry_cost,
                    snapshot.tokens_of_blood,
                ),
            ],
        )
        self.assertNotIn(secret_detail, repr(launcher_logger.method_calls))


if __name__ == "__main__":
    unittest.main()

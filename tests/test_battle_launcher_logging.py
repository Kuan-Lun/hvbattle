import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from hvbrowser import MaintenanceNavigationBlocker, Realm

from hvbattle import (
    ArenaOption,
    GrindfestOption,
    RingOfBloodChallenge,
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
        lifecycle = Mock()
        lifecycle.enable = AsyncMock()
        lifecycle.trigger = Mock()
        lifecycle.wait = AsyncMock()
        lifecycle.close = Mock()
        launcher._main_document_lifecycle = Mock(return_value=lifecycle)
        launcher._confirm_battle_form_receipt = AsyncMock(
            return_value=MaintenanceNavigationBlocker.ACTIVE
        )
        return launcher, client.page

    @staticmethod
    def _ring_values() -> (
        tuple[RingOfBloodOption, RingOfBloodSnapshot, dict[str, object]]
    ):
        option = RingOfBloodOption(105, "Konata", 1.0, 1)
        snapshot = RingOfBloodSnapshot(
            20,
            (option,),
            (
                RingOfBloodChallenge("Konata", 1.0, 1, option),
                RingOfBloodChallenge("Triple Trio and the Tree", None, None),
            ),
        )
        payload: dict[str, object] = {
            "tokenText": "You have 20 tokens of blood.",
            "rows": [
                {
                    "onclick": "init_battle(105,1)",
                    "challengeName": "Konata",
                    "expText": "X1.0",
                    "entryCostText": "1 Token",
                },
                {
                    "onclick": None,
                    "challengeName": "Triple Trio and the Tree",
                    "expText": "",
                    "entryCostText": "Completed",
                },
            ],
        }
        return option, snapshot, payload

    @staticmethod
    def _ring_inspection_calls(option: RingOfBloodOption) -> list[object]:
        return [
            call(
                "Ring of Blood inspection complete tokens=%s challenges=%s "
                "start_actions=%s",
                20,
                2,
                1,
            ),
            call(
                "Ring of Blood challenge observed challenge=%r startable=%s "
                "id=%s entry_cost=%s",
                option.challenge_name,
                True,
                option.battle_id,
                option.entry_cost,
            ),
            call(
                "Ring of Blood challenge observed challenge=%r startable=%s "
                "id=%s entry_cost=%s",
                "Triple Trio and the Tree",
                False,
                None,
                None,
            ),
        ]

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
        for kind, method_name, option, expected_url in cases:
            with self.subTest(kind=kind):
                launcher, page = self._launcher()
                page.evaluate = AsyncMock(return_value="unexpected-page")

                with patch("hvbattle.battle_launcher.logger") as launcher_logger:
                    submitted = await getattr(launcher, method_name)(
                        option,
                        expected_realm=Realm.PERSISTENT,
                    )

                self.assertFalse(submitted)
                page.evaluate.assert_awaited_once()
                launcher_logger.debug.assert_called_once_with(
                    f"Battle form submission skipped kind={kind} id=%s "
                    "reason=unexpected-page",
                    option.battle_id,
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
                page.evaluate = AsyncMock(return_value="form-unavailable")

                with patch("hvbattle.battle_launcher.logger") as launcher_logger:
                    submitted = await getattr(launcher, method_name)(
                        option,
                        expected_realm=Realm.PERSISTENT,
                    )

                self.assertFalse(submitted)
                launcher_logger.warning.assert_called_once_with(
                    f"Battle form was not submitted kind={kind} id=%s reason=%s",
                    option.battle_id,
                    "form-unavailable",
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
                launcher._confirm_battle_form_receipt.side_effect = TimeoutError(
                    "receipt missing"
                )
                page.evaluate = AsyncMock(side_effect=submission_error)

                with (
                    patch("hvbattle.battle_launcher.logger") as launcher_logger,
                    self.assertRaises(RuntimeError) as raised,
                ):
                    await getattr(launcher, method_name)(
                        option,
                        expected_realm=Realm.PERSISTENT,
                    )

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
                page.evaluate = AsyncMock(return_value="submitted")

                with patch("hvbattle.battle_launcher.logger") as launcher_logger:
                    submitted = await getattr(launcher, method_name)(
                        option,
                        expected_realm=Realm.PERSISTENT,
                    )

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
        page.evaluate = AsyncMock(return_value="unexpected-page")

        with patch("hvbattle.battle_launcher.logger") as launcher_logger:
            outcome = await launcher.start_ring_of_blood(
                option,
                expected_before=snapshot,
                expected_realm=Realm.PERSISTENT,
            )

        self.assertIs(outcome, RingOfBloodStartOutcome.OPTION_UNAVAILABLE)
        launcher_logger.debug.assert_any_call(
            "Ring of Blood pre-submit check id=%s reason=unexpected-page",
            option.battle_id,
        )
        launcher_logger.info.assert_not_called()
        launcher_logger.warning.assert_not_called()
        self.assertNotIn(secret, repr(launcher_logger.method_calls))

    async def test_ring_inspection_logs_all_parsed_challenge_details(self) -> None:
        launcher, page = self._launcher()
        option, snapshot, payload = self._ring_values()
        page.evaluate = AsyncMock(return_value=payload)

        with patch("hvbattle.battle_launcher.logger") as launcher_logger:
            inspected = await launcher.inspect_ring_of_blood()

        self.assertEqual(inspected, snapshot)
        self.assertEqual(
            launcher_logger.debug.call_args_list,
            self._ring_inspection_calls(option),
        )
        launcher_logger.info.assert_not_called()

    async def test_ring_inspection_rejection_logs_only_fixed_reason_code(self) -> None:
        launcher, page = self._launcher()
        secret = "secret-dom-detail"
        page.evaluate = AsyncMock(
            return_value={
                "tokenText": "You have 20 tokens of blood.",
                "rows": [
                    {
                        "onclick": "init_battle(105,1)",
                        "challengeName": "Konata",
                        "expText": secret,
                        "entryCostText": "1 Token",
                    }
                ],
            }
        )

        with (
            patch("hvbattle.battle_launcher.logger") as launcher_logger,
            self.assertRaisesRegex(RuntimeError, "EXP multiplier"),
        ):
            await launcher.inspect_ring_of_blood()

        launcher_logger.warning.assert_called_once_with(
            "Ring of Blood inspection rejected reason=%s row_index=%s",
            "ring.exp-invalid",
            0,
        )
        self.assertNotIn(secret, repr(launcher_logger.method_calls))
        logged = repr(launcher_logger.method_calls)
        self.assertNotIn("init_battle", logged)
        self.assertNotIn("tokenText", logged)

    async def test_ring_success_logs_precheck_and_atomic_submission(self) -> None:
        launcher, page = self._launcher()
        option, snapshot, _payload = self._ring_values()
        page.evaluate = AsyncMock(return_value="submitted")

        with patch("hvbattle.battle_launcher.logger") as launcher_logger:
            outcome = await launcher.start_ring_of_blood(
                option,
                expected_before=snapshot,
                expected_realm=Realm.PERSISTENT,
            )

        self.assertIs(outcome, RingOfBloodStartOutcome.SUBMITTED)
        self.assertEqual(
            launcher_logger.debug.call_args_list,
            [
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
        launcher_logger.info.assert_not_called()
        launcher_logger.warning.assert_not_called()

    async def test_ring_precheck_logs_when_target_action_disappears(self) -> None:
        launcher, page = self._launcher()
        option, snapshot, _payload = self._ring_values()
        page.evaluate = AsyncMock(return_value="option-unavailable")

        with patch("hvbattle.battle_launcher.logger") as launcher_logger:
            outcome = await launcher.start_ring_of_blood(
                option,
                expected_before=snapshot,
                expected_realm=Realm.PERSISTENT,
            )

        self.assertIs(outcome, RingOfBloodStartOutcome.OPTION_UNAVAILABLE)
        launcher_logger.debug.assert_any_call(
            "Ring of Blood atomic submission check id=%s result=%s",
            option.battle_id,
            "option-unavailable",
        )
        launcher_logger.debug.assert_any_call(
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
                option, snapshot, _payload = self._ring_values()
                page.evaluate = AsyncMock(return_value=atomic_result)

                with patch("hvbattle.battle_launcher.logger") as launcher_logger:
                    outcome = await launcher.start_ring_of_blood(
                        option,
                        expected_before=snapshot,
                        expected_realm=Realm.PERSISTENT,
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
                launcher_logger.debug.assert_any_call(
                    "Ring of Blood atomic submission check id=%s result=%s",
                    option.battle_id,
                    expected_reason,
                )
                if expected_reason == "unexpected-page":
                    launcher_logger.warning.assert_not_called()
                else:
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
        option, snapshot, _payload = self._ring_values()
        secret_detail = "server rejected private response detail"
        submission_error = RuntimeError(secret_detail)
        launcher._confirm_battle_form_receipt.side_effect = TimeoutError(
            "receipt missing"
        )
        page.evaluate = AsyncMock(side_effect=submission_error)

        with (
            patch("hvbattle.battle_launcher.logger") as launcher_logger,
            self.assertRaises(RuntimeError) as raised,
        ):
            await launcher.start_ring_of_blood(
                option,
                expected_before=snapshot,
                expected_realm=Realm.PERSISTENT,
            )

        self.assertIs(raised.exception, submission_error)
        launcher_logger.error.assert_called_once_with(
            "Battle form submission outcome is unknown "
            "kind=ring-of-blood id=%s error_type=%s",
            option.battle_id,
            "RuntimeError",
        )
        launcher_logger.exception.assert_not_called()
        launcher_logger.debug.assert_not_called()
        launcher_logger.info.assert_not_called()
        self.assertNotIn(secret_detail, repr(launcher_logger.method_calls))


if __name__ == "__main__":
    unittest.main()

import os
import subprocess
import sys
import tomllib
import typing
import unittest
from pathlib import Path
from unittest.mock import patch


class PublicApiTests(unittest.TestCase):
    def test_clean_break_package_line_is_exact(self) -> None:
        project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with project_path.open("rb") as project_file:
            project = tomllib.load(project_file)["project"]

        self.assertEqual(project["version"], "0.8.1")
        self.assertIn("hvbrowser>=0.5.0,<0.6", project["dependencies"])

    def test_import_does_not_require_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            import hvbattle

        self.assertTrue(hasattr(hvbattle, "BattleSession"))
        self.assertTrue(hasattr(hvbattle, "BattleRunner"))
        self.assertTrue(hasattr(hvbattle, "BattleTurnPhase"))
        self.assertTrue(hasattr(hvbattle, "BattleTurnState"))
        self.assertTrue(hasattr(hvbattle, "BattleStrategy"))
        self.assertTrue(hasattr(hvbattle, "BaseControlPanel"))
        self.assertTrue(hasattr(hvbattle, "ControlPanel"))
        self.assertTrue(hasattr(hvbattle, "NullControlPanel"))
        self.assertTrue(hasattr(hvbattle, "BattleCompleted"))
        self.assertTrue(hasattr(hvbattle, "BattleStopped"))
        self.assertTrue(hasattr(hvbattle, "BattleAbsent"))
        self.assertTrue(hasattr(hvbattle, "BattlePresence"))
        self.assertTrue(hasattr(hvbattle, "BattleStepIdle"))
        self.assertTrue(hasattr(hvbattle, "BattleStepIdleReason"))
        self.assertTrue(hasattr(hvbattle, "BattleStepProgress"))
        self.assertTrue(hasattr(hvbattle, "BattleStepProgressKind"))
        self.assertTrue(hasattr(hvbattle, "BattleStepResult"))
        self.assertTrue(hasattr(hvbattle, "BattleInterruptedError"))
        self.assertTrue(hasattr(hvbattle, "BattleActionOutcomeUnknownError"))
        self.assertTrue(hasattr(hvbattle, "TurnDecision"))
        self.assertTrue(hasattr(hvbattle, "ArenaOption"))
        self.assertTrue(hasattr(hvbattle, "GrindfestOption"))
        self.assertTrue(hasattr(hvbattle, "RingOfBloodChallenge"))
        self.assertTrue(hasattr(hvbattle, "RingOfBloodOption"))
        self.assertTrue(hasattr(hvbattle, "RingOfBloodSnapshot"))
        self.assertTrue(hasattr(hvbattle, "RingOfBloodStartOutcome"))
        self.assertTrue(hasattr(hvbattle, "PonyChartArtifactError"))
        self.assertTrue(hasattr(hvbattle, "PonyChartRefreshOutcome"))
        self.assertTrue(hasattr(hvbattle, "PonyChartResolutionError"))
        self.assertTrue(hasattr(hvbattle, "refresh_ponychart_classifier"))

    def test_strategy_annotations_resolve_at_runtime(self) -> None:
        from hvbattle import BattleStrategy

        hints = typing.get_type_hints(BattleStrategy.take_turn)

        self.assertIn("session", hints)

    def test_import_does_not_eagerly_load_ml_runtime(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, hvbattle; " "print('ponychart_classifier' in sys.modules)",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.stdout.strip(), "False")

    def test_import_does_not_require_tkinter(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.modules['tkinter'] = None; import hvbattle; print('ok')",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.stdout.strip(), "ok")

    def test_battle_interruption_requires_valid_machine_diagnostic_code(self) -> None:
        from hvbattle import BattleInterruptedError, BattleRecoveryExhaustedError

        for error_type in (BattleInterruptedError, BattleRecoveryExhaustedError):
            with self.subTest(error_type=error_type.__name__):
                error = error_type(
                    "Human-readable detail",
                    diagnostic_code="battle.turn-timeout",
                )

                self.assertEqual(error.diagnostic_code, "battle.turn-timeout")
                self.assertEqual(str(error), "Human-readable detail")
                with self.assertRaises(TypeError):
                    error_type("legacy constructor")  # type: ignore[call-arg]

        invalid_codes = (
            "",
            "Battle.UPPERCASE",
            "battle contains spaces",
            "<html>secret</html>",
            "battle.é",
            "b" * 129,
        )
        for diagnostic_code in invalid_codes:
            with self.subTest(diagnostic_code=diagnostic_code):
                with self.assertRaisesRegex(ValueError, "diagnostic_code"):
                    BattleInterruptedError(
                        "detail",
                        diagnostic_code=diagnostic_code,
                    )


if __name__ == "__main__":
    unittest.main()

import os
import subprocess
import sys
import typing
import unittest
from unittest.mock import patch


class PublicApiTests(unittest.TestCase):
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
        self.assertTrue(hasattr(hvbattle, "BattleInterruptedError"))
        self.assertTrue(hasattr(hvbattle, "BattleActionOutcomeUnknownError"))
        self.assertTrue(hasattr(hvbattle, "TurnDecision"))
        self.assertTrue(hasattr(hvbattle, "ArenaOption"))
        self.assertTrue(hasattr(hvbattle, "GrindfestOption"))
        self.assertTrue(hasattr(hvbattle, "PonyChartResolutionError"))

    def test_strategy_annotations_resolve_at_runtime(self) -> None:
        from hvbattle import BattleStrategy

        hints = typing.get_type_hints(BattleStrategy.take_turn)

        self.assertIn("session", hints)

    def test_battle_driver_is_only_a_session_compatibility_alias(self) -> None:
        from hvbattle import BattleDriver, BattleSession

        self.assertIs(BattleDriver, BattleSession)
        self.assertFalse(hasattr(BattleDriver, "battle"))

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


if __name__ == "__main__":
    unittest.main()

import os
import subprocess
import sys
import unittest
from unittest.mock import patch


class PublicApiTests(unittest.TestCase):
    def test_import_does_not_require_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            import hvbattle

        self.assertTrue(hasattr(hvbattle, "BattleDriver"))
        self.assertTrue(hasattr(hvbattle, "StatThreshold"))

    def test_default_configuration_is_available(self) -> None:
        from hvbattle import DEFAULT_FORBIDDEN_SKILLS, DEFAULT_STATTHRESHOLD

        self.assertIsInstance(DEFAULT_FORBIDDEN_SKILLS, frozenset)
        self.assertGreater(DEFAULT_STATTHRESHOLD.hp_low, 0)

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


if __name__ == "__main__":
    unittest.main()

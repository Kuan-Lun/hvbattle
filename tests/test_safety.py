import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from hvbrowser import HVDriver

import hvbattle.hv_battle as battle_module
import hvbattle.hv_battle_ponychart as ponychart_module
from hvbattle import BattleDriver


class BattleDriverSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_startup_preloads_ponychart_before_browser(self) -> None:
        driver = BattleDriver(headless=True)
        events: list[str] = []
        driver._setup_alert_handler = AsyncMock()
        driver._init_battle_components = AsyncMock()

        with (
            patch.object(
                battle_module,
                "preload_ponychart_classifier",
                side_effect=lambda: events.append("preload"),
            ),
            patch.object(
                HVDriver,
                "_init_browser",
                new=AsyncMock(side_effect=lambda: events.append("browser")),
            ),
        ):
            await driver._init_browser()

        self.assertEqual(events, ["preload", "browser"])

    async def test_run_current_does_nothing_outside_battle(self) -> None:
        driver = object.__new__(BattleDriver)
        driver._wait_if_paused = AsyncMock()
        driver._is_in_battle = AsyncMock(return_value=False)
        driver._run_active_battle = AsyncMock()
        driver.repairequipment = AsyncMock()
        driver._ensure_stamina = AsyncMock()
        driver._try_auto_start_battle = AsyncMock()

        result = await BattleDriver.run_current(driver)

        self.assertFalse(result)
        driver._run_active_battle.assert_not_awaited()
        driver.repairequipment.assert_not_awaited()
        driver._ensure_stamina.assert_not_awaited()
        driver._try_auto_start_battle.assert_not_awaited()

    async def test_run_current_only_runs_an_active_battle(self) -> None:
        driver = object.__new__(BattleDriver)
        driver._wait_if_paused = AsyncMock()
        driver._is_in_battle = AsyncMock(return_value=True)
        driver._run_active_battle = AsyncMock()

        result = await BattleDriver.run_current(driver)

        self.assertTrue(result)
        driver._run_active_battle.assert_awaited_once_with()


class BattleDriverConfigurationTests(unittest.TestCase):
    def test_auto_next_defaults_off_and_constructor_does_not_update_model(
        self,
    ) -> None:
        updater = Mock()

        driver = BattleDriver(headless=True, ponychart_updater=updater)

        self.assertFalse(driver.auto_next_arena_battle)
        self.assertFalse(driver.auto_next_grindfest_battle)
        updater.assert_not_called()
        driver.control_panel.destroy()

    def test_auto_next_requires_explicit_opt_in(self) -> None:
        driver = BattleDriver(
            headless=True,
            auto_next_arena_battle=True,
            auto_next_grindfest_battle=True,
        )

        self.assertTrue(driver.auto_next_arena_battle)
        self.assertTrue(driver.auto_next_grindfest_battle)
        driver.control_panel.destroy()


class PonyChartPreloadTests(unittest.TestCase):
    def test_preload_loads_model_once_before_prediction(self) -> None:
        original_predictor = ponychart_module._predict
        fake_module = types.ModuleType("ponychart_classifier")
        preload = Mock()

        def predict(_path: str) -> object:
            raise AssertionError("Prediction must not run during preload")

        fake_module.preload = preload
        fake_module.predict = predict
        ponychart_module._predict = None
        try:
            with patch.dict(sys.modules, {"ponychart_classifier": fake_module}):
                ponychart_module.preload_ponychart_classifier()
                ponychart_module.preload_ponychart_classifier()
        finally:
            ponychart_module._predict = original_predictor

        preload.assert_called_once_with()


class ArchitectureTests(unittest.TestCase):
    def test_hvbattle_uses_hvbrowser_boundary_not_hbrowser_directly(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbattle"
        imported_modules: set[str] = set()

        for source_file in source_root.glob("*.py"):
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.add(node.module)

        self.assertFalse(
            any(
                name == "hbrowser" or name.startswith("hbrowser.")
                for name in imported_modules
            )
        )

    def test_public_driver_has_no_flee_operation(self) -> None:
        self.assertFalse(any("flee" in name.casefold() for name in dir(BattleDriver)))


if __name__ == "__main__":
    unittest.main()

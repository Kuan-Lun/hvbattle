import ast
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from hvbattle._timing import SemanticDeadline
from hvbattle.contracts import BattleInterruptedError
from hvbattle.hv_battle_item_provider import ItemProvider
from hvbattle.hv_battle_skill_manager import SkillManager


class MenuDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_menu_uses_one_atomic_protocol_mutation(self) -> None:
        manager = object.__new__(SkillManager)
        manager.hvdriver = Mock()
        manager.hvdriver.page = Mock()
        manager.hvdriver.page.evaluate = AsyncMock(
            return_value={"status": "open", "clicks": 2}
        )

        await manager.open_spells_menu()

        manager.hvdriver.page.evaluate.assert_awaited_once()
        script = manager.hvdriver.page.evaluate.await_args.args[0]
        self.assertIn("clicks <= 2", script)
        self.assertIn("control.click()", script)

    async def test_skill_menu_unknown_mutation_is_never_retried(self) -> None:
        manager = object.__new__(SkillManager)
        manager.hvdriver = Mock()
        manager.hvdriver.page = Mock()
        error = RuntimeError("detached after click")
        manager.hvdriver.page.evaluate = AsyncMock(side_effect=error)

        with (
            patch(
                "hvbattle.hv_battle_skill_manager.is_browser_generation_error",
                return_value=False,
            ),
            self.assertRaises(BattleInterruptedError) as raised,
        ):
            await manager.open_skills_menu()

        self.assertIs(raised.exception.__cause__, error)
        manager.hvdriver.page.evaluate.assert_awaited_once()

    async def test_no_receipt_skill_click_is_one_atomic_mutation(self) -> None:
        manager = object.__new__(SkillManager)
        manager.hvdriver = Mock()
        manager.hvdriver.page = Mock()
        manager.hvdriver.page.evaluate = AsyncMock(return_value={"status": "clicked"})
        manager.element_action_manager = Mock()

        await manager._click_skill("skill_1", iswait=False)

        manager.hvdriver.page.evaluate.assert_awaited_once()
        script = manager.hvdriver.page.evaluate.await_args.args[0]
        self.assertEqual(script.count("element.click()"), 1)
        manager.element_action_manager.click_and_wait_log_locator.assert_not_called()

    async def test_no_receipt_skill_click_unknown_is_not_retried(self) -> None:
        manager = object.__new__(SkillManager)
        manager.hvdriver = Mock()
        manager.hvdriver.page = Mock()
        error = RuntimeError("click acknowledgement lost")
        manager.hvdriver.page.evaluate = AsyncMock(side_effect=error)

        with (
            patch(
                "hvbattle.hv_battle_skill_manager.is_browser_generation_error",
                return_value=False,
            ),
            self.assertRaises(BattleInterruptedError) as raised,
        ):
            await manager._click_skill("skill_1", iswait=False)

        self.assertIs(raised.exception.__cause__, error)
        manager.hvdriver.page.evaluate.assert_awaited_once()

    async def test_items_menu_uses_one_atomic_protocol_mutation(self) -> None:
        provider = object.__new__(ItemProvider)
        provider.hvdriver = Mock()
        provider.hvdriver.page = Mock()
        provider.hvdriver.page.evaluate = AsyncMock(
            return_value={"status": "open", "clicked": True}
        )

        await provider.click_items_menu()

        provider.hvdriver.page.evaluate.assert_awaited_once()
        script = provider.hvdriver.page.evaluate.await_args.args[0]
        self.assertEqual(script.count("control.click()"), 1)

    async def test_items_menu_unknown_mutation_is_never_retried(self) -> None:
        provider = object.__new__(ItemProvider)
        provider.hvdriver = Mock()
        provider.hvdriver.page = Mock()
        error = RuntimeError("response lost")
        provider.hvdriver.page.evaluate = AsyncMock(side_effect=error)

        with (
            patch(
                "hvbattle.hv_battle_item_provider.is_browser_generation_error",
                return_value=False,
            ),
            self.assertRaises(BattleInterruptedError) as raised,
        ):
            await provider.click_items_menu()

        self.assertIs(raised.exception.__cause__, error)
        provider.hvdriver.page.evaluate.assert_awaited_once()

    async def test_items_menu_does_not_accept_after_shared_deadline(self) -> None:
        now = 0.0
        provider = object.__new__(ItemProvider)
        provider.hvdriver = Mock()
        provider.hvdriver.page = Mock()

        async def late_ack(_script: str) -> dict[str, object]:
            nonlocal now
            now = 5.1
            return {"status": "open", "clicked": True}

        provider.hvdriver.page.evaluate = AsyncMock(side_effect=late_ack)
        deadline = SemanticDeadline(expires_at=5.0, _clock=lambda: now)

        with self.assertRaisesRegex(TimeoutError, "Items menu deadline"):
            await provider.click_items_menu(deadline=deadline)

        provider.hvdriver.page.evaluate.assert_awaited_once()


class MenuArchitectureTests(unittest.TestCase):
    def test_production_menu_flows_do_not_use_generic_retry_clicks(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbattle"
        violations: list[str] = []
        for filename in (
            "hv_battle_skill_manager.py",
            "hv_battle_item_provider.py",
        ):
            source_file = source_root / filename
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {"click_until", "click_resilient", "click_locator"}
                ):
                    violations.append(f"{filename}:{node.lineno}:{node.func.attr}")

        self.assertEqual(violations, [])

    def test_action_manager_does_not_reexport_dead_generic_clicks(self) -> None:
        source_file = (
            Path(__file__).parents[1]
            / "src"
            / "hvbattle"
            / "hv_battle_action_manager.py"
        )
        tree = ast.parse(source_file.read_text(), filename=str(source_file))
        methods = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }

        self.assertTrue(
            {"click_until", "click_resilient", "click_locator"}.isdisjoint(methods)
        )


if __name__ == "__main__":
    unittest.main()

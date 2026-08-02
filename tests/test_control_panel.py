import asyncio
import unittest
from unittest.mock import Mock

from hvbattle import ControlPanel, NullControlPanel


class ControlPanelStateTests(unittest.TestCase):
    def test_toggle_default_is_available_before_gui_processes_command(self) -> None:
        panel = object.__new__(ControlPanel)
        panel._toggle_dict = {}
        panel._cmd_queue = Mock()

        panel.register_toggle(
            "auto_next_arena_battle",
            "Arena",
            default=True,
            group="Auto Next Battle",
        )

        self.assertTrue(panel.get_toggle("auto_next_arena_battle"))
        panel._cmd_queue.put.assert_called_once_with(
            (
                "register_toggle",
                (
                    "auto_next_arena_battle",
                    "Arena",
                    True,
                    "Auto Next Battle",
                ),
            )
        )

    def test_toggle_reads_live_shared_state(self) -> None:
        panel = object.__new__(ControlPanel)
        panel._toggle_dict = {"auto_next_arena_battle": True}

        panel._toggle_dict["auto_next_arena_battle"] = False

        self.assertFalse(panel.get_toggle("auto_next_arena_battle"))

    def test_forbidden_skills_are_available_before_gui_processes_command(
        self,
    ) -> None:
        panel = object.__new__(ControlPanel)
        panel._skill_dict = {}
        panel._cmd_queue = Mock()

        panel.set_skills(
            {"Debuffs": ["imperil", "weaken"]},
            forbidden={"imperil"},
        )

        self.assertEqual(panel.get_forbidden_skills(), frozenset({"imperil"}))
        panel._cmd_queue.put.assert_called_once()


class NullControlPanelTests(unittest.TestCase):
    def test_headless_panel_preserves_toggle_and_skill_contract(self) -> None:
        panel = NullControlPanel()
        panel.register_toggle("arena", "Arena", default=True)
        panel.set_skills({"Debuffs": ["imperil"]}, {"imperil"})

        self.assertTrue(panel.get_toggle("arena"))
        self.assertFalse(panel.get_toggle("missing"))
        self.assertEqual(panel.get_forbidden_skills(), frozenset({"imperil"}))
        asyncio.run(panel.wait_if_paused())
        panel.destroy()


if __name__ == "__main__":
    unittest.main()

import asyncio
import unittest
from unittest.mock import Mock

from hvbattle import BaseControlPanel, ControlPanel, NullControlPanel
from hvbattle.control_panel import (
    _close_gui,
    _commit_integer_control,
    _invoke_callback,
    _parse_integer,
    _publish_boolean,
)


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

    def test_integer_default_is_available_before_gui_processes_command(self) -> None:
        panel = object.__new__(ControlPanel)
        panel._integer_dict = {}
        panel._cmd_queue = Mock()

        panel.register_integer(
            "lottery_target",
            "Lottery target",
            default=1_000,
            minimum=0,
            group="Between Battles",
        )

        self.assertEqual(panel.get_integer("lottery_target"), 1_000)
        panel._cmd_queue.put.assert_called_once_with(
            (
                "register_integer",
                (
                    "lottery_target",
                    "Lottery target",
                    1_000,
                    0,
                    None,
                    "Between Battles",
                ),
            )
        )

    def test_integer_parser_keeps_only_valid_values(self) -> None:
        self.assertEqual(_parse_integer("250", 0, 1_000), 250)
        self.assertIsNone(_parse_integer("", 0, 1_000))
        self.assertIsNone(_parse_integer("tickets", 0, 1_000))
        self.assertIsNone(_parse_integer("-1", 0, 1_000))
        self.assertIsNone(_parse_integer("1001", 0, 1_000))

    def test_integer_reads_only_committed_shared_state(self) -> None:
        panel = object.__new__(ControlPanel)
        panel._integer_dict = {"lottery_target": 1_000}

        panel._integer_dict["lottery_target"] = 500

        self.assertEqual(panel.get_integer("lottery_target"), 500)


class GuiCallbackTests(unittest.TestCase):
    def test_apply_commits_valid_integer_and_updates_visible_status(self) -> None:
        shared = {"target": 1_000}
        variable = Mock()
        variable.get.return_value = "500"
        entry = Mock()
        status = Mock()

        _commit_integer_control(
            shared,
            "target",
            variable,
            entry,
            status,
            0,
            None,
            "system-background",
        )

        self.assertEqual(shared["target"], 500)
        entry.config.assert_called_once_with(background="system-background")
        status.config.assert_called_once_with(text="Applied: 500")

    def test_invalid_apply_preserves_commit_and_displays_value_in_use(self) -> None:
        shared = {"target": 1_000}
        variable = Mock()
        variable.get.return_value = "not-a-number"
        entry = Mock()
        status = Mock()

        _commit_integer_control(
            shared,
            "target",
            variable,
            entry,
            status,
            0,
            None,
            "system-background",
        )

        self.assertEqual(shared["target"], 1_000)
        entry.config.assert_called_once_with(background="misty rose")
        status.config.assert_called_once_with(text="Invalid; using 1000")

    def test_checkbox_callback_publishes_immediately(self) -> None:
        shared = {"food": True}
        variable = Mock()
        variable.get.return_value = False

        _publish_boolean(shared, "food", variable)

        self.assertFalse(shared["food"])

    def test_return_binding_invokes_the_same_apply_callback(self) -> None:
        apply = Mock()

        _invoke_callback(apply, object())

        apply.assert_called_once_with()

    def test_window_close_sets_pause_before_destroying_gui(self) -> None:
        events: list[str] = []
        pause_flag = Mock()
        pause_flag.set.side_effect = lambda: events.append("pause")

        _close_gui(pause_flag, lambda: events.append("destroy"))

        self.assertEqual(events, ["pause", "destroy"])


class NullControlPanelTests(unittest.TestCase):
    def test_headless_panel_preserves_toggle_and_action_contract(self) -> None:
        panel = NullControlPanel()
        panel.register_toggle("arena", "Arena", default=True)
        panel.register_integer("lottery_target", "Lottery target", 1_000, minimum=0)
        panel.set_actions({"Debuffs": ["imperil"]}, {"imperil"})

        self.assertTrue(panel.get_toggle("arena"))
        self.assertFalse(panel.get_toggle("missing"))
        self.assertEqual(panel.get_integer("lottery_target"), 1_000)
        self.assertEqual(panel.get_disabled_actions(), frozenset({"imperil"}))
        asyncio.run(panel.wait_if_paused())
        panel.destroy()

    def test_integer_registration_rejects_invalid_ranges_and_types(self) -> None:
        panel = NullControlPanel()

        for default, minimum, maximum, error in (
            (None, None, None, TypeError),
            (True, 0, None, TypeError),
            (0, True, None, TypeError),
            (0, None, False, TypeError),
            (0, 2, 1, ValueError),
            (-1, 0, None, ValueError),
            (2, None, 1, ValueError),
        ):
            with (
                self.subTest(default=default, minimum=minimum, maximum=maximum),
                self.assertRaises(error),
            ):
                panel.register_integer(
                    "value",
                    "Value",
                    default,  # type: ignore[arg-type]
                    minimum=minimum,  # type: ignore[arg-type]
                    maximum=maximum,
                )

    def test_unknown_integer_name_is_not_silently_defaulted(self) -> None:
        with self.assertRaisesRegex(KeyError, "Unknown integer control"):
            NullControlPanel().get_integer("lottery_target")

    def test_legacy_subclass_does_not_need_integer_methods(self) -> None:
        class LegacyPanel(BaseControlPanel):
            def __init__(self) -> None:
                self.disabled = frozenset[str]()

            def set_title(self, title: str) -> None:
                del title

            def register_toggle(
                self,
                name: str,
                label: str,
                default: bool = False,
                *,
                group: str = "Options",
            ) -> None:
                del name, label, default, group

            def get_toggle(self, name: str) -> bool:
                del name
                return False

            def set_skills(self, skill_groups, forbidden) -> None:  # type: ignore[no-untyped-def]
                del skill_groups
                self.disabled = frozenset(forbidden)

            def get_forbidden_skills(self) -> frozenset[str]:
                return self.disabled

            async def wait_if_paused(self) -> None:
                return

            def destroy(self) -> None:
                return

        panel = LegacyPanel()
        panel.set_actions({"Items": ["mystic gem"]}, {"mystic gem"})

        self.assertEqual(panel.get_disabled_actions(), frozenset({"mystic gem"}))
        with self.assertRaises(NotImplementedError):
            panel.register_integer("target", "Target")


if __name__ == "__main__":
    unittest.main()

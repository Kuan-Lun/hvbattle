import asyncio
import unittest
from unittest.mock import Mock

from hvbattle import BaseControlPanel, ControlPanel, NullControlPanel
from hvbattle.control_panel import (
    _checklist_grid_position,
    _close_gui,
    _commit_integer_control,
    _invoke_callback,
    _parse_integer,
    _publish_boolean,
    _publish_checklist_selection,
    _set_paused,
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

    def test_checklist_selection_is_available_before_gui_command(self) -> None:
        panel = object.__new__(ControlPanel)
        panel._checklist_dict = {}
        panel._cmd_queue = Mock()

        panel.set_checklist(
            "ring",
            "Ring of Blood",
            (("first", "First Challenge"), ("second", "Second Challenge")),
            ("second", "first"),
            status="10 Tokens of Blood",
        )

        self.assertEqual(panel.get_checklist_selection("ring"), ("first", "second"))
        panel._cmd_queue.put.assert_called_once_with(
            (
                "set_checklist",
                (
                    "ring",
                    "Ring of Blood",
                    (
                        ("first", "First Challenge"),
                        ("second", "Second Challenge"),
                    ),
                    ("first", "second"),
                    "10 Tokens of Blood",
                ),
            )
        )

    def test_checklist_replacement_removes_stale_shared_selection(self) -> None:
        panel = object.__new__(ControlPanel)
        panel._checklist_dict = {}
        panel._cmd_queue = Mock()
        panel.set_checklist(
            "arena",
            "The Arena",
            (("old", "Old Challenge"), ("keep", "Kept Challenge")),
            ("old", "keep"),
        )

        panel.set_checklist(
            "arena",
            "The Arena",
            (("new", "New Challenge"), ("keep", "Kept Challenge")),
            ("keep",),
        )

        self.assertEqual(panel.get_checklist_selection("arena"), ("keep",))
        self.assertEqual(panel._cmd_queue.put.call_count, 2)
        self.assertEqual(
            panel._cmd_queue.put.call_args.args[0][1][2],
            (("new", "New Challenge"), ("keep", "Kept Challenge")),
        )

    def test_checklist_reads_one_live_atomic_tuple(self) -> None:
        panel = object.__new__(ControlPanel)
        panel._checklist_dict = {"arena": ("low", "high")}

        panel._checklist_dict["arena"] = ("high",)

        self.assertEqual(panel.get_checklist_selection("arena"), ("high",))

    def test_empty_checklist_has_an_empty_selection(self) -> None:
        panel = object.__new__(ControlPanel)
        panel._checklist_dict = {}
        panel._cmd_queue = Mock()

        panel.set_checklist(
            "arena",
            "The Arena",
            (),
            status="No currently available challenges",
        )

        self.assertEqual(panel.get_checklist_selection("arena"), ())
        self.assertEqual(
            panel._cmd_queue.put.call_args.args[0],
            (
                "set_checklist",
                (
                    "arena",
                    "The Arena",
                    (),
                    (),
                    "No currently available challenges",
                ),
            ),
        )

    def test_checklist_namespace_is_independent_from_other_controls(self) -> None:
        panel = object.__new__(ControlPanel)
        panel._toggle_dict = {}
        panel._integer_dict = {}
        panel._checklist_dict = {}
        panel._cmd_queue = Mock()

        panel.register_toggle("activity", "Activity", default=True)
        panel.register_integer("activity", "Activity count", default=3)
        panel.set_checklist(
            "activity",
            "Activity choices",
            (("challenge", "Challenge"),),
            ("challenge",),
        )

        self.assertTrue(panel.get_toggle("activity"))
        self.assertEqual(panel.get_integer("activity"), 3)
        self.assertEqual(panel.get_checklist_selection("activity"), ("challenge",))

    def test_programmatic_pause_blocks_until_pause_flag_is_cleared(self) -> None:
        async def exercise() -> None:
            panel = object.__new__(ControlPanel)
            panel._pause_flag = asyncio.Event()
            panel._cmd_queue = Mock()

            panel.pause()
            waiter = asyncio.create_task(panel.wait_if_paused())
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            panel._pause_flag.clear()
            await asyncio.wait_for(waiter, timeout=1)
            panel._cmd_queue.put.assert_called_once_with(("pause", None))

        asyncio.run(exercise())


class GuiCallbackTests(unittest.TestCase):
    def test_checklist_grid_starts_a_new_column_every_twelve_choices(self) -> None:
        self.assertEqual(_checklist_grid_position(0), (0, 0))
        self.assertEqual(_checklist_grid_position(11), (11, 0))
        self.assertEqual(_checklist_grid_position(12), (0, 1))
        self.assertEqual(_checklist_grid_position(23), (11, 1))
        self.assertEqual(_checklist_grid_position(24), (0, 2))

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

    def test_checklist_callback_publishes_one_ordered_tuple(self) -> None:
        shared = {"ring": ("first",)}
        first = Mock()
        second = Mock()
        third = Mock()
        first.get.return_value = False
        second.get.return_value = True
        third.get.return_value = True

        _publish_checklist_selection(
            shared,
            "ring",
            ("first", "second", "third"),
            (first, second, third),
        )

        self.assertEqual(shared["ring"], ("second", "third"))

    def test_pause_state_updates_flag_and_button_together(self) -> None:
        pause_flag = Mock()
        pause_button = Mock()

        _set_paused(pause_flag, pause_button, True)

        pause_flag.set.assert_called_once_with()
        pause_button.config.assert_called_once_with(text="Resume")

        _set_paused(pause_flag, pause_button, False)

        pause_flag.clear.assert_called_once_with()
        self.assertEqual(pause_button.config.call_args.args, ())
        self.assertEqual(pause_button.config.call_args.kwargs, {"text": "Pause"})

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
        panel.set_checklist(
            "ring",
            "Ring of Blood",
            (("triple", "Triple Trio and the Tree"),),
            ("triple",),
        )
        panel.set_actions({"Debuffs": ["imperil"]}, {"imperil"})

        self.assertTrue(panel.get_toggle("arena"))
        self.assertFalse(panel.get_toggle("missing"))
        self.assertEqual(panel.get_integer("lottery_target"), 1_000)
        self.assertEqual(panel.get_checklist_selection("ring"), ("triple",))
        self.assertEqual(panel.get_disabled_actions(), frozenset({"imperil"}))
        panel.pause()
        asyncio.run(panel.wait_if_paused())
        panel.destroy()

    def test_headless_checklist_replace_preserves_choice_order(self) -> None:
        panel = NullControlPanel()
        panel.set_checklist(
            "arena",
            "The Arena",
            (("low", "Low"), ("high", "High")),
            ("high", "low"),
        )

        self.assertEqual(panel.get_checklist_selection("arena"), ("low", "high"))

        panel.set_checklist(
            "arena",
            "The Arena",
            (("new", "New"), ("high", "High")),
            ("high",),
        )

        self.assertEqual(panel.get_checklist_selection("arena"), ("high",))

    def test_checklist_configuration_rejects_ambiguous_values(self) -> None:
        panel = NullControlPanel()

        invalid_cases = (
            ("", "Label", (), (), None, ValueError),
            ("name", "", (), (), None, ValueError),
            (
                "name",
                "Label",
                (("same", "First"), ("same", "Second")),
                (),
                None,
                ValueError,
            ),
            ("name", "Label", (("key", ""),), (), None, ValueError),
            (
                "name",
                "Label",
                (("key", "Choice"),),
                ("key", "key"),
                None,
                ValueError,
            ),
            (
                "name",
                "Label",
                (("key", "Choice"),),
                ("missing",),
                None,
                ValueError,
            ),
            ("name", "Label", (("key", "Choice"),), (), 1, TypeError),
            ("name", "Label", "key", (), None, TypeError),
            ("name", "Label", (("key", "Choice"),), "key", None, TypeError),
            ("name", "Label", (["key", "Choice"],), (), None, TypeError),
        )
        for name, label, choices, selected, status, error in invalid_cases:
            with (
                self.subTest(
                    name=name,
                    label=label,
                    choices=choices,
                    selected=selected,
                    status=status,
                ),
                self.assertRaises(error),
            ):
                panel.set_checklist(
                    name,
                    label,
                    choices,  # type: ignore[arg-type]
                    selected,  # type: ignore[arg-type]
                    status=status,  # type: ignore[arg-type]
                )

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

    def test_unknown_checklist_name_is_not_silently_defaulted(self) -> None:
        with self.assertRaisesRegex(KeyError, "Unknown checklist control"):
            NullControlPanel().get_checklist_selection("ring")

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
        with self.assertRaises(NotImplementedError):
            panel.set_checklist("challenges", "Challenges", ())
        with self.assertRaises(NotImplementedError):
            panel.get_checklist_selection("challenges")
        with self.assertRaises(NotImplementedError):
            panel.pause()


if __name__ == "__main__":
    unittest.main()

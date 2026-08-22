import asyncio
import socket
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from hvbattle import BaseControlPanel, ControlPanel, NullControlPanel
from hvbattle.control_panel import (
    _IPC_HEADER,
    ControlPanelProcessOwnershipError,
    _allocate_checklist_column,
    _checklist_choice_layout,
    _checklist_grid_position,
    _checklist_text_wraplength,
    _checklist_viewport_wraplength,
    _close_gui,
    _commit_integer_control,
    _ControlPanelLifecycle,
    _invoke_callback,
    _merge_checklist_replacement,
    _parse_gui_child_arguments,
    _parse_integer,
    _publish_boolean,
    _publish_checklist_selection,
    _receive_exact,
    _receive_frame,
    _render_pause_button,
    _run_gui_callback_fail_closed,
    _send_frame,
    _set_paused,
)


def _owned_panel(*, alive: bool = True) -> ControlPanel:
    panel = object.__new__(ControlPanel)
    panel._state = _ControlPanelLifecycle.OPEN
    panel._state_lock = threading.Lock()
    panel._rpc_lock = threading.Lock()
    panel._shutdown_lock = threading.Lock()
    panel._request_id = 0
    panel._channel = Mock(spec=socket.socket)
    panel._listener = None
    panel._process = Mock()
    panel._process.poll.return_value = None if alive else 1
    panel._process.shutdown.return_value = 0
    return panel


class ControlPanelLifecycleTests(unittest.TestCase):
    def test_invalid_aclose_deadline_does_not_poison_open_state(self) -> None:
        panel = _owned_panel()

        with self.assertRaisesRegex(ValueError, "finite monotonic"):
            asyncio.run(panel.aclose(expires_at=float("inf")))

        self.assertIs(panel._state, _ControlPanelLifecycle.OPEN)
        panel._process.shutdown.assert_not_called()

    def test_expired_aclose_deadline_rejects_cached_closed_state(self) -> None:
        panel = _owned_panel(alive=False)
        panel._state = _ControlPanelLifecycle.CLOSED

        with self.assertRaisesRegex(
            ControlPanelProcessOwnershipError,
            "before ownership proof",
        ):
            asyncio.run(panel.aclose(expires_at=time.monotonic() - 1))

    def test_aclose_deadline_includes_state_lock_wait(self) -> None:
        panel = _owned_panel()
        panel._state_lock.acquire()
        try:
            with self.assertRaisesRegex(
                ControlPanelProcessOwnershipError,
                "state ownership",
            ):
                asyncio.run(panel.aclose(expires_at=time.monotonic() + 0.01))
        finally:
            panel._state_lock.release()

        self.assertIs(panel._state, _ControlPanelLifecycle.OPEN)
        panel._process.shutdown.assert_not_called()

    def test_shutdown_lock_completion_after_deadline_is_rejected(self) -> None:
        panel = _owned_panel()
        clock = Mock(side_effect=(0.0, 0.0, 2.0))

        with self.assertRaisesRegex(
            ControlPanelProcessOwnershipError,
            "serialization completed after",
        ):
            panel._shutdown_owned(expires_at=1.0, clock=clock)

        panel._process.shutdown.assert_not_called()

    def test_aclose_rejects_cleanup_result_delivered_after_deadline(self) -> None:
        panel = _owned_panel()
        expires_at = time.monotonic() + 1.0

        with (
            patch(
                "hvbattle.control_panel._remaining",
                side_effect=(1.0, 1.0, 1.0, 0.0),
            ),
            patch.object(panel, "_shutdown_owned"),
            self.assertRaisesRegex(
                ControlPanelProcessOwnershipError,
                "completed after",
            ),
        ):
            asyncio.run(panel.aclose(expires_at=expires_at))

    def test_frame_send_rejects_completion_after_absolute_deadline(self) -> None:
        now = 0.0
        channel = Mock(spec=socket.socket)

        def finish_late(_frame: bytes) -> None:
            nonlocal now
            now = 2.0

        channel.sendall.side_effect = finish_late
        with self.assertRaisesRegex(TimeoutError, "completed after"):
            _send_frame(
                channel,
                ("request",),
                expires_at=1.0,
                clock=lambda: now,
            )

    def test_frame_receive_rejects_chunk_completed_after_deadline(self) -> None:
        now = 0.0
        channel = Mock(spec=socket.socket)

        def finish_late(_size: int) -> bytes:
            nonlocal now
            now = 2.0
            return b"x"

        channel.recv.side_effect = finish_late
        with self.assertRaisesRegex(TimeoutError, "completed after"):
            _receive_exact(
                channel,
                1,
                expires_at=1.0,
                clock=lambda: now,
            )

    def test_frame_receive_rejects_deserialize_completed_after_deadline(self) -> None:
        now = 0.0

        def decode_late(_payload: bytes) -> object:
            nonlocal now
            now = 2.0
            return ("response",)

        with (
            patch(
                "hvbattle.control_panel._receive_exact",
                side_effect=(_IPC_HEADER.pack(1), b"x"),
            ),
            patch(
                "hvbattle.control_panel.pickle.loads",
                side_effect=decode_late,
            ),
            self.assertRaisesRegex(TimeoutError, "decoded after"),
        ):
            _receive_frame(
                Mock(spec=socket.socket),
                expires_at=1.0,
                clock=lambda: now,
            )

    def test_child_cli_requires_a_valid_loopback_token(self) -> None:
        token = "ab" * 32
        self.assertEqual(
            _parse_gui_child_arguments(("41321", token)),
            (41_321, token),
        )
        for arguments in (("0", token), ("41321", "short"), ("41321", "zz" * 32)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                _parse_gui_child_arguments(arguments)

    @staticmethod
    def _startup_transport() -> tuple[Mock, Mock]:
        listener = Mock(spec=socket.socket)
        channel = Mock(spec=socket.socket)
        listener.getsockname.return_value = ("127.0.0.1", 41_321)
        listener.accept.return_value = (channel, ("127.0.0.1", 41_322))
        return listener, channel

    def test_startup_uses_supervised_module_process_and_authenticated_ipc(
        self,
    ) -> None:
        started_at = time.monotonic()
        listener, channel = self._startup_transport()
        owner = Mock()
        owner.poll.return_value = None
        owner.shutdown.return_value = 0
        auth_token = "ab" * 32

        with (
            patch("hvbattle.control_panel.socket.socket", return_value=listener),
            patch(
                "hvbattle.control_panel.secrets.token_hex",
                return_value=auth_token,
            ),
            patch(
                "hvbattle.control_panel.start_owned_process",
                return_value=owner,
            ) as start_process,
            patch(
                "hvbattle.control_panel._receive_frame",
                return_value=("ready", auth_token),
            ),
        ):
            panel = ControlPanel()

        parameters = start_process.call_args.args[1]
        self.assertEqual(parameters[0].rsplit("/", maxsplit=1)[-1], "control_panel.py")
        self.assertEqual(parameters[1:], ["41321", auth_token])
        self.assertLessEqual(start_process.call_args.kwargs["startup_timeout"], 5.0)
        self.assertLessEqual(
            start_process.call_args.kwargs["deadline"],
            started_at + 10.1,
        )
        self.assertGreater(
            start_process.call_args.kwargs["deadline"],
            started_at,
        )
        listener.bind.assert_called_once_with(("127.0.0.1", 0))
        listener.listen.assert_called_once_with(1)
        listener.close.assert_called_once_with()
        self.assertIs(panel._channel, channel)
        self.assertIs(panel._state, _ControlPanelLifecycle.OPEN)

        with (
            patch("hvbattle.control_panel._send_frame"),
            patch(
                "hvbattle.control_panel._receive_frame",
                return_value=("response", 1, True, None),
            ),
        ):
            panel.destroy()

    def test_startup_rejects_open_state_committed_after_work_deadline(self) -> None:
        listener, channel = self._startup_transport()
        owner = Mock()
        owner.poll.return_value = None
        auth_token = "ab" * 32

        with (
            patch("hvbattle.control_panel.socket.socket", return_value=listener),
            patch(
                "hvbattle.control_panel.secrets.token_hex",
                return_value=auth_token,
            ),
            patch(
                "hvbattle.control_panel.start_owned_process",
                return_value=owner,
            ),
            patch(
                "hvbattle.control_panel._receive_frame",
                return_value=("ready", auth_token),
            ),
            patch.object(ControlPanel, "_shutdown_owned") as shutdown,
            patch(
                "hvbattle.control_panel._remaining",
                side_effect=(5.0, 5.0, 5.0, 5.0, 0.0),
            ),
            self.assertRaisesRegex(TimeoutError, "work deadline"),
        ):
            ControlPanel()

        shutdown.assert_called_once()
        self.assertGreater(
            shutdown.call_args.kwargs["expires_at"],
            time.monotonic(),
        )
        listener.close.assert_called_once_with()
        channel.close.assert_not_called()

    def test_supervisor_start_failure_closes_listener_without_orphan_handle(
        self,
    ) -> None:
        listener, _channel = self._startup_transport()
        with (
            patch("hvbattle.control_panel.socket.socket", return_value=listener),
            patch(
                "hvbattle.control_panel.start_owned_process",
                side_effect=RuntimeError("start failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "start failed"),
        ):
            ControlPanel()

        listener.close.assert_called_once_with()

    def test_ready_failure_shuts_owner_down_inside_original_startup_deadline(
        self,
    ) -> None:
        listener, channel = self._startup_transport()
        owner = Mock()
        owner.poll.return_value = None
        owner.shutdown.return_value = 1
        started_at = time.monotonic()

        with (
            patch("hvbattle.control_panel.socket.socket", return_value=listener),
            patch(
                "hvbattle.control_panel.start_owned_process",
                return_value=owner,
            ) as start_process,
            patch("hvbattle.control_panel._send_frame") as send_frame,
            patch(
                "hvbattle.control_panel._receive_frame",
                side_effect=(
                    RuntimeError("ready failed"),
                    ("response", 1, True, None),
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "ready failed"),
        ):
            ControlPanel()

        self.assertEqual(send_frame.call_args.args[1], ("request", 1, "destroy", None))
        self.assertLessEqual(
            owner.shutdown.call_args.kwargs["deadline"], started_at + 10.1
        )
        self.assertEqual(
            owner.shutdown.call_args.kwargs["deadline"],
            start_process.call_args.kwargs["deadline"],
        )
        owner.shutdown.assert_called_once()
        channel.close.assert_called_once_with()
        listener.close.assert_called_once_with()

    def test_destroy_closes_only_after_owner_returns_exitcode_proof(self) -> None:
        panel = _owned_panel()
        owner = panel._process

        with (
            patch("hvbattle.control_panel._send_frame"),
            patch(
                "hvbattle.control_panel._receive_frame",
                return_value=("response", 1, True, None),
            ),
        ):
            panel.destroy()

        owner.shutdown.assert_called_once()
        self.assertIsNone(panel._process)
        self.assertIsNone(panel._channel)
        self.assertIs(panel._state, _ControlPanelLifecycle.CLOSED)

    def test_destroy_delegates_all_phases_to_one_owner_deadline(self) -> None:
        panel = _owned_panel()
        owner = panel._process
        started_at = time.monotonic()

        with (
            patch("hvbattle.control_panel._send_frame"),
            patch(
                "hvbattle.control_panel._receive_frame",
                return_value=("response", 1, True, None),
            ),
        ):
            panel.destroy()

        shutdown = owner.shutdown.call_args.kwargs
        self.assertEqual(shutdown["graceful_timeout"], 1.5)
        self.assertEqual(shutdown["terminate_timeout"], 1.0)
        self.assertEqual(shutdown["kill_timeout"], 1.0)
        self.assertEqual(shutdown["cleanup_timeout"], 1.0)
        self.assertLessEqual(shutdown["deadline"], started_at + 5.1)

    def test_unresolved_owner_is_retained_and_destroy_raises(self) -> None:
        panel = _owned_panel()
        owner = panel._process
        owner.shutdown.side_effect = RuntimeError("raw secret")

        with (
            patch("hvbattle.control_panel._send_frame"),
            patch(
                "hvbattle.control_panel._receive_frame",
                return_value=("response", 1, True, None),
            ),
            self.assertRaisesRegex(
                ControlPanelProcessOwnershipError,
                "process-tree-ownership-unresolved",
            ) as raised,
        ):
            panel.destroy()

        self.assertNotIn("secret", str(raised.exception))
        self.assertIs(panel._process, owner)
        self.assertIs(panel._state, _ControlPanelLifecycle.CLOSING)
        panel._channel.close.assert_not_called()

        with self.assertRaisesRegex(RuntimeError, "closing"):
            panel.is_paused()

    def test_destroy_can_retry_retained_owner(self) -> None:
        panel = _owned_panel()
        owner = panel._process
        owner.shutdown.side_effect = (RuntimeError("first failure"), 0)

        with (
            patch("hvbattle.control_panel._send_frame"),
            patch(
                "hvbattle.control_panel._receive_frame",
                side_effect=(
                    ("response", 1, True, None),
                    ("response", 2, True, None),
                ),
            ),
        ):
            with self.assertRaises(ControlPanelProcessOwnershipError):
                panel.destroy()
            panel.destroy()

        self.assertEqual(owner.shutdown.call_count, 2)
        self.assertIs(panel._state, _ControlPanelLifecycle.CLOSED)

    def test_aclose_finishes_ownership_proof_before_repeated_cancellation(
        self,
    ) -> None:
        async def exercise() -> None:
            panel = _owned_panel()
            started = threading.Event()
            release = threading.Event()
            completed = threading.Event()
            captured_deadlines: list[float] = []

            def close_owned(*, expires_at: float) -> None:
                captured_deadlines.append(expires_at)
                started.set()
                if not release.wait(timeout=1.0):
                    raise TimeoutError("test did not release cleanup")
                panel._process = None
                panel._channel = None
                with panel._state_lock:
                    panel._state = _ControlPanelLifecycle.CLOSED
                completed.set()

            called_at = time.monotonic()
            with patch.object(panel, "_shutdown_owned", side_effect=close_owned):
                closing = asyncio.create_task(panel.aclose())
                while not started.is_set():
                    await asyncio.sleep(0)
                closing.cancel()
                await asyncio.sleep(0)
                closing.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await closing

            self.assertTrue(completed.is_set())
            self.assertIs(panel._state, _ControlPanelLifecycle.CLOSED)
            self.assertLessEqual(captured_deadlines[0], called_at + 5.1)

        asyncio.run(exercise())

    def test_aclose_surfaces_ownership_failure_even_when_cancelled(self) -> None:
        async def exercise() -> None:
            panel = _owned_panel()
            started = threading.Event()
            release = threading.Event()

            def fail_close(*, expires_at: float) -> None:
                del expires_at
                started.set()
                if not release.wait(timeout=1.0):
                    raise TimeoutError("test did not release cleanup")
                raise ControlPanelProcessOwnershipError("owner unresolved")

            with patch.object(panel, "_shutdown_owned", side_effect=fail_close):
                closing = asyncio.create_task(panel.aclose())
                while not started.is_set():
                    await asyncio.sleep(0)
                closing.cancel()
                release.set()
                with self.assertRaisesRegex(
                    ControlPanelProcessOwnershipError,
                    "owner unresolved",
                ):
                    await closing

            self.assertIs(panel._state, _ControlPanelLifecycle.CLOSING)

        asyncio.run(exercise())

    def test_frames_round_trip_without_manager_or_queue(self) -> None:
        sender, receiver = socket.socketpair()
        try:
            expires_at = time.monotonic() + 1.0
            _send_frame(
                sender, ("request", 7, "is_paused", None), expires_at=expires_at
            )
            self.assertEqual(
                _receive_frame(receiver, expires_at=expires_at),
                ("request", 7, "is_paused", None),
            )
        finally:
            sender.close()
            receiver.close()

    def test_rpc_requires_matching_ack_and_marks_transport_failure_closing(
        self,
    ) -> None:
        panel = _owned_panel()
        with (
            patch("hvbattle.control_panel._send_frame") as send_frame,
            patch(
                "hvbattle.control_panel._receive_frame",
                return_value=("response", 99, True, False),
            ),
            self.assertRaisesRegex(
                ControlPanelProcessOwnershipError,
                "mismatched IPC",
            ),
        ):
            panel.get_toggle("arena")

        self.assertEqual(
            send_frame.call_args.args[1],
            ("request", 1, "get_toggle", "arena"),
        )
        self.assertIs(panel._state, _ControlPanelLifecycle.CLOSING)

    def test_rpc_rejects_ack_that_finishes_after_its_deadline(self) -> None:
        panel = _owned_panel()
        with (
            patch(
                "hvbattle.control_panel.time.monotonic",
                side_effect=(0.0, 0.0, 6.0),
            ),
            patch("hvbattle.control_panel._send_frame"),
            patch(
                "hvbattle.control_panel._receive_frame",
                return_value=("response", 1, True, False),
            ),
            self.assertRaisesRegex(
                ControlPanelProcessOwnershipError,
                "arrived after",
            ),
        ):
            panel.get_toggle("arena")

        self.assertIs(panel._state, _ControlPanelLifecycle.CLOSING)

    def test_remote_domain_error_does_not_forfeit_process_ownership(self) -> None:
        panel = _owned_panel()
        with (
            patch("hvbattle.control_panel._send_frame"),
            patch(
                "hvbattle.control_panel._receive_frame",
                return_value=(
                    "response",
                    1,
                    False,
                    ("KeyError", "Unknown integer control: lottery"),
                ),
            ),
            self.assertRaisesRegex(KeyError, "Unknown integer control"),
        ):
            panel.get_integer("lottery")

        self.assertIs(panel._state, _ControlPanelLifecycle.OPEN)


class ControlPanelStateTests(unittest.TestCase):
    def test_toggle_mutation_and_read_are_acknowledged(self) -> None:
        panel = _owned_panel()
        with patch.object(panel, "_rpc", side_effect=(None, True)) as rpc:
            panel.register_toggle("arena", "Arena", default=True, group="Campaign")
            self.assertTrue(panel.get_toggle("arena"))

        self.assertEqual(
            rpc.call_args_list,
            [
                call("register_toggle", ("arena", "Arena", True, "Campaign")),
                call("get_toggle", "arena"),
            ],
        )

    def test_integer_mutation_and_read_validate_rpc_types(self) -> None:
        panel = _owned_panel()
        with patch.object(panel, "_rpc", side_effect=(None, 1_000)) as rpc:
            panel.register_integer(
                "lottery_target",
                "Lottery target",
                default=1_000,
                minimum=0,
            )
            self.assertEqual(panel.get_integer("lottery_target"), 1_000)

        self.assertEqual(rpc.call_count, 2)

    def test_checklist_rpc_uses_normalized_selection_order(self) -> None:
        panel = _owned_panel()
        choices = (("first", "First"), ("second", "Second"))
        with patch.object(
            panel,
            "_rpc",
            side_effect=((1, ("first", "second")), (2, ("second",))),
        ) as rpc:
            panel.set_checklist(
                "ring",
                "Ring of Blood",
                choices,
                ("second", "first"),
            )
            self.assertEqual(panel.get_checklist_selection("ring"), ("second",))

        self.assertEqual(
            rpc.call_args_list,
            [
                call(
                    "set_checklist",
                    (
                        "ring",
                        "Ring of Blood",
                        choices,
                        ("first", "second"),
                        None,
                    ),
                ),
                call("get_checklist", "ring"),
            ],
        )

    def test_skill_mutation_and_read_are_acknowledged(self) -> None:
        panel = _owned_panel()
        groups = {"Debuffs": ["imperil", "weaken"]}
        with patch.object(
            panel,
            "_rpc",
            side_effect=(None, ("imperil",)),
        ) as rpc:
            panel.set_skills(groups, {"imperil"})
            self.assertEqual(panel.get_forbidden_skills(), frozenset({"imperil"}))

        self.assertEqual(rpc.call_count, 2)

    def test_programmatic_pause_waits_for_acknowledged_resume(self) -> None:
        async def exercise() -> None:
            panel = _owned_panel()
            with (
                patch.object(
                    panel,
                    "_rpc",
                    side_effect=(None, True, False),
                ) as rpc,
                patch("hvbattle.control_panel.asyncio.sleep", new_callable=AsyncMock),
                patch("hvbattle.control_panel.logger") as control_logger,
            ):
                panel.pause()
                await panel.wait_if_paused()

            self.assertEqual(
                rpc.call_args_list,
                [call("pause"), call("is_paused"), call("is_paused")],
            )
            self.assertEqual(
                control_logger.info.call_args_list,
                [
                    call("Battle control panel pause requested"),
                    call("Battle control panel is paused; waiting for Resume"),
                    call("Battle control panel resumed"),
                ],
            )

        asyncio.run(exercise())

    def test_invalid_rpc_value_fails_closed(self) -> None:
        panel = _owned_panel()
        with (
            patch.object(panel, "_rpc", return_value=object()),
            self.assertRaisesRegex(RuntimeError, "invalid pause state"),
        ):
            panel.is_paused()

        self.assertIs(panel._state, _ControlPanelLifecycle.CLOSING)


class GuiCallbackTests(unittest.TestCase):
    def test_checklist_replacement_preserves_live_common_choice(self) -> None:
        self.assertEqual(
            _merge_checklist_replacement(
                ("old", "keep"),
                ("old",),
                ("new", "keep"),
                ("new", "keep"),
            ),
            ("new",),
        )

    def test_integer_parser_rejects_partial_and_out_of_range_values(self) -> None:
        self.assertEqual(_parse_integer("250", 0, 1_000), 250)
        self.assertIsNone(_parse_integer("", 0, 1_000))
        self.assertIsNone(_parse_integer("tickets", 0, 1_000))
        self.assertIsNone(_parse_integer("-1", 0, 1_000))
        self.assertIsNone(_parse_integer("1001", 0, 1_000))

    def test_checklist_columns_stay_stable_across_frame_rebuilds(self) -> None:
        columns: dict[str, int] = {}

        self.assertEqual(_allocate_checklist_column(columns, "arena"), 0)
        self.assertEqual(_allocate_checklist_column(columns, "ring"), 1)
        self.assertEqual(_allocate_checklist_column(columns, "arena"), 0)
        self.assertEqual(columns, {"arena": 0, "ring": 1})

    def test_checklist_grid_keeps_all_choices_in_one_column(self) -> None:
        for index in range(25):
            with self.subTest(index=index):
                self.assertEqual(
                    _checklist_grid_position(index, rows_per_column=25),
                    (index, 0),
                )

    def test_dynamic_checklist_width_uses_remaining_screen_space(self) -> None:
        one_checklist = _checklist_text_wraplength(1_600, 600, 1, 1)
        two_checklists = _checklist_text_wraplength(1_600, 600, 2, 1)
        two_choice_columns = _checklist_text_wraplength(1_600, 600, 2, 2)

        self.assertGreater(one_checklist, two_checklists)
        self.assertGreater(two_checklists, two_choice_columns)

    def test_dynamic_checklist_width_is_bounded_for_extreme_names(self) -> None:
        self.assertEqual(_checklist_text_wraplength(10_000, 0, 1, 1), 420)
        self.assertEqual(_checklist_text_wraplength(800, 700, 2, 1), 1)

    def test_checklist_wraplength_tracks_the_allocated_viewport(self) -> None:
        self.assertEqual(_checklist_viewport_wraplength(500), 420)
        self.assertEqual(_checklist_viewport_wraplength(300), 252)
        self.assertEqual(_checklist_viewport_wraplength(48), 1)
        self.assertEqual(_checklist_viewport_wraplength(1), 1)

    def test_checklist_wraplength_rejects_invalid_viewport_width(self) -> None:
        with self.assertRaisesRegex(ValueError, "viewport_width must be positive"):
            _checklist_viewport_wraplength(0)

    def test_narrow_layout_keeps_one_choice_column(self) -> None:
        self.assertEqual(
            _checklist_choice_layout(1_366, 550, 2, 25),
            (1, 25, 296),
        )

    def test_wide_layout_keeps_one_choice_column(self) -> None:
        self.assertEqual(
            _checklist_choice_layout(1_920, 600, 2, 25),
            (1, 25, 420),
        )

    def test_empty_checklist_layout_has_one_placeholder_row(self) -> None:
        self.assertEqual(
            _checklist_choice_layout(1_366, 550, 2, 0),
            (1, 1, 296),
        )

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

    def test_checklist_callback_merges_one_choice_in_shared_order(self) -> None:
        shared = {"ring": (4, ("first", "second", "third"), ("first",))}
        variable = Mock()
        variable.get.return_value = True

        _publish_checklist_selection(
            shared,
            threading.RLock(),
            "ring",
            4,
            "third",
            variable,
        )

        self.assertEqual(
            shared["ring"],
            (5, ("first", "second", "third"), ("first", "third")),
        )

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

    def test_queued_pause_render_does_not_reapply_stale_state(self) -> None:
        pause_flag = threading.Event()
        pause_button = Mock()

        pause_flag.set()
        pause_flag.clear()
        _render_pause_button(pause_flag, pause_button)

        self.assertFalse(pause_flag.is_set())
        pause_button.config.assert_called_once_with(text="Pause")

        pause_button.reset_mock()
        pause_flag.set()
        _render_pause_button(pause_flag, pause_button)

        self.assertTrue(pause_flag.is_set())
        pause_button.config.assert_called_once_with(text="Resume")

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

    def test_scheduled_callback_failure_pauses_and_destroys_gui(self) -> None:
        events: list[str] = []
        pause_flag = Mock()
        pause_flag.set.side_effect = lambda: events.append("pause")

        def fail() -> None:
            events.append("callback")
            raise RuntimeError("scheduled callback failed")

        _run_gui_callback_fail_closed(
            pause_flag,
            lambda: events.append("destroy"),
            fail,
        )

        self.assertEqual(events, ["callback", "pause", "destroy"])

    def test_successful_scheduled_callback_keeps_gui_open(self) -> None:
        pause_flag = Mock()
        destroy = Mock()
        callback = Mock()

        _run_gui_callback_fail_closed(pause_flag, destroy, callback)

        callback.assert_called_once_with()
        pause_flag.set.assert_not_called()
        destroy.assert_not_called()


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
        self.assertFalse(panel.is_paused())
        panel.pause()
        self.assertFalse(panel.is_paused())
        asyncio.run(panel.wait_if_paused())
        asyncio.run(panel.aclose())
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

            def is_paused(self) -> bool:
                return False

            async def wait_if_paused(self) -> None:
                return

            async def aclose(self, *, expires_at: float | None = None) -> None:
                del expires_at
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

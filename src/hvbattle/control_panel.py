"""Reusable battle control panel primitives without campaign policy."""

from __future__ import annotations

import asyncio
import logging
import math
import pickle
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from enum import Enum, auto
from functools import partial
from pathlib import Path
from typing import Any, Final, cast

from hvbrowser.runtime import (
    OwnedProcess,
    ProcessOwnershipError,
    close_forwarded_logging,
    configure_forwarded_logging,
    start_owned_process,
)

logger = logging.getLogger("hvbattle.control_panel")

_GUI_START_TOTAL_TIMEOUT: Final = 10.0
_GUI_START_CLEANUP_RESERVE: Final = 5.0
_GUI_RPC_TIMEOUT: Final = 5.0
_GUI_SHUTDOWN_TOTAL_TIMEOUT: Final = 5.0
_GUI_DESTROY_RPC_TIMEOUT: Final = 0.5
_GUI_GRACEFUL_JOIN_TIMEOUT: Final = 1.5
_GUI_TERMINATE_JOIN_TIMEOUT: Final = 1.0
_GUI_KILL_JOIN_TIMEOUT: Final = 1.0
_GUI_OWNER_CLEANUP_TIMEOUT: Final = 1.0
_GUI_POLL_INTERVAL_MS: Final = 20
_IPC_HEADER: Final = struct.Struct("!I")
_IPC_MAX_PAYLOAD_BYTES: Final = 8 * 1024 * 1024
_CHECKLIST_TEXT_MAX_WRAP_LENGTH = 420
_CHECKLIST_TEXT_HORIZONTAL_OVERHEAD = 48
_CHECKLIST_CANVAS_HORIZONTAL_OVERHEAD = 24
_CHECKLIST_NON_CHOICE_VERTICAL_OVERHEAD = 160
_WINDOW_SCREEN_MARGIN = 80
_WINDOW_CONTENT_HORIZONTAL_OVERHEAD = 48
_CLEANUP_DESTROY_COMMAND = "destroy-command-failed"
_CLEANUP_PROCESS_ALIVE = "process-tree-ownership-unresolved"
_CLEANUP_PROCESS_CLOSE = "process-owner-shutdown-failed"
_CLEANUP_CHANNEL_CLOSE = "channel-close-failed"
_CLEANUP_LISTENER_CLOSE = "listener-close-failed"
_CLEANUP_RPC_SERIALIZATION = "rpc-serialization-failed"
_CLEANUP_STATE_SERIALIZATION = "state-serialization-failed"
_IPC_AUTH_TOKEN_BYTES: Final = 32


class ControlPanelProcessOwnershipError(ProcessOwnershipError):
    """The GUI child process could not be proven reaped and released."""


class _ControlPanelLifecycle(Enum):
    STARTING = auto()
    OPEN = auto()
    CLOSING = auto()
    CLOSED = auto()


def _remaining(expires_at: float, clock: Callable[[], float] = time.monotonic) -> float:
    return max(0.0, expires_at - clock())


def _encode_frame(message: object) -> bytes:
    payload = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    if len(payload) > _IPC_MAX_PAYLOAD_BYTES:
        raise ValueError("Battle control panel IPC payload is too large")
    return _IPC_HEADER.pack(len(payload)) + payload


def _send_frame(
    channel: socket.socket,
    message: object,
    *,
    expires_at: float,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    frame = _encode_frame(message)
    remaining = _remaining(expires_at, clock)
    if remaining <= 0:
        raise TimeoutError("Battle control panel IPC send deadline expired")
    channel.settimeout(remaining)
    channel.sendall(frame)
    if _remaining(expires_at, clock) <= 0:
        raise TimeoutError("Battle control panel IPC send completed after its deadline")


def _receive_exact(
    channel: socket.socket,
    size: int,
    *,
    expires_at: float,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        remaining = _remaining(expires_at, clock)
        if remaining <= 0:
            raise TimeoutError("Battle control panel IPC receive deadline expired")
        channel.settimeout(remaining)
        chunk = channel.recv(size - len(chunks))
        if not chunk:
            raise EOFError("Battle control panel IPC channel closed")
        chunks.extend(chunk)
        if _remaining(expires_at, clock) <= 0:
            raise TimeoutError(
                "Battle control panel IPC receive completed after its deadline"
            )
    return bytes(chunks)


def _receive_frame(
    channel: socket.socket,
    *,
    expires_at: float,
    clock: Callable[[], float] = time.monotonic,
) -> object:
    header = _receive_exact(
        channel,
        _IPC_HEADER.size,
        expires_at=expires_at,
        clock=clock,
    )
    (payload_size,) = _IPC_HEADER.unpack(header)
    if payload_size > _IPC_MAX_PAYLOAD_BYTES:
        raise RuntimeError("Battle control panel IPC payload is too large")
    payload = _receive_exact(
        channel,
        payload_size,
        expires_at=expires_at,
        clock=clock,
    )
    message = cast(object, pickle.loads(payload))
    if _remaining(expires_at, clock) <= 0:
        raise TimeoutError("Battle control panel IPC frame decoded after its deadline")
    return message


def _publish_boolean(shared: Any, name: str, variable: Any) -> None:
    shared[name] = bool(variable.get())


def _publish_checklist_selection(
    shared: Any,
    checklist_lock: Any,
    name: str,
    frame_revision: int,
    key: str,
    variable: Any,
) -> None:
    """Merge one checkbox change into the newest checklist generation."""
    checked = bool(variable.get())
    with checklist_lock:
        revision, keys, selected = _unpack_checklist_state(shared[name])
        if frame_revision > revision:
            raise RuntimeError("checklist frame revision is newer than shared state")
        if key not in keys:
            # The callback came from a frame replaced while the click was in
            # flight. Removed choices must never be reintroduced.
            return
        selected_set = set(selected)
        if checked:
            selected_set.add(key)
        else:
            selected_set.discard(key)
        merged = tuple(candidate for candidate in keys if candidate in selected_set)
        if merged != selected:
            shared[name] = (revision + 1, keys, merged)


def _unpack_checklist_state(
    state: Any,
) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    revision, keys, selected = state
    return int(revision), tuple(keys), tuple(selected)


def _merge_checklist_replacement(
    current_keys: tuple[str, ...],
    current_selected: tuple[str, ...],
    new_keys: tuple[str, ...],
    proposed_selected: tuple[str, ...],
) -> tuple[str, ...]:
    """Preserve live common choices while applying defaults only to new ones."""
    current_key_set = frozenset(current_keys)
    current_selected_set = frozenset(current_selected)
    proposed_selected_set = frozenset(proposed_selected)
    return tuple(
        key
        for key in new_keys
        if (
            key in current_selected_set
            if key in current_key_set
            else key in proposed_selected_set
        )
    )


def _checklist_grid_position(
    index: int,
    rows_per_column: int,
) -> tuple[int, int]:
    if rows_per_column <= 0:
        raise ValueError("rows_per_column must be positive")
    return index % rows_per_column, index // rows_per_column


def _checklist_text_wraplength(
    screen_width: int,
    controls_width: int,
    checklist_count: int,
    choice_column_count: int,
) -> int:
    """Fit dynamic checklist text beside the controls without unbounded width."""
    if choice_column_count <= 0:
        raise ValueError("choice_column_count must be positive")

    frame_width = _checklist_frame_width(
        screen_width,
        controls_width,
        checklist_count,
    )
    return _checklist_viewport_wraplength(max(1, frame_width // choice_column_count))


def _checklist_viewport_wraplength(viewport_width: int) -> int:
    """Bound text to one currently allocated checklist viewport."""
    if viewport_width <= 0:
        raise ValueError("viewport_width must be positive")
    return min(
        _CHECKLIST_TEXT_MAX_WRAP_LENGTH,
        max(1, viewport_width - _CHECKLIST_TEXT_HORIZONTAL_OVERHEAD),
    )


def _checklist_frame_width(
    screen_width: int,
    controls_width: int,
    checklist_count: int,
) -> int:
    if screen_width <= 0:
        raise ValueError("screen_width must be positive")
    if controls_width < 0:
        raise ValueError("controls_width must not be negative")
    if checklist_count <= 0:
        raise ValueError("checklist_count must be positive")

    available_width = max(
        screen_width
        - controls_width
        - _WINDOW_SCREEN_MARGIN
        - _WINDOW_CONTENT_HORIZONTAL_OVERHEAD,
        0,
    )
    return max(1, available_width // checklist_count)


def _checklist_choice_layout(
    screen_width: int,
    controls_width: int,
    checklist_count: int,
    choice_count: int,
) -> tuple[int, int, int]:
    """Return one vertical choice column with a dynamic text wrap length."""
    if choice_count < 0:
        raise ValueError("choice_count must not be negative")
    column_count = 1
    rows_per_column = max(1, choice_count)
    wraplength = _checklist_text_wraplength(
        screen_width,
        controls_width,
        checklist_count,
        column_count,
    )
    return column_count, rows_per_column, wraplength


def _allocate_checklist_column(columns: dict[str, int], name: str) -> int:
    """Keep a checklist in its first assigned column across frame rebuilds."""
    column = columns.get(name)
    if column is None:
        column = len(columns)
        columns[name] = column
    return column


def _commit_integer_control(
    shared: Any,
    name: str,
    variable: Any,
    entry: Any,
    status: Any,
    minimum: int | None,
    maximum: int | None,
    valid_background: str,
) -> None:
    value = _parse_integer(variable.get(), minimum, maximum)
    if value is None:
        applied = shared[name]
        entry.config(background="misty rose")
        status.config(text=f"Invalid; using {applied}")
        return
    shared[name] = value
    entry.config(background=valid_background)
    status.config(text=f"Applied: {value}")


def _close_gui(pause_flag: Any, destroy: Callable[[], None]) -> None:
    try:
        pause_flag.set()
    finally:
        destroy()


def _run_gui_callback_fail_closed(
    pause_flag: Any,
    destroy: Callable[[], None],
    callback: Callable[[], None],
) -> None:
    """Close the GUI and pause the parent if a scheduled callback fails."""
    try:
        callback()
    except Exception:
        _close_gui(pause_flag, destroy)


def _set_paused(pause_flag: Any, pause_button: Any, paused: bool) -> None:
    if paused:
        pause_flag.set()
        pause_button.config(text="Resume")
    else:
        pause_flag.clear()
        pause_button.config(text="Pause")


def _render_pause_button(pause_flag: Any, pause_button: Any) -> None:
    """Render the authoritative shared pause state without changing it."""
    pause_button.config(text="Resume" if pause_flag.is_set() else "Pause")


def _invoke_callback(callback: Callable[[], None], _event: Any) -> None:
    callback()


def _run_gui(channel: socket.socket, auth_token: str) -> None:
    """Run Tk and authoritative control state in one owned child process."""
    import tkinter as tk

    pause_flag = threading.Event()
    toggle_dict: dict[str, bool] = {}
    integer_dict: dict[str, int] = {}
    checklist_dict: dict[
        str,
        tuple[int, tuple[str, ...], tuple[str, ...]],
    ] = {}
    checklist_observed_revisions: dict[str, int] = {}
    checklist_lock = threading.RLock()
    action_dict: dict[str, bool] = {}

    root = tk.Tk()
    root.title("Battle Control Panel")
    root.minsize(width=300, height=0)
    root.maxsize(
        width=max(300, root.winfo_screenwidth() - _WINDOW_SCREEN_MARGIN),
        height=max(1, root.winfo_screenheight() - _WINDOW_SCREEN_MARGIN),
    )

    pause_button = tk.Button(root, text="Pause")
    pause_button.pack(padx=10, pady=5)

    body = tk.Frame(root)
    body.pack(padx=10, pady=5, fill="both", expand=True)
    body.rowconfigure(0, weight=1)

    controls_container = tk.Frame(body)
    controls_container.grid(row=0, column=0, sticky="nsew")
    action_container = tk.Frame(controls_container)
    action_container.pack(pady=(0, 5), fill="x")
    toggle_container = tk.Frame(controls_container)
    toggle_container.pack(pady=(5, 0), fill="x")

    checklist_container = tk.Frame(body)
    checklist_container.grid(row=0, column=1, padx=(5, 0), sticky="nsew")
    checklist_container.rowconfigure(0, weight=1)
    body.columnconfigure(1, weight=1)

    local_actions: dict[str, tk.BooleanVar] = {}
    local_toggles: dict[str, tk.BooleanVar] = {}
    toggle_groups: dict[str, tk.LabelFrame] = {}
    checklist_frames: dict[str, tk.LabelFrame] = {}
    # Frames are rebuilt whenever fresh server choices arrive, so their
    # first-seen outer columns must live independently of the frame objects.
    checklist_columns: dict[str, int] = {}
    checklist_spanning_widgets: dict[str, tuple[Any, ...]] = {}
    checklist_choice_widgets: dict[str, tuple[Any, ...]] = {}
    checklist_choice_canvases: dict[str, Any] = {}
    checklist_choice_containers: dict[str, Any] = {}
    checklist_choice_windows: dict[str, int] = {}
    checklist_choice_scrollbars: dict[str, Any] = {}
    checklist_reflow_pending = False
    checklist_viewport_refresh_pending = False

    def group_frame(group: str) -> tk.LabelFrame:
        frame = toggle_groups.get(group)
        if frame is None:
            frame = tk.LabelFrame(toggle_container, text=group)
            frame.pack(side="left", padx=5, pady=3, fill="both", expand=True)
            toggle_groups[group] = frame
        return frame

    def toggle_pause() -> None:
        _set_paused(pause_flag, pause_button, not pause_flag.is_set())

    def reflow_checklists_impl() -> None:
        nonlocal checklist_reflow_pending
        checklist_reflow_pending = False
        if not checklist_frames:
            return

        # A command burst can create the left controls and both checklists in
        # one callback. Flush geometry before budgeting the dynamic labels.
        root.update_idletasks()
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        controls_width = controls_container.winfo_reqwidth()
        checklist_count = len(checklist_frames)
        frame_width = _checklist_frame_width(
            screen_width,
            controls_width,
            checklist_count,
        )
        spanning_wraplength = _checklist_text_wraplength(
            screen_width,
            controls_width,
            checklist_count,
            1,
        )
        for name in checklist_frames:
            choice_layout = _checklist_choice_layout(
                screen_width,
                controls_width,
                checklist_count,
                len(checklist_choice_widgets[name]),
            )
            rows_per_column = choice_layout[1]
            choice_wraplength = choice_layout[2]
            for index, widget in enumerate(checklist_choice_widgets[name]):
                grid_row, grid_column = _checklist_grid_position(
                    index,
                    rows_per_column,
                )
                widget.grid_configure(row=grid_row, column=grid_column)
                widget.config(wraplength=choice_wraplength)
            for widget in checklist_spanning_widgets[name]:
                widget.config(wraplength=spanning_wraplength)

        root.update_idletasks()
        # Only the choice area scrolls; titles, status, and Pause remain visible.
        max_canvas_height = max(
            1,
            screen_height
            - _WINDOW_SCREEN_MARGIN
            - _CHECKLIST_NON_CHOICE_VERTICAL_OVERHEAD,
        )
        max_canvas_width = max(
            1,
            frame_width - _CHECKLIST_CANVAS_HORIZONTAL_OVERHEAD,
        )
        for name, canvas in checklist_choice_canvases.items():
            choices_container = checklist_choice_containers[name]
            content_width = choices_container.winfo_reqwidth()
            content_height = choices_container.winfo_reqheight()
            canvas.config(
                width=min(content_width, max_canvas_width),
                height=min(content_height, max_canvas_height),
                scrollregion=(0, 0, content_width, content_height),
            )

        refresh_checklist_viewports_impl()

    def reflow_checklists() -> None:
        _run_gui_callback_fail_closed(
            pause_flag,
            root.destroy,
            reflow_checklists_impl,
        )

    def schedule_checklist_reflow() -> None:
        nonlocal checklist_reflow_pending
        if checklist_reflow_pending:
            return
        checklist_reflow_pending = True
        root.after(20, reflow_checklists)

    def refresh_checklist_viewports_impl() -> None:
        nonlocal checklist_viewport_refresh_pending
        checklist_viewport_refresh_pending = False
        if not checklist_frames:
            return

        # The window manager may allocate less space than Tk requested. Wrap
        # against the final frame and canvas widths so resizing never exposes
        # a hidden horizontal overflow.
        root.update_idletasks()
        for name, frame in checklist_frames.items():
            canvas = checklist_choice_canvases[name]
            frame_width = max(1, frame.winfo_width())
            canvas_width = max(1, canvas.winfo_width())
            spanning_wraplength = _checklist_viewport_wraplength(frame_width)
            choice_wraplength = _checklist_viewport_wraplength(canvas_width)
            for widget in checklist_spanning_widgets[name]:
                widget.config(wraplength=spanning_wraplength)
            for widget in checklist_choice_widgets[name]:
                widget.config(wraplength=choice_wraplength)
            canvas.itemconfigure(
                checklist_choice_windows[name],
                width=canvas_width,
            )

        root.update_idletasks()
        for name, canvas in checklist_choice_canvases.items():
            content_height = checklist_choice_containers[name].winfo_reqheight()
            canvas_width = max(1, canvas.winfo_width())
            canvas.config(
                scrollregion=(0, 0, canvas_width, content_height),
            )
            scrollbar = checklist_choice_scrollbars[name]
            if content_height > canvas.winfo_height():
                scrollbar.grid()
            else:
                scrollbar.grid_remove()

    def refresh_checklist_viewports() -> None:
        _run_gui_callback_fail_closed(
            pause_flag,
            root.destroy,
            refresh_checklist_viewports_impl,
        )

    def schedule_checklist_viewport_refresh(event: Any = None) -> None:
        nonlocal checklist_viewport_refresh_pending
        if (
            event is not None
            and event.widget is not root
            and not any(
                event.widget is canvas for canvas in checklist_choice_canvases.values()
            )
        ):
            return
        if checklist_viewport_refresh_pending:
            return
        checklist_viewport_refresh_pending = True
        root.after(20, refresh_checklist_viewports)

    def handle_command(command: str, arguments: Any) -> object:
        match command:
            case "register_toggle":
                name, label, default, group = arguments
                frame = group_frame(group)
                toggle_variable = tk.BooleanVar(value=default)
                local_toggles[name] = toggle_variable
                toggle_dict[name] = default
                tk.Checkbutton(
                    frame,
                    text=label,
                    variable=toggle_variable,
                    command=partial(
                        _publish_boolean,
                        toggle_dict,
                        name,
                        toggle_variable,
                    ),
                ).pack(anchor="w", padx=5, pady=1)
                schedule_checklist_reflow()
            case "get_toggle":
                return bool(toggle_dict.get(arguments, False))
            case "register_integer":
                name, label, default, minimum, maximum, group = arguments
                frame = group_frame(group)
                row = tk.Frame(frame)
                row.pack(anchor="w", padx=5, pady=1, fill="x")
                tk.Label(row, text=label).pack(side="left")
                integer_variable = tk.StringVar(value=str(default))
                integer_dict[name] = default
                entry = tk.Entry(row, textvariable=integer_variable, width=10)
                entry.pack(side="right", padx=(5, 0))
                normal_background = entry.cget("background")
                status = tk.Label(row, text=f"Applied: {default}")
                status.pack(side="right", padx=(5, 0))

                apply_integer = partial(
                    _commit_integer_control,
                    integer_dict,
                    name,
                    integer_variable,
                    entry,
                    status,
                    minimum,
                    maximum,
                    normal_background,
                )

                tk.Button(row, text="Apply", command=apply_integer).pack(
                    side="right", padx=(5, 0)
                )

                entry.bind("<Return>", partial(_invoke_callback, apply_integer))
                schedule_checklist_reflow()
            case "get_integer":
                try:
                    return integer_dict[arguments]
                except KeyError:
                    raise KeyError(f"Unknown integer control: {arguments}") from None
            case "set_checklist":
                name, label, choices, proposed_selected, status = arguments
                keys = tuple(key for key, _choice_label in choices)
                with checklist_lock:
                    current_state = checklist_dict.get(name)
                    if current_state is None:
                        frame_revision = 1
                        selected = proposed_selected
                    else:
                        current_revision, current_keys, current_selected = (
                            _unpack_checklist_state(current_state)
                        )
                        frame_revision = current_revision + 1
                        if checklist_observed_revisions.get(name) == current_revision:
                            selected = proposed_selected
                        else:
                            selected = _merge_checklist_replacement(
                                current_keys,
                                current_selected,
                                keys,
                                proposed_selected,
                            )
                    checklist_dict[name] = (frame_revision, keys, selected)
                    checklist_observed_revisions[name] = frame_revision
                old_frame = checklist_frames.pop(name, None)
                if old_frame is not None:
                    old_frame.destroy()

                frame = tk.LabelFrame(checklist_container)
                title = tk.Label(
                    frame,
                    text=label,
                    anchor="w",
                    justify="left",
                    wraplength=_CHECKLIST_TEXT_MAX_WRAP_LENGTH,
                )
                frame.config(labelwidget=title)
                column = _allocate_checklist_column(checklist_columns, name)
                frame.grid(
                    row=0,
                    column=column,
                    padx=5,
                    pady=3,
                    sticky="nsew",
                )
                checklist_container.columnconfigure(
                    column,
                    weight=1,
                    uniform="checklists",
                )
                checklist_frames[name] = frame

                spanning_widgets: list[Any] = [title]
                if status is not None:
                    status_label = tk.Label(
                        frame,
                        text=status,
                        anchor="w",
                        justify="left",
                        wraplength=_CHECKLIST_TEXT_MAX_WRAP_LENGTH,
                    )
                    status_label.pack(anchor="w", padx=5, pady=(2, 1))
                    spanning_widgets.append(status_label)
                choices_viewport = tk.Frame(frame)
                choices_viewport.pack(fill="both", expand=True)
                choices_viewport.rowconfigure(0, weight=1)
                choices_viewport.columnconfigure(0, weight=1)
                canvas = tk.Canvas(
                    choices_viewport,
                    width=1,
                    height=1,
                    borderwidth=0,
                    highlightthickness=0,
                    background=frame.cget("background"),
                )
                canvas.grid(row=0, column=0, sticky="nsew")
                scrollbar = tk.Scrollbar(
                    choices_viewport,
                    orient="vertical",
                    command=canvas.yview,
                )
                scrollbar.grid(row=0, column=1, sticky="ns")
                canvas.config(yscrollcommand=scrollbar.set)
                choices_container = tk.Frame(
                    canvas,
                    background=frame.cget("background"),
                )
                choice_window = canvas.create_window(
                    (0, 0),
                    window=choices_container,
                    anchor="nw",
                )
                canvas.bind(
                    "<Configure>",
                    schedule_checklist_viewport_refresh,
                )

                selected_keys = frozenset(selected)
                variables = tuple(
                    tk.BooleanVar(value=key in selected_keys) for key in keys
                )
                choice_widgets: list[Any] = []
                if not choices:
                    empty_label = tk.Label(
                        choices_container,
                        text="No choices available",
                        anchor="w",
                        justify="left",
                        wraplength=_CHECKLIST_TEXT_MAX_WRAP_LENGTH,
                    )
                    empty_label.pack(anchor="w", padx=5, pady=1)
                    spanning_widgets.append(empty_label)
                for index, ((_key, choice_label), variable) in enumerate(
                    zip(choices, variables, strict=True)
                ):
                    checkbox = tk.Checkbutton(
                        choices_container,
                        text=choice_label,
                        variable=variable,
                        anchor="w",
                        justify="left",
                        wraplength=_CHECKLIST_TEXT_MAX_WRAP_LENGTH,
                        command=partial(
                            _publish_checklist_selection,
                            checklist_dict,
                            checklist_lock,
                            name,
                            frame_revision,
                            _key,
                            variable,
                        ),
                    )
                    choice_widgets.append(checkbox)
                    grid_row, grid_column = _checklist_grid_position(
                        index,
                        max(1, len(choices)),
                    )
                    checkbox.grid(
                        row=grid_row,
                        column=grid_column,
                        sticky="w",
                        padx=5,
                        pady=1,
                    )
                checklist_spanning_widgets[name] = tuple(spanning_widgets)
                checklist_choice_widgets[name] = tuple(choice_widgets)
                checklist_choice_canvases[name] = canvas
                checklist_choice_containers[name] = choices_container
                checklist_choice_windows[name] = choice_window
                checklist_choice_scrollbars[name] = scrollbar
                schedule_checklist_reflow()
                return frame_revision, selected
            case "get_checklist":
                try:
                    revision, _keys, selected = _unpack_checklist_state(
                        checklist_dict[arguments]
                    )
                except KeyError:
                    raise KeyError(f"Unknown checklist control: {arguments}") from None
                checklist_observed_revisions[arguments] = revision
                return revision, selected
            case "set_actions":
                action_groups, disabled = arguments
                for widget in action_container.winfo_children():
                    widget.destroy()
                local_actions.clear()
                action_dict.clear()
                for column, (group_name, actions) in enumerate(action_groups.items()):
                    frame = tk.LabelFrame(action_container, text=group_name)
                    frame.grid(row=0, column=column, padx=5, pady=3, sticky="nsew")
                    for action in actions:
                        action_variable = tk.BooleanVar(value=action not in disabled)
                        local_actions[action] = action_variable
                        action_dict[action] = action_variable.get()
                        tk.Checkbutton(
                            frame,
                            text=action,
                            variable=action_variable,
                            command=partial(
                                _publish_boolean,
                                action_dict,
                                action,
                                action_variable,
                            ),
                        ).pack(anchor="w", padx=5, pady=1)
                    action_container.columnconfigure(column, weight=1)
                schedule_checklist_reflow()
            case "get_disabled_actions":
                return tuple(
                    name for name, enabled in action_dict.items() if not enabled
                )
            case "set_title":
                root.title(arguments)
            case "pause":
                pause_flag.set()
                _render_pause_button(pause_flag, pause_button)
            case "is_paused":
                return pause_flag.is_set()
            case "destroy":
                pause_flag.set()
            case _:
                raise RuntimeError("Unknown battle control panel command")
        return None

    incoming = bytearray()
    outgoing = bytearray()
    destroy_requested = False

    def queue_message(message: object) -> None:
        outgoing.extend(_encode_frame(message))

    def flush_messages() -> None:
        while outgoing:
            try:
                sent = channel.send(outgoing)
            except BlockingIOError:
                return
            if sent <= 0:
                raise EOFError("Battle control panel IPC channel closed")
            del outgoing[:sent]

    def dispatch_message(message: object) -> None:
        nonlocal destroy_requested
        match message:
            case ("request", int() as request_id, str() as command, arguments):
                if request_id <= 0:
                    raise RuntimeError("Invalid battle control panel request id")
            case _:
                raise RuntimeError("Invalid battle control panel IPC request")
        try:
            result = handle_command(command, arguments)
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            queue_message(
                (
                    "response",
                    request_id,
                    False,
                    (type(error).__name__, str(error)),
                )
            )
        else:
            queue_message(("response", request_id, True, result))
            if command == "destroy":
                destroy_requested = True

    def drain_messages() -> None:
        while True:
            try:
                chunk = channel.recv(65_536)
            except BlockingIOError:
                break
            if not chunk:
                raise EOFError("Battle control panel parent closed its IPC channel")
            incoming.extend(chunk)

        while len(incoming) >= _IPC_HEADER.size:
            (payload_size,) = _IPC_HEADER.unpack(incoming[: _IPC_HEADER.size])
            if payload_size > _IPC_MAX_PAYLOAD_BYTES:
                raise RuntimeError("Battle control panel IPC payload is too large")
            frame_size = _IPC_HEADER.size + payload_size
            if len(incoming) < frame_size:
                return
            payload = bytes(incoming[_IPC_HEADER.size : frame_size])
            del incoming[:frame_size]
            dispatch_message(cast(object, pickle.loads(payload)))

    def poll_commands_impl() -> None:
        drain_messages()
        flush_messages()
        if destroy_requested and not outgoing:
            root.destroy()
            return
        root.after(_GUI_POLL_INTERVAL_MS, poll_commands)

    def poll_commands() -> None:
        _run_gui_callback_fail_closed(
            pause_flag,
            root.destroy,
            poll_commands_impl,
        )

    pause_button.config(command=toggle_pause)
    # Losing the only interactive control surface must fail safe. The current
    # operation may finish, but the parent pauses at its next gate.
    root.protocol(
        "WM_DELETE_WINDOW",
        partial(_close_gui, pause_flag, root.destroy),
    )
    root.bind("<Configure>", schedule_checklist_viewport_refresh)
    channel.setblocking(False)
    queue_message(("ready", auth_token))
    flush_messages()
    root.after(_GUI_POLL_INTERVAL_MS, poll_commands)
    try:
        root.mainloop()
    finally:
        pause_flag.set()
        channel.close()


class BaseControlPanel(ABC):
    """Common contract for interactive and headless battle controls."""

    @abstractmethod
    def set_title(self, title: str) -> None: ...

    @abstractmethod
    def register_toggle(
        self,
        name: str,
        label: str,
        default: bool = False,
        *,
        group: str = "Options",
    ) -> None: ...

    @abstractmethod
    def get_toggle(self, name: str) -> bool: ...

    def register_integer(
        self,
        name: str,
        label: str,
        default: int = 0,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
        group: str = "Options",
    ) -> None:
        """Register an integer control when the implementation supports it."""
        raise NotImplementedError("This control panel has no integer controls")

    def get_integer(self, name: str) -> int:
        """Return a committed integer value when supported."""
        raise NotImplementedError("This control panel has no integer controls")

    def set_checklist(
        self,
        name: str,
        label: str,
        choices: Iterable[tuple[str, str]],
        selected: Iterable[str] = (),
        *,
        status: str | None = None,
    ) -> None:
        """Replace a named checklist when the implementation supports it."""
        raise NotImplementedError("This control panel has no checklist controls")

    def get_checklist_selection(self, name: str) -> tuple[str, ...]:
        """Return one ordered, atomically committed checklist selection."""
        raise NotImplementedError("This control panel has no checklist controls")

    def pause(self) -> None:
        """Request a pause when the implementation supports interaction."""
        raise NotImplementedError(
            "This control panel cannot be paused programmatically"
        )

    @abstractmethod
    def set_actions(
        self, action_groups: dict[str, list[str]], disabled: Iterable[str]
    ) -> None: ...

    @abstractmethod
    def get_disabled_actions(self) -> frozenset[str]: ...

    @abstractmethod
    def is_paused(self) -> bool:
        """Return the committed pause state without blocking."""

    @abstractmethod
    async def wait_if_paused(self) -> None: ...

    @abstractmethod
    async def aclose(self, *, expires_at: float | None = None) -> None:
        """Close owned resources without abandoning cleanup on cancellation."""

    @abstractmethod
    def destroy(self) -> None: ...


class ControlPanel(BaseControlPanel):
    """Synchronous RPC facade for one exclusively owned Tk child process."""

    def __init__(self) -> None:
        self._state = _ControlPanelLifecycle.STARTING
        self._state_lock = threading.Lock()
        self._rpc_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._request_id = 0
        self._channel: socket.socket | None = None
        self._listener: socket.socket | None = None
        self._process: OwnedProcess | None = None
        startup_expires_at = time.monotonic() + _GUI_START_TOTAL_TIMEOUT
        try:
            work_expires_at = startup_expires_at - _GUI_START_CLEANUP_RESERVE
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener = listener
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = int(listener.getsockname()[1])
            auth_token = secrets.token_hex(_IPC_AUTH_TOKEN_BYTES)
            startup_timeout = _remaining(work_expires_at)
            if startup_timeout <= 0:
                raise TimeoutError("Battle control panel startup deadline expired")
            self._process = start_owned_process(
                sys.executable,
                [
                    str(Path(__file__).with_name("control_panel.py")),
                    str(port),
                    auth_token,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                forward_logging=True,
                startup_timeout=max(sys.float_info.epsilon, startup_timeout),
                deadline=startup_expires_at,
            )
            accept_timeout = _remaining(work_expires_at)
            if accept_timeout <= 0:
                raise TimeoutError("Battle control panel startup deadline expired")
            listener.settimeout(accept_timeout)
            self._channel, peer = listener.accept()
            if _remaining(work_expires_at) <= 0:
                raise TimeoutError(
                    "Battle control panel accept completed after its startup deadline"
                )
            if peer[0] != "127.0.0.1":
                raise RuntimeError("Battle control panel IPC peer is not local")
            if _receive_frame(self._channel, expires_at=work_expires_at) != (
                "ready",
                auth_token,
            ):
                raise RuntimeError("Battle control panel returned invalid startup IPC")
            if self._process.poll() is not None:
                raise RuntimeError("Battle control panel exited during startup")
            if _remaining(work_expires_at) <= 0:
                raise TimeoutError(
                    "Battle control panel READY arrived after its startup deadline"
                )
            listener.close()
            self._listener = None
            with self._state_lock:
                self._state = _ControlPanelLifecycle.OPEN
            if _remaining(work_expires_at) <= 0:
                raise TimeoutError(
                    "Battle control panel startup completed after its work deadline"
                )
        except BaseException as startup_error:
            self._mark_closing()
            try:
                self._shutdown_owned(expires_at=startup_expires_at)
            except ControlPanelProcessOwnershipError as ownership_error:
                raise ownership_error from startup_error
            raise

    def _mark_closing(self) -> None:
        with self._state_lock:
            if self._state is not _ControlPanelLifecycle.CLOSED:
                self._state = _ControlPanelLifecycle.CLOSING

    def _require_open_locked(self) -> tuple[socket.socket, OwnedProcess]:
        with self._state_lock:
            state = self._state
        if state is _ControlPanelLifecycle.CLOSED:
            raise RuntimeError("Battle control panel has been destroyed")
        if state is not _ControlPanelLifecycle.OPEN:
            raise RuntimeError("Battle control panel is closing")
        channel = self._channel
        process = self._process
        if channel is None or process is None:
            self._mark_closing()
            raise ControlPanelProcessOwnershipError(
                "Battle control panel lost its child-process owner"
            )
        try:
            returncode = process.poll()
        except BaseException as error:
            self._mark_closing()
            raise ControlPanelProcessOwnershipError(
                "Battle control panel child state is unavailable"
            ) from error
        if returncode is not None:
            self._mark_closing()
            raise ControlPanelProcessOwnershipError(
                "Battle control panel child process is not running"
            )
        return channel, process

    @staticmethod
    def _raise_remote_error(error: object) -> None:
        match error:
            case ("KeyError", str() as message):
                raise KeyError(message)
            case ("TypeError", str() as message):
                raise TypeError(message)
            case ("ValueError", str() as message):
                raise ValueError(message)
            case ("RuntimeError", str() as message):
                raise RuntimeError(message)
            case _:
                raise RuntimeError("Battle control panel returned invalid error IPC")

    def _rpc(self, command: str, arguments: object = None) -> object:
        """Run one serialized GUI request within one five-second deadline."""

        expires_at = time.monotonic() + _GUI_RPC_TIMEOUT
        acquired = self._rpc_lock.acquire(timeout=_remaining(expires_at))
        if not acquired:
            self._mark_closing()
            raise ControlPanelProcessOwnershipError(
                "Battle control panel RPC serialization deadline expired"
            )
        try:
            channel, _process = self._require_open_locked()
            self._request_id += 1
            request_id = self._request_id
            try:
                _send_frame(
                    channel,
                    ("request", request_id, command, arguments),
                    expires_at=expires_at,
                )
                response = _receive_frame(channel, expires_at=expires_at)
            except (EOFError, OSError, TimeoutError) as error:
                self._mark_closing()
                raise ControlPanelProcessOwnershipError(
                    "Battle control panel RPC did not receive a bounded ACK"
                ) from error
            match response:
                case ("response", response_id, bool() as succeeded, result) if (
                    response_id == request_id
                ):
                    pass
                case _:
                    self._mark_closing()
                    raise ControlPanelProcessOwnershipError(
                        "Battle control panel returned mismatched IPC"
                    )
            if not succeeded:
                self._raise_remote_error(result)
            if _remaining(expires_at) <= 0:
                self._mark_closing()
                raise ControlPanelProcessOwnershipError(
                    "Battle control panel RPC ACK arrived after its deadline"
                )
            return result
        finally:
            self._rpc_lock.release()

    def _shutdown_owned(
        self,
        *,
        expires_at: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Close IPC only after the supervisor proves process-tree cleanup."""

        active_clock = clock or time.monotonic
        reasons: list[str] = []

        def record(reason: str) -> None:
            if reason not in reasons:
                reasons.append(reason)

        def remaining() -> float:
            return _remaining(expires_at, active_clock)

        if remaining() <= 0:
            raise ControlPanelProcessOwnershipError(
                "Battle control panel shutdown deadline expired before ownership proof"
            )
        shutdown_acquired = self._shutdown_lock.acquire(timeout=remaining())
        if not shutdown_acquired:
            raise ControlPanelProcessOwnershipError(
                "Battle control panel shutdown serialization deadline expired"
            )

        rpc_acquired = False
        try:
            if remaining() <= 0:
                raise ControlPanelProcessOwnershipError(
                    "Battle control panel shutdown serialization completed after its "
                    "deadline"
                )
            state_acquired = self._state_lock.acquire(timeout=remaining())
            if not state_acquired:
                raise ControlPanelProcessOwnershipError(
                    "Battle control panel state serialization deadline expired"
                )
            try:
                if remaining() <= 0:
                    raise ControlPanelProcessOwnershipError(
                        "Battle control panel state serialization completed after its "
                        "shutdown deadline"
                    )
                if self._state is _ControlPanelLifecycle.CLOSED:
                    return
                self._state = _ControlPanelLifecycle.CLOSING
            finally:
                self._state_lock.release()

            listener = self._listener
            if listener is not None:
                try:
                    listener.close()
                except BaseException:
                    record(_CLEANUP_LISTENER_CLOSE)
                else:
                    self._listener = None

            channel = self._channel
            process = self._process
            rpc_acquired = self._rpc_lock.acquire(
                timeout=min(_GUI_DESTROY_RPC_TIMEOUT, remaining())
            )
            if rpc_acquired and channel is not None and process is not None:
                self._request_id += 1
                request_id = self._request_id
                rpc_expires_at = min(
                    expires_at,
                    active_clock() + _GUI_DESTROY_RPC_TIMEOUT,
                )
                try:
                    _send_frame(
                        channel,
                        ("request", request_id, "destroy", None),
                        expires_at=rpc_expires_at,
                        clock=active_clock,
                    )
                    response = _receive_frame(
                        channel,
                        expires_at=rpc_expires_at,
                        clock=active_clock,
                    )
                    if response != ("response", request_id, True, None):
                        raise RuntimeError("invalid destroy ACK")
                except BaseException:
                    record(_CLEANUP_DESTROY_COMMAND)
            elif not rpc_acquired:
                record(_CLEANUP_RPC_SERIALIZATION)
                if channel is not None:
                    try:
                        channel.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            if process is not None:
                try:
                    returncode = process.shutdown(
                        graceful_timeout=_GUI_GRACEFUL_JOIN_TIMEOUT,
                        terminate_timeout=_GUI_TERMINATE_JOIN_TIMEOUT,
                        kill_timeout=_GUI_KILL_JOIN_TIMEOUT,
                        cleanup_timeout=_GUI_OWNER_CLEANUP_TIMEOUT,
                        deadline=expires_at,
                    )
                    if type(returncode) is not int:
                        raise ProcessOwnershipError(
                            "Process owner returned no exit code proof"
                        )
                except BaseException:
                    record(_CLEANUP_PROCESS_CLOSE)
                else:
                    self._process = None

            if not rpc_acquired:
                rpc_acquired = self._rpc_lock.acquire(timeout=remaining())
                if not rpc_acquired:
                    record(_CLEANUP_RPC_SERIALIZATION)

            if self._process is None and rpc_acquired and channel is not None:
                try:
                    channel.close()
                except BaseException:
                    record(_CLEANUP_CHANNEL_CLOSE)
                else:
                    self._channel = None

            if (
                self._process is None
                and self._channel is None
                and self._listener is None
            ):
                state_acquired = self._state_lock.acquire(timeout=remaining())
                if not state_acquired:
                    record(_CLEANUP_STATE_SERIALIZATION)
                    raise ControlPanelProcessOwnershipError(
                        "Battle control panel state serialization deadline expired"
                    )
                try:
                    if remaining() <= 0:
                        record(_CLEANUP_STATE_SERIALIZATION)
                        raise ControlPanelProcessOwnershipError(
                            "Battle control panel state serialization completed after "
                            "its shutdown deadline"
                        )
                    self._state = _ControlPanelLifecycle.CLOSED
                finally:
                    self._state_lock.release()
                if remaining() <= 0:
                    raise ControlPanelProcessOwnershipError(
                        "Battle control panel ownership was resolved after its "
                        "shutdown deadline"
                    )
                return
            if self._process is not None:
                record(_CLEANUP_PROCESS_ALIVE)
            raise ControlPanelProcessOwnershipError(
                "Battle control panel child ownership is unresolved "
                f"reason_codes={','.join(reasons)}"
            )
        finally:
            if rpc_acquired:
                self._rpc_lock.release()
            self._shutdown_lock.release()

    @staticmethod
    def _expect_none(result: object) -> None:
        if result is not None:
            raise RuntimeError("Battle control panel returned invalid mutation ACK")

    def set_title(self, title: str) -> None:
        self._expect_none(self._rpc("set_title", title))

    def register_toggle(
        self,
        name: str,
        label: str,
        default: bool = False,
        *,
        group: str = "Options",
    ) -> None:
        self._expect_none(self._rpc("register_toggle", (name, label, default, group)))

    def get_toggle(self, name: str) -> bool:
        value = self._rpc("get_toggle", name)
        if not isinstance(value, bool):
            self._mark_closing()
            raise RuntimeError("Battle control panel returned an invalid toggle state")
        return value

    def register_integer(
        self,
        name: str,
        label: str,
        default: int = 0,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
        group: str = "Options",
    ) -> None:
        _validate_integer_configuration(default, minimum, maximum)
        self._expect_none(
            self._rpc(
                "register_integer",
                (name, label, default, minimum, maximum, group),
            )
        )

    def get_integer(self, name: str) -> int:
        value = self._rpc("get_integer", name)
        if type(value) is not int:
            self._mark_closing()
            raise RuntimeError("Battle control panel returned an invalid integer state")
        return value

    def set_checklist(
        self,
        name: str,
        label: str,
        choices: Iterable[tuple[str, str]],
        selected: Iterable[str] = (),
        *,
        status: str | None = None,
    ) -> None:
        normalized_choices, normalized_selected = _validate_checklist_configuration(
            name,
            label,
            choices,
            selected,
            status,
        )
        result = self._rpc(
            "set_checklist",
            (name, label, normalized_choices, normalized_selected, status),
        )
        match result:
            case (
                int() as revision,
                tuple() as effective_selected,
            ) if revision > 0 and all(
                isinstance(key, str) for key in effective_selected
            ):
                return
            case _:
                self._mark_closing()
                raise RuntimeError(
                    "Battle control panel returned an invalid checklist ACK"
                )

    def get_checklist_selection(self, name: str) -> tuple[str, ...]:
        result = self._rpc("get_checklist", name)
        match result:
            case (int() as revision, tuple() as selected) if revision > 0 and all(
                isinstance(key, str) for key in selected
            ):
                return cast(tuple[str, ...], selected)
            case _:
                self._mark_closing()
                raise RuntimeError(
                    "Battle control panel returned invalid checklist state"
                )

    def pause(self) -> None:
        self._expect_none(self._rpc("pause"))
        logger.info("Battle control panel pause requested")

    def set_actions(
        self, action_groups: dict[str, list[str]], disabled: Iterable[str]
    ) -> None:
        disabled_set = frozenset(disabled)
        self._expect_none(self._rpc("set_actions", (action_groups, disabled_set)))

    def get_disabled_actions(self) -> frozenset[str]:
        value = self._rpc("get_disabled_actions")
        if not isinstance(value, tuple) or any(
            not isinstance(name, str) for name in value
        ):
            self._mark_closing()
            raise RuntimeError("Battle control panel returned invalid action state")
        return frozenset(value)

    def is_paused(self) -> bool:
        """Read the live pause flag, failing closed if the GUI is unavailable."""

        paused = self._rpc("is_paused")
        if not isinstance(paused, bool):
            self._mark_closing()
            raise RuntimeError("Battle control panel returned an invalid pause state")
        return paused

    async def wait_if_paused(self) -> None:
        waiting = False
        while True:
            if not self.is_paused():
                if waiting:
                    logger.info("Battle control panel resumed")
                return
            if not waiting:
                logger.info("Battle control panel is paused; waiting for Resume")
                waiting = True
            await asyncio.sleep(0.5)

    async def aclose(self, *, expires_at: float | None = None) -> None:
        """Finish one bounded ownership proof before propagating cancellation."""

        local_expires_at = time.monotonic() + _GUI_SHUTDOWN_TOTAL_TIMEOUT
        if expires_at is None:
            expires_at = local_expires_at
        elif (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int | float)
            or not math.isfinite(expires_at)
        ):
            raise ValueError("expires_at must be a finite monotonic deadline")
        else:
            expires_at = min(float(expires_at), local_expires_at)
        if _remaining(expires_at) <= 0:
            raise ControlPanelProcessOwnershipError(
                "Battle control panel close deadline expired before ownership proof"
            )
        state_acquired = self._state_lock.acquire(timeout=_remaining(expires_at))
        if not state_acquired:
            raise ControlPanelProcessOwnershipError(
                "Battle control panel close deadline expired waiting for state ownership"
            )
        try:
            if _remaining(expires_at) <= 0:
                raise ControlPanelProcessOwnershipError(
                    "Battle control panel state ownership arrived after its close "
                    "deadline"
                )
            if self._state is _ControlPanelLifecycle.CLOSED:
                return
            self._state = _ControlPanelLifecycle.CLOSING
        finally:
            self._state_lock.release()
        cleanup = asyncio.create_task(
            asyncio.to_thread(self._shutdown_owned, expires_at=expires_at)
        )
        cancellation_requested = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancellation_requested = True
        # Ownership failure takes precedence over cancellation: callers must
        # never mistake an unresolved child for a clean cancellation boundary.
        cleanup.result()
        if _remaining(expires_at) <= 0:
            raise ControlPanelProcessOwnershipError(
                "Battle control panel close completed after its ownership deadline"
            )
        if cancellation_requested:
            raise asyncio.CancelledError

    def destroy(self) -> None:
        expires_at = time.monotonic() + _GUI_SHUTDOWN_TOTAL_TIMEOUT
        state_acquired = self._state_lock.acquire(timeout=_remaining(expires_at))
        if not state_acquired:
            raise ControlPanelProcessOwnershipError(
                "Battle control panel destroy deadline expired waiting for state "
                "ownership"
            )
        try:
            if _remaining(expires_at) <= 0:
                raise ControlPanelProcessOwnershipError(
                    "Battle control panel state ownership arrived after its destroy "
                    "deadline"
                )
            if self._state is _ControlPanelLifecycle.CLOSED:
                return
            self._state = _ControlPanelLifecycle.CLOSING
        finally:
            self._state_lock.release()
        self._shutdown_owned(expires_at=expires_at)
        if _remaining(expires_at) <= 0:
            raise ControlPanelProcessOwnershipError(
                "Battle control panel destroy completed after its ownership deadline"
            )


class NullControlPanel(BaseControlPanel):
    """Headless control panel with the same in-memory configuration contract."""

    def __init__(self) -> None:
        self._toggles: dict[str, bool] = {}
        self._integers: dict[str, int] = {}
        self._checklists: dict[str, tuple[str, ...]] = {}
        self._disabled_actions: frozenset[str] = frozenset()

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
        del label, group
        self._toggles[name] = default

    def get_toggle(self, name: str) -> bool:
        return self._toggles.get(name, False)

    def register_integer(
        self,
        name: str,
        label: str,
        default: int = 0,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
        group: str = "Options",
    ) -> None:
        del label, group
        _validate_integer_configuration(default, minimum, maximum)
        self._integers[name] = default

    def get_integer(self, name: str) -> int:
        try:
            return self._integers[name]
        except KeyError:
            raise KeyError(f"Unknown integer control: {name}") from None

    def set_checklist(
        self,
        name: str,
        label: str,
        choices: Iterable[tuple[str, str]],
        selected: Iterable[str] = (),
        *,
        status: str | None = None,
    ) -> None:
        _choices, normalized_selected = _validate_checklist_configuration(
            name,
            label,
            choices,
            selected,
            status,
        )
        self._checklists[name] = normalized_selected

    def get_checklist_selection(self, name: str) -> tuple[str, ...]:
        try:
            return self._checklists[name]
        except KeyError:
            raise KeyError(f"Unknown checklist control: {name}") from None

    def pause(self) -> None:
        return

    def set_actions(
        self, action_groups: dict[str, list[str]], disabled: Iterable[str]
    ) -> None:
        del action_groups
        self._disabled_actions = frozenset(disabled)

    def get_disabled_actions(self) -> frozenset[str]:
        return self._disabled_actions

    def is_paused(self) -> bool:
        return False

    async def wait_if_paused(self) -> None:
        return

    async def aclose(self, *, expires_at: float | None = None) -> None:
        if expires_at is not None and (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int | float)
            or not math.isfinite(expires_at)
        ):
            raise ValueError("expires_at must be a finite monotonic deadline")
        return

    def destroy(self) -> None:
        return


def _validate_integer_configuration(
    default: int,
    minimum: int | None,
    maximum: int | None,
) -> None:
    if not isinstance(default, int) or isinstance(default, bool):
        raise TypeError("default must be an integer")
    for label, value in (("minimum", minimum), ("maximum", maximum)):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise TypeError(f"{label} must be an integer or None")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minimum cannot exceed maximum")
    if minimum is not None and default < minimum:
        raise ValueError("default cannot be less than minimum")
    if maximum is not None and default > maximum:
        raise ValueError("default cannot exceed maximum")


def _validate_checklist_configuration(
    name: str,
    label: str,
    choices: Iterable[tuple[str, str]],
    selected: Iterable[str],
    status: str | None,
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    if not isinstance(name, str):
        raise TypeError("checklist name must be str")
    if not name.strip():
        raise ValueError("checklist name must not be empty")
    if not isinstance(label, str):
        raise TypeError("checklist label must be str")
    if not label.strip():
        raise ValueError("checklist label must not be empty")
    if status is not None and not isinstance(status, str):
        raise TypeError("checklist status must be str or None")
    if isinstance(choices, (str, bytes)) or not isinstance(choices, Iterable):
        raise TypeError("checklist choices must be an iterable of key-label tuples")
    if isinstance(selected, (str, bytes)) or not isinstance(selected, Iterable):
        raise TypeError("checklist selected keys must be an iterable of strings")

    normalized_choices: list[tuple[str, str]] = []
    choice_keys: set[str] = set()
    for choice in choices:
        if not isinstance(choice, tuple) or len(choice) != 2:
            raise TypeError("each checklist choice must be a key-label tuple")
        key, choice_label = choice
        if not isinstance(key, str):
            raise TypeError("checklist choice key must be str")
        if not key.strip():
            raise ValueError("checklist choice key must not be empty")
        if not isinstance(choice_label, str):
            raise TypeError("checklist choice label must be str")
        if not choice_label.strip():
            raise ValueError("checklist choice label must not be empty")
        if key in choice_keys:
            raise ValueError(f"duplicate checklist choice key: {key}")
        choice_keys.add(key)
        normalized_choices.append((key, choice_label))

    selected_keys: set[str] = set()
    for key in selected:
        if not isinstance(key, str):
            raise TypeError("checklist selected key must be str")
        if key in selected_keys:
            raise ValueError(f"duplicate checklist selected key: {key}")
        selected_keys.add(key)
    unknown = selected_keys.difference(choice_keys)
    if unknown:
        raise ValueError(f"unknown checklist selected key: {sorted(unknown)[0]}")

    ordered_selection = tuple(
        key for key, _choice_label in normalized_choices if key in selected_keys
    )
    return tuple(normalized_choices), ordered_selection


def _parse_integer(
    raw_value: str,
    minimum: int | None,
    maximum: int | None,
) -> int | None:
    try:
        value = int(raw_value)
    except ValueError:
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _parse_gui_child_arguments(arguments: Sequence[str]) -> tuple[int, str]:
    if len(arguments) != 2:
        raise ValueError("control panel child requires port and auth token")
    try:
        port = int(arguments[0])
    except ValueError as error:
        raise ValueError("control panel IPC port is invalid") from error
    auth_token = arguments[1]
    if not 1 <= port <= 65_535:
        raise ValueError("control panel IPC port is out of range")
    if len(auth_token) != _IPC_AUTH_TOKEN_BYTES * 2:
        raise ValueError("control panel IPC token has an invalid length")
    try:
        bytes.fromhex(auth_token)
    except ValueError as error:
        raise ValueError("control panel IPC token is invalid") from error
    return port, auth_token


def _run_gui_child(arguments: Sequence[str]) -> int:
    port, auth_token = _parse_gui_child_arguments(arguments)
    channel = socket.create_connection(
        ("127.0.0.1", port),
        timeout=_GUI_RPC_TIMEOUT,
    )
    try:
        _run_gui(channel, auth_token)
    finally:
        channel.close()
    return 0


def _run_owned_gui_child(arguments: Sequence[str]) -> int:
    """Own the optional forwarding lifecycle around the GUI business result."""

    configure_forwarded_logging()
    try:
        return _run_gui_child(arguments)
    finally:
        try:
            close_forwarded_logging()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(_run_owned_gui_child(sys.argv[1:]))

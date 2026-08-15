"""Reusable battle control panel primitives without campaign policy."""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from functools import partial
from multiprocessing import Queue
from queue import Empty
from typing import Any

from hvbrowser.runtime import setup_logger

logger = setup_logger(__name__)

_GUI_START_TIMEOUT = 10.0
_GUI_STOP_TIMEOUT = 3.0
_CHECKLIST_TEXT_MAX_WRAP_LENGTH = 420
_CHECKLIST_TEXT_HORIZONTAL_OVERHEAD = 48
_CHECKLIST_CANVAS_HORIZONTAL_OVERHEAD = 24
_CHECKLIST_NON_CHOICE_VERTICAL_OVERHEAD = 160
_WINDOW_SCREEN_MARGIN = 80
_WINDOW_CONTENT_HORIZONTAL_OVERHEAD = 48
_CLEANUP_PROCESS_STATE = "process-state-unavailable"
_CLEANUP_DESTROY_COMMAND = "destroy-command-failed"
_CLEANUP_PROCESS_JOIN = "process-join-failed"
_CLEANUP_PROCESS_TERMINATE = "process-terminate-failed"
_CLEANUP_PROCESS_ALIVE = "process-still-alive"
_CLEANUP_MANAGER_SHUTDOWN = "manager-shutdown-failed"


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


def _checklist_render_state(
    shared: Any,
    checklist_lock: Any,
    name: str,
    command_revision: int,
    command_keys: tuple[str, ...],
    command_selected: tuple[str, ...],
) -> tuple[int, tuple[str, ...]] | None:
    """Return the newest compatible state, or skip a superseded command."""
    with checklist_lock:
        revision, keys, selected = _unpack_checklist_state(shared[name])
        if revision < command_revision:
            raise RuntimeError("shared checklist revision moved backwards")
        if revision == command_revision:
            if keys != command_keys or selected != command_selected:
                raise RuntimeError("shared checklist state does not match its command")
            return revision, selected
        if keys != command_keys:
            return None
        return revision, selected


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


def _run_gui(
    pause_flag: Any,
    toggle_dict: Any,
    integer_dict: Any,
    checklist_dict: Any,
    checklist_lock: Any,
    skill_dict: Any,
    cmd_queue: Queue[tuple[str, Any]],
    ready_event: Any,
) -> None:
    """Run Tk in a child process so importing hvbattle stays headless-safe."""
    import tkinter as tk

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
    skill_container = tk.Frame(controls_container)
    skill_container.pack(pady=(0, 5), fill="x")
    toggle_container = tk.Frame(controls_container)
    toggle_container.pack(pady=(5, 0), fill="x")

    checklist_container = tk.Frame(body)
    checklist_container.grid(row=0, column=1, padx=(5, 0), sticky="nsew")
    checklist_container.rowconfigure(0, weight=1)
    body.columnconfigure(1, weight=1)

    local_skills: dict[str, tk.BooleanVar] = {}
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

    def sync_to_shared_impl() -> None:
        for name, skill_variable in local_skills.items():
            skill_dict[name] = skill_variable.get()
        for name, toggle_variable in local_toggles.items():
            toggle_dict[name] = toggle_variable.get()
        root.after(200, sync_to_shared)

    def sync_to_shared() -> None:
        _run_gui_callback_fail_closed(
            pause_flag,
            root.destroy,
            sync_to_shared_impl,
        )

    def poll_commands_impl() -> None:
        while True:
            try:
                command, arguments = cmd_queue.get_nowait()
            except Empty:
                break
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
                case "set_checklist":
                    name, label, choices, revision, selected, status = arguments
                    keys = tuple(key for key, _choice_label in choices)
                    render_state = _checklist_render_state(
                        checklist_dict,
                        checklist_lock,
                        name,
                        revision,
                        keys,
                        selected,
                    )
                    if render_state is None:
                        continue
                    frame_revision, selected = render_state
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
                case "set_skills":
                    skill_groups, forbidden = arguments
                    for widget in skill_container.winfo_children():
                        widget.destroy()
                    local_skills.clear()
                    for column, (group_name, skills) in enumerate(skill_groups.items()):
                        frame = tk.LabelFrame(skill_container, text=group_name)
                        frame.grid(row=0, column=column, padx=5, pady=3, sticky="nsew")
                        for skill in skills:
                            skill_variable = tk.BooleanVar(value=skill not in forbidden)
                            local_skills[skill] = skill_variable
                            skill_dict[skill] = skill_variable.get()
                            tk.Checkbutton(
                                frame,
                                text=skill,
                                variable=skill_variable,
                                command=partial(
                                    _publish_boolean,
                                    skill_dict,
                                    skill,
                                    skill_variable,
                                ),
                            ).pack(anchor="w", padx=5, pady=1)
                        skill_container.columnconfigure(column, weight=1)
                    schedule_checklist_reflow()
                case "set_title":
                    root.title(arguments)
                case "pause":
                    _render_pause_button(pause_flag, pause_button)
                case "destroy":
                    root.destroy()
                    return
        root.after(100, poll_commands)

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
    root.after(100, poll_commands)
    root.after(200, sync_to_shared)
    ready_event.set()
    try:
        root.mainloop()
    finally:
        # A graceful Tk exit that did not pass through WM_DELETE_WINDOW must
        # still stop the parent at its next live-control boundary.
        try:
            pause_flag.set()
        except Exception:
            # The manager may already be unavailable during interpreter
            # shutdown. The parent independently checks the GUI process.
            pass


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

    def set_actions(
        self, action_groups: dict[str, list[str]], disabled: Iterable[str]
    ) -> None:
        """Configure named action permissions using the legacy skill channel."""
        self.set_skills(action_groups, disabled)

    def get_disabled_actions(self) -> frozenset[str]:
        """Return disabled named actions using the legacy skill channel."""
        return self.get_forbidden_skills()

    @abstractmethod
    def set_skills(
        self, skill_groups: dict[str, list[str]], forbidden: Iterable[str]
    ) -> None: ...

    @abstractmethod
    def get_forbidden_skills(self) -> frozenset[str]: ...

    @abstractmethod
    def is_paused(self) -> bool:
        """Return the committed pause state without blocking."""

    @abstractmethod
    async def wait_if_paused(self) -> None: ...

    @abstractmethod
    def destroy(self) -> None: ...


class ControlPanel(BaseControlPanel):
    """Tk control panel hosted in a dedicated child process."""

    def __init__(self) -> None:
        self._destroyed = False
        self._manager: Any = None
        self._pause_flag: Any = None
        self._toggle_dict: Any = None
        self._integer_dict: Any = None
        self._checklist_dict: Any = None
        self._checklist_lock: Any = None
        self._checklist_observed_revisions: dict[str, int] = {}
        self._skill_dict: Any = None
        self._cmd_queue: Any = None
        self._process: Any = None
        try:
            self._manager = multiprocessing.Manager()
            self._pause_flag = self._manager.Event()
            self._toggle_dict = self._manager.dict()
            self._integer_dict = self._manager.dict()
            self._checklist_dict = self._manager.dict()
            self._checklist_lock = self._manager.RLock()
            self._skill_dict = self._manager.dict()
            self._cmd_queue = self._manager.Queue()
            ready_event = self._manager.Event()
            self._process = multiprocessing.Process(
                target=_run_gui,
                args=(
                    self._pause_flag,
                    self._toggle_dict,
                    self._integer_dict,
                    self._checklist_dict,
                    self._checklist_lock,
                    self._skill_dict,
                    self._cmd_queue,
                    ready_event,
                ),
                daemon=True,
            )
            self._process.start()
            self._wait_until_ready(ready_event)
        except BaseException:
            self._destroyed = True
            self._report_cleanup_failures("startup", self._cleanup_resources())
            raise

    def _wait_until_ready(self, ready_event: Any) -> None:
        deadline = time.monotonic() + _GUI_START_TIMEOUT
        while time.monotonic() < deadline:
            if ready_event.wait(timeout=0.1):
                if self._process.is_alive():
                    return
                break
            if not self._process.is_alive():
                break
        raise RuntimeError("Battle control panel failed to start")

    def _cleanup_resources(self) -> tuple[str, ...]:
        """Best-effort cleanup for both partial startup and normal destroy."""

        reasons: list[str] = []

        def record(reason: str) -> None:
            if reason not in reasons:
                reasons.append(reason)

        process = self._process
        if process is not None:
            alive: bool | None
            try:
                alive = bool(process.is_alive())
            except BaseException:
                alive = None
                record(_CLEANUP_PROCESS_STATE)

            if alive is not False:
                try:
                    if self._cmd_queue is None:
                        raise RuntimeError
                    self._cmd_queue.put(("destroy", None))
                except BaseException:
                    record(_CLEANUP_DESTROY_COMMAND)

            try:
                process.join(timeout=_GUI_STOP_TIMEOUT)
            except BaseException:
                record(_CLEANUP_PROCESS_JOIN)

            try:
                alive = bool(process.is_alive())
            except BaseException:
                alive = None
                record(_CLEANUP_PROCESS_STATE)

            if alive is not False:
                try:
                    process.terminate()
                except BaseException:
                    record(_CLEANUP_PROCESS_TERMINATE)
                try:
                    process.join(timeout=_GUI_STOP_TIMEOUT)
                except BaseException:
                    record(_CLEANUP_PROCESS_JOIN)

            try:
                if process.is_alive():
                    record(_CLEANUP_PROCESS_ALIVE)
            except BaseException:
                record(_CLEANUP_PROCESS_STATE)

        if self._manager is not None:
            try:
                self._manager.shutdown()
            except BaseException:
                record(_CLEANUP_MANAGER_SHUTDOWN)

        return tuple(reasons)

    @staticmethod
    def _report_cleanup_failures(phase: str, reasons: tuple[str, ...]) -> None:
        if reasons:
            logger.warning(
                "Battle control panel cleanup incomplete phase=%s reason_codes=%s",
                phase,
                ",".join(reasons),
            )

    def _require_live_gui(self) -> None:
        """Fail closed instead of using stale state after the GUI exits."""
        if self._destroyed:
            raise RuntimeError("Battle control panel has been destroyed")
        try:
            alive = self._process.is_alive()
        except Exception as error:
            self._pause_best_effort()
            raise RuntimeError(
                "Battle control panel GUI process state is unavailable"
            ) from error
        if alive:
            return
        self._pause_best_effort()
        raise RuntimeError("Battle control panel GUI process is not running")

    def _pause_best_effort(self) -> None:
        try:
            self._pause_flag.set()
        except Exception:
            pass

    def set_title(self, title: str) -> None:
        self._require_live_gui()
        self._cmd_queue.put(("set_title", title))
        self._require_live_gui()

    def register_toggle(
        self,
        name: str,
        label: str,
        default: bool = False,
        *,
        group: str = "Options",
    ) -> None:
        self._require_live_gui()
        self._toggle_dict[name] = default
        self._cmd_queue.put(("register_toggle", (name, label, default, group)))
        self._require_live_gui()

    def get_toggle(self, name: str) -> bool:
        self._require_live_gui()
        value = bool(self._toggle_dict.get(name, False))
        self._require_live_gui()
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
        self._require_live_gui()
        _validate_integer_configuration(default, minimum, maximum)
        self._integer_dict[name] = default
        self._cmd_queue.put(
            (
                "register_integer",
                (name, label, default, minimum, maximum, group),
            )
        )
        self._require_live_gui()

    def get_integer(self, name: str) -> int:
        self._require_live_gui()
        try:
            value = int(self._integer_dict[name])
        except KeyError:
            raise KeyError(f"Unknown integer control: {name}") from None
        self._require_live_gui()
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
        self._require_live_gui()
        normalized_choices, normalized_selected = _validate_checklist_configuration(
            name,
            label,
            choices,
            selected,
            status,
        )
        keys = tuple(key for key, _choice_label in normalized_choices)
        with self._checklist_lock:
            current_state = self._checklist_dict.get(name)
            if current_state is None:
                revision = 1
                effective_selected = normalized_selected
            else:
                current_revision, current_keys, current_selected = (
                    _unpack_checklist_state(current_state)
                )
                revision = current_revision + 1
                if self._checklist_observed_revisions.get(name) == current_revision:
                    effective_selected = normalized_selected
                else:
                    effective_selected = _merge_checklist_replacement(
                        current_keys,
                        current_selected,
                        keys,
                        normalized_selected,
                    )
            self._checklist_dict[name] = (revision, keys, effective_selected)
            self._checklist_observed_revisions[name] = revision
        self._cmd_queue.put(
            (
                "set_checklist",
                (
                    name,
                    label,
                    normalized_choices,
                    revision,
                    effective_selected,
                    status,
                ),
            )
        )
        self._require_live_gui()

    def get_checklist_selection(self, name: str) -> tuple[str, ...]:
        self._require_live_gui()
        try:
            with self._checklist_lock:
                revision, _keys, selected = _unpack_checklist_state(
                    self._checklist_dict[name]
                )
                self._checklist_observed_revisions[name] = revision
        except KeyError:
            raise KeyError(f"Unknown checklist control: {name}") from None
        self._require_live_gui()
        return selected

    def pause(self) -> None:
        self._require_live_gui()
        self._pause_flag.set()
        self._cmd_queue.put(("pause", None))
        self._require_live_gui()
        logger.info("Battle control panel pause requested")

    def set_skills(
        self, skill_groups: dict[str, list[str]], forbidden: Iterable[str]
    ) -> None:
        self._require_live_gui()
        forbidden_set = frozenset(forbidden)
        self._skill_dict.clear()
        for skills in skill_groups.values():
            for skill in skills:
                self._skill_dict[skill] = skill not in forbidden_set
        self._cmd_queue.put(("set_skills", (skill_groups, forbidden_set)))
        self._require_live_gui()

    def get_forbidden_skills(self) -> frozenset[str]:
        self._require_live_gui()
        forbidden = frozenset(
            name for name, enabled in self._skill_dict.items() if not enabled
        )
        self._require_live_gui()
        return forbidden

    def is_paused(self) -> bool:
        """Read the live pause flag, failing closed if the GUI is unavailable."""

        self._require_live_gui()
        try:
            paused = self._pause_flag.is_set()
        except Exception as error:
            self._pause_best_effort()
            raise RuntimeError(
                "Battle control panel pause state is unavailable"
            ) from error
        self._require_live_gui()
        if not isinstance(paused, bool):
            self._pause_best_effort()
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

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._report_cleanup_failures("destroy", self._cleanup_resources())


class NullControlPanel(BaseControlPanel):
    """Headless control panel with the same in-memory configuration contract."""

    def __init__(self) -> None:
        self._toggles: dict[str, bool] = {}
        self._integers: dict[str, int] = {}
        self._checklists: dict[str, tuple[str, ...]] = {}
        self._forbidden_skills: frozenset[str] = frozenset()

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

    def set_skills(
        self, skill_groups: dict[str, list[str]], forbidden: Iterable[str]
    ) -> None:
        del skill_groups
        self._forbidden_skills = frozenset(forbidden)

    def get_forbidden_skills(self) -> frozenset[str]:
        return self._forbidden_skills

    def is_paused(self) -> bool:
        return False

    async def wait_if_paused(self) -> None:
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

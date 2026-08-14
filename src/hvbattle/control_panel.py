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

_GUI_START_TIMEOUT = 10.0
_GUI_STOP_TIMEOUT = 3.0
_CHECKLIST_ROWS_PER_COLUMN = 12


def _publish_boolean(shared: Any, name: str, variable: Any) -> None:
    shared[name] = bool(variable.get())


def _publish_checklist_selection(
    shared: Any,
    name: str,
    keys: tuple[str, ...],
    variables: tuple[Any, ...],
) -> None:
    """Publish one ordered checklist snapshot with a single shared write."""
    shared[name] = tuple(
        key for key, variable in zip(keys, variables, strict=True) if variable.get()
    )


def _checklist_grid_position(index: int) -> tuple[int, int]:
    return index % _CHECKLIST_ROWS_PER_COLUMN, index // _CHECKLIST_ROWS_PER_COLUMN


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
    pause_flag.set()
    destroy()


def _set_paused(pause_flag: Any, pause_button: Any, paused: bool) -> None:
    if paused:
        pause_flag.set()
        pause_button.config(text="Resume")
    else:
        pause_flag.clear()
        pause_button.config(text="Pause")


def _invoke_callback(callback: Callable[[], None], _event: Any) -> None:
    callback()


def _run_gui(
    pause_flag: Any,
    toggle_dict: Any,
    integer_dict: Any,
    checklist_dict: Any,
    skill_dict: Any,
    cmd_queue: Queue[tuple[str, Any]],
    ready_event: Any,
) -> None:
    """Run Tk in a child process so importing hvbattle stays headless-safe."""
    import tkinter as tk

    root = tk.Tk()
    root.title("Battle Control Panel")
    root.minsize(width=300, height=0)

    pause_button = tk.Button(root, text="Pause")
    pause_button.pack(padx=10, pady=5)

    skill_container = tk.Frame(root)
    skill_container.pack(padx=10, pady=5, fill="x")
    toggle_container = tk.Frame(root)
    toggle_container.pack(padx=10, pady=5, fill="x")
    checklist_container = tk.Frame(root)
    checklist_container.pack(padx=10, pady=5, fill="x")

    local_skills: dict[str, tk.BooleanVar] = {}
    local_toggles: dict[str, tk.BooleanVar] = {}
    toggle_groups: dict[str, tk.LabelFrame] = {}
    checklist_frames: dict[str, tk.LabelFrame] = {}

    def group_frame(group: str) -> tk.LabelFrame:
        frame = toggle_groups.get(group)
        if frame is None:
            frame = tk.LabelFrame(toggle_container, text=group)
            frame.pack(side="left", padx=5, pady=3, fill="both", expand=True)
            toggle_groups[group] = frame
        return frame

    def toggle_pause() -> None:
        _set_paused(pause_flag, pause_button, not pause_flag.is_set())

    def sync_to_shared() -> None:
        for name, skill_variable in local_skills.items():
            skill_dict[name] = skill_variable.get()
        for name, toggle_variable in local_toggles.items():
            toggle_dict[name] = toggle_variable.get()
        root.after(200, sync_to_shared)

    def poll_commands() -> None:
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
                case "set_checklist":
                    name, label, choices, selected, status = arguments
                    old_frame = checklist_frames.pop(name, None)
                    if old_frame is not None:
                        old_frame.destroy()

                    frame = tk.LabelFrame(checklist_container, text=label)
                    frame.pack(padx=5, pady=3, fill="x")
                    checklist_frames[name] = frame

                    if status is not None:
                        tk.Label(frame, text=status, anchor="w").pack(
                            anchor="w", padx=5, pady=(2, 1)
                        )
                    choices_container = tk.Frame(frame)
                    choices_container.pack(fill="x")

                    selected_keys = frozenset(selected)
                    keys = tuple(key for key, _choice_label in choices)
                    variables = tuple(
                        tk.BooleanVar(value=key in selected_keys) for key in keys
                    )
                    checklist_dict[name] = selected
                    if not choices:
                        tk.Label(
                            choices_container,
                            text="No choices available",
                            anchor="w",
                        ).pack(anchor="w", padx=5, pady=1)
                    for index, ((_key, choice_label), variable) in enumerate(
                        zip(choices, variables, strict=True)
                    ):
                        checkbox = tk.Checkbutton(
                            choices_container,
                            text=choice_label,
                            variable=variable,
                            command=partial(
                                _publish_checklist_selection,
                                checklist_dict,
                                name,
                                keys,
                                variables,
                            ),
                        )
                        grid_row, grid_column = _checklist_grid_position(index)
                        checkbox.grid(
                            row=grid_row,
                            column=grid_column,
                            sticky="w",
                            padx=5,
                            pady=1,
                        )
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
                case "set_title":
                    root.title(arguments)
                case "pause":
                    _set_paused(pause_flag, pause_button, True)
                case "destroy":
                    root.destroy()
                    return
        root.after(100, poll_commands)

    pause_button.config(command=toggle_pause)
    # Losing the only interactive control surface must fail safe. The current
    # operation may finish, but the parent pauses at its next gate.
    root.protocol(
        "WM_DELETE_WINDOW",
        partial(_close_gui, pause_flag, root.destroy),
    )
    root.after(100, poll_commands)
    root.after(200, sync_to_shared)
    ready_event.set()
    root.mainloop()


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
    async def wait_if_paused(self) -> None: ...

    @abstractmethod
    def destroy(self) -> None: ...


class ControlPanel(BaseControlPanel):
    """Tk control panel hosted in a dedicated child process."""

    def __init__(self) -> None:
        self._manager = multiprocessing.Manager()
        self._pause_flag = self._manager.Event()
        self._toggle_dict = self._manager.dict()
        self._integer_dict = self._manager.dict()
        self._checklist_dict = self._manager.dict()
        self._skill_dict = self._manager.dict()
        self._cmd_queue = self._manager.Queue()
        ready_event = self._manager.Event()
        self._destroyed = False
        self._process = multiprocessing.Process(
            target=_run_gui,
            args=(
                self._pause_flag,
                self._toggle_dict,
                self._integer_dict,
                self._checklist_dict,
                self._skill_dict,
                self._cmd_queue,
                ready_event,
            ),
            daemon=True,
        )
        self._process.start()
        self._wait_until_ready(ready_event)

    def _wait_until_ready(self, ready_event: Any) -> None:
        deadline = time.monotonic() + _GUI_START_TIMEOUT
        while time.monotonic() < deadline:
            if ready_event.wait(timeout=0.1):
                return
            if not self._process.is_alive():
                break
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(timeout=_GUI_STOP_TIMEOUT)
        self._manager.shutdown()
        raise RuntimeError("Battle control panel failed to start")

    def set_title(self, title: str) -> None:
        self._cmd_queue.put(("set_title", title))

    def register_toggle(
        self,
        name: str,
        label: str,
        default: bool = False,
        *,
        group: str = "Options",
    ) -> None:
        self._toggle_dict[name] = default
        self._cmd_queue.put(("register_toggle", (name, label, default, group)))

    def get_toggle(self, name: str) -> bool:
        return bool(self._toggle_dict.get(name, False))

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
        self._integer_dict[name] = default
        self._cmd_queue.put(
            (
                "register_integer",
                (name, label, default, minimum, maximum, group),
            )
        )

    def get_integer(self, name: str) -> int:
        try:
            return int(self._integer_dict[name])
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
        normalized_choices, normalized_selected = _validate_checklist_configuration(
            name,
            label,
            choices,
            selected,
            status,
        )
        self._checklist_dict[name] = normalized_selected
        self._cmd_queue.put(
            (
                "set_checklist",
                (
                    name,
                    label,
                    normalized_choices,
                    normalized_selected,
                    status,
                ),
            )
        )

    def get_checklist_selection(self, name: str) -> tuple[str, ...]:
        try:
            return tuple(self._checklist_dict[name])
        except KeyError:
            raise KeyError(f"Unknown checklist control: {name}") from None

    def pause(self) -> None:
        self._pause_flag.set()
        self._cmd_queue.put(("pause", None))

    def set_skills(
        self, skill_groups: dict[str, list[str]], forbidden: Iterable[str]
    ) -> None:
        forbidden_set = frozenset(forbidden)
        self._skill_dict.clear()
        for skills in skill_groups.values():
            for skill in skills:
                self._skill_dict[skill] = skill not in forbidden_set
        self._cmd_queue.put(("set_skills", (skill_groups, forbidden_set)))

    def get_forbidden_skills(self) -> frozenset[str]:
        return frozenset(
            name for name, enabled in self._skill_dict.items() if not enabled
        )

    async def wait_if_paused(self) -> None:
        while self._pause_flag.is_set():
            await asyncio.sleep(0.5)

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        try:
            if self._process.is_alive():
                self._cmd_queue.put(("destroy", None))
                self._process.join(timeout=_GUI_STOP_TIMEOUT)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=_GUI_STOP_TIMEOUT)
        finally:
            self._manager.shutdown()


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

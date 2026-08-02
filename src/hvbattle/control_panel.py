"""Reusable battle control panel primitives without campaign policy."""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from multiprocessing import Queue
from queue import Empty
from typing import Any

_GUI_START_TIMEOUT = 10.0
_GUI_STOP_TIMEOUT = 3.0


def _run_gui(
    pause_flag: Any,
    toggle_dict: Any,
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

    local_skills: dict[str, tk.BooleanVar] = {}
    local_toggles: dict[str, tk.BooleanVar] = {}
    toggle_groups: dict[str, tk.LabelFrame] = {}

    def toggle_pause() -> None:
        if pause_flag.is_set():
            pause_flag.clear()
            pause_button.config(text="Pause")
        else:
            pause_flag.set()
            pause_button.config(text="Resume")

    def sync_to_shared() -> None:
        for name, variable in local_skills.items():
            skill_dict[name] = variable.get()
        for name, variable in local_toggles.items():
            toggle_dict[name] = variable.get()
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
                    frame = toggle_groups.get(group)
                    if frame is None:
                        frame = tk.LabelFrame(toggle_container, text=group)
                        frame.pack(
                            side="left", padx=5, pady=3, fill="both", expand=True
                        )
                        toggle_groups[group] = frame
                    variable = tk.BooleanVar(value=default)
                    local_toggles[name] = variable
                    toggle_dict[name] = default
                    tk.Checkbutton(frame, text=label, variable=variable).pack(
                        anchor="w", padx=5, pady=1
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
                            variable = tk.BooleanVar(value=skill not in forbidden)
                            local_skills[skill] = variable
                            skill_dict[skill] = variable.get()
                            tk.Checkbutton(frame, text=skill, variable=variable).pack(
                                anchor="w", padx=5, pady=1
                            )
                        skill_container.columnconfigure(column, weight=1)
                case "set_title":
                    root.title(arguments)
                case "destroy":
                    root.destroy()
                    return
        root.after(100, poll_commands)

    pause_button.config(command=toggle_pause)
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
        self._skill_dict = self._manager.dict()
        self._cmd_queue = self._manager.Queue()
        ready_event = self._manager.Event()
        self._destroyed = False
        self._process = multiprocessing.Process(
            target=_run_gui,
            args=(
                self._pause_flag,
                self._toggle_dict,
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

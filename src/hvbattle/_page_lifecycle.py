"""Pre-armed main-document lifecycle synchronization for internal navigation."""

import asyncio
from typing import Any

from hvbrowser.runtime import wait_for_zendriver
from zendriver import cdp

from ._timing import SemanticDeadline


class MainFrameDOMContentLoadedWaiter:
    """Resolve only for DOMContentLoaded on the next main-frame loader."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._future: asyncio.Future[tuple[str, str]] = (
            asyncio.get_running_loop().create_future()
        )
        self._triggered = False
        self._old_loader: tuple[str, str] | None = None
        self._old_loader_id: Any = None
        self._main_loader: tuple[str, str] | None = None
        self._dom_content_loaded: set[tuple[str, str]] = set()
        self._closed = False
        page.add_handler(cdp.page.FrameNavigated, self._on_frame_navigated)
        page.add_handler(cdp.page.LifecycleEvent, self._on_lifecycle_event)

    async def enable(self, *, deadline: SemanticDeadline) -> None:
        """Enable lifecycle delivery while both event handlers are already armed."""
        command_timeout = deadline.protocol_timeout()
        await wait_for_zendriver(
            self._page.send(cdp.page.set_lifecycle_events_enabled(True)),
            timeout=command_timeout,
            owner=self._page,
        )
        deadline.require_remaining(
            "Lifecycle enable acknowledgement arrived after its deadline"
        )
        command_timeout = deadline.protocol_timeout()
        frame_tree = await wait_for_zendriver(
            self._page.send(cdp.page.get_frame_tree()),
            timeout=command_timeout,
            owner=self._page,
        )
        deadline.require_remaining(
            "Main-frame lookup result arrived after its lifecycle deadline"
        )
        frame = frame_tree.frame
        self._old_loader = (str(frame.id_), str(frame.loader_id))
        self._old_loader_id = frame.loader_id

    @property
    def old_loader_id(self) -> Any:
        if self._old_loader_id is None:
            raise RuntimeError("lifecycle waiter was not enabled")
        return self._old_loader_id

    def trigger(self) -> None:
        """Start accepting events immediately before the navigation mutation."""
        if self._closed:
            raise RuntimeError("lifecycle waiter is closed")
        self._triggered = True

    async def _on_frame_navigated(
        self, event: cdp.page.FrameNavigated, *_: Any
    ) -> None:
        if not self._triggered or self._future.done():
            return
        frame = event.frame
        if frame.parent_id is not None:
            return
        loader = (str(frame.id_), str(frame.loader_id))
        if not all(loader) or loader == self._old_loader:
            return
        self._main_loader = loader
        self._resolve_if_ready(loader)

    async def _on_lifecycle_event(
        self, event: cdp.page.LifecycleEvent, *_: Any
    ) -> None:
        if (
            not self._triggered
            or self._future.done()
            or event.name != "DOMContentLoaded"
        ):
            return
        loader = (str(event.frame_id), str(event.loader_id))
        if loader == self._old_loader:
            return
        self._dom_content_loaded.add(loader)
        self._resolve_if_ready(loader)

    def _resolve_if_ready(self, loader: tuple[str, str]) -> None:
        if loader == self._main_loader and loader in self._dom_content_loaded:
            self._future.set_result(loader)

    async def wait(self, deadline: SemanticDeadline) -> tuple[str, str]:
        """Wait locally; timing out cannot leave a protocol command in flight."""
        remaining = deadline.require_remaining(
            "Main-frame DOMContentLoaded deadline was exhausted"
        )
        try:
            async with asyncio.timeout(remaining):
                return await self._future
        except TimeoutError as error:
            raise TimeoutError(
                "New main-frame loader did not reach DOMContentLoaded"
            ) from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._page.remove_handlers(cdp.page.FrameNavigated, self._on_frame_navigated)
        self._page.remove_handlers(cdp.page.LifecycleEvent, self._on_lifecycle_event)
        if not self._future.done():
            self._future.cancel()


__all__ = ["MainFrameDOMContentLoadedWaiter"]

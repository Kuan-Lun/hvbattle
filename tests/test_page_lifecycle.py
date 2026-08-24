import asyncio
import unittest
from collections import defaultdict
from types import SimpleNamespace
from typing import Any

from zendriver import cdp

from hvbattle._page_lifecycle import MainFrameDOMContentLoadedWaiter
from hvbattle._timing import SemanticDeadline
from hvbattle.testing import TestingElementActionManager as ElementActionManager


class _LifecyclePage:
    def __init__(self) -> None:
        self.handlers: dict[type[object], list[Any]] = defaultdict(list)
        self.main_frame_id = "main-frame"
        self.loader_id = cdp.network.LoaderId("old-loader")
        self.commands: list[dict[str, object]] = []

    def add_handler(self, event_type: type[object], handler: Any) -> None:
        self.handlers[event_type].append(handler)

    def remove_handlers(self, event_type: type[object], handler: Any) -> None:
        self.handlers[event_type].remove(handler)

    async def send(self, command: Any) -> object:
        payload = next(command)
        self.commands.append(payload)
        method = payload["method"]
        if method == "Page.setLifecycleEventsEnabled":
            return None
        if method == "Page.getFrameTree":
            return SimpleNamespace(
                frame=SimpleNamespace(
                    id_=self.main_frame_id,
                    loader_id=self.loader_id,
                )
            )
        if method == "Page.reload":
            self.assert_reload_loader(payload)
            await self.emit_main_document("new-loader")
            return None
        raise AssertionError(f"Unexpected command: {method}")

    def assert_reload_loader(self, payload: dict[str, object]) -> None:
        if payload.get("params") != {"loaderId": "old-loader"}:
            raise AssertionError(f"Reload did not guard the old loader: {payload!r}")

    async def emit_frame(
        self,
        *,
        frame_id: str,
        loader_id: str,
        parent_id: str | None,
    ) -> None:
        event = SimpleNamespace(
            frame=SimpleNamespace(
                id_=frame_id,
                loader_id=loader_id,
                parent_id=parent_id,
            )
        )
        for handler in tuple(self.handlers[cdp.page.FrameNavigated]):
            await handler(event)

    async def emit_dom_content_loaded(
        self,
        *,
        frame_id: str,
        loader_id: str,
    ) -> None:
        event = SimpleNamespace(
            frame_id=frame_id,
            loader_id=loader_id,
            name="DOMContentLoaded",
        )
        for handler in tuple(self.handlers[cdp.page.LifecycleEvent]):
            await handler(event)

    async def emit_main_document(self, loader_id: str) -> None:
        await self.emit_frame(
            frame_id=self.main_frame_id,
            loader_id=loader_id,
            parent_id=None,
        )
        await self.emit_dom_content_loaded(
            frame_id=self.main_frame_id,
            loader_id=loader_id,
        )


class MainFrameLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_deadline_rejects_before_constructing_command(self) -> None:
        page = _LifecyclePage()
        waiter = MainFrameDOMContentLoadedWaiter(page)
        deadline = SemanticDeadline(expires_at=0.0, _clock=lambda: 1.0)

        with self.assertRaisesRegex(TimeoutError, "No protocol transaction"):
            await waiter.enable(deadline=deadline)

        self.assertEqual(page.commands, [])
        waiter.close()

    async def test_late_lifecycle_enable_ack_stops_before_frame_lookup(self) -> None:
        now = 0.0

        class _LatePage(_LifecyclePage):
            async def send(self, command: Any) -> object:
                nonlocal now
                result = await super().send(command)
                now = 2.0
                return result

        page = _LatePage()
        waiter = MainFrameDOMContentLoadedWaiter(page)
        deadline = SemanticDeadline(expires_at=1.0, _clock=lambda: now)

        with self.assertRaisesRegex(TimeoutError, "arrived after"):
            await waiter.enable(deadline=deadline)

        self.assertEqual(
            [command["method"] for command in page.commands],
            ["Page.setLifecycleEventsEnabled"],
        )
        waiter.close()

    async def test_old_loader_and_iframe_events_do_not_resolve_waiter(self) -> None:
        page = _LifecyclePage()
        deadline = SemanticDeadline.after(1)
        waiter = MainFrameDOMContentLoadedWaiter(page)
        await waiter.enable(deadline=deadline)
        waiter.trigger()
        wait_task = asyncio.create_task(waiter.wait(deadline))

        await page.emit_main_document("old-loader")
        await page.emit_frame(
            frame_id="child-frame",
            loader_id="child-loader",
            parent_id=page.main_frame_id,
        )
        await page.emit_dom_content_loaded(
            frame_id="child-frame",
            loader_id="child-loader",
        )
        await asyncio.sleep(0)
        self.assertFalse(wait_task.done())

        await page.emit_main_document("new-loader")
        self.assertEqual(
            await wait_task,
            (page.main_frame_id, "new-loader"),
        )
        waiter.close()
        self.assertEqual(page.handlers[cdp.page.FrameNavigated], [])
        self.assertEqual(page.handlers[cdp.page.LifecycleEvent], [])

    async def test_recovery_reload_guards_loader_and_waits_for_lifecycle(self) -> None:
        page = _LifecyclePage()
        manager = ElementActionManager(SimpleNamespace(page=page))

        await manager.reload_current_page(deadline=SemanticDeadline.after(1))

        self.assertEqual(
            [command["method"] for command in page.commands],
            [
                "Page.setLifecycleEventsEnabled",
                "Page.getFrameTree",
                "Page.reload",
            ],
        )
        self.assertEqual(page.handlers[cdp.page.FrameNavigated], [])
        self.assertEqual(page.handlers[cdp.page.LifecycleEvent], [])


if __name__ == "__main__":
    unittest.main()

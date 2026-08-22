import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import hvbattle.battle_state as state_module
from hvbattle.battle_state import BattleStateStore, CombatLogTracker


class CombatLogTrackerTests(unittest.TestCase):
    def test_repeated_message_is_reported_once_for_each_new_occurrence(self) -> None:
        tracker = CombatLogTracker()
        tracker.update(SimpleNamespace(log=SimpleNamespace(lines=["same hit"])))

        tracker.update(
            SimpleNamespace(log=SimpleNamespace(lines=["same hit", "same hit"]))
        )

        self.assertEqual(tracker.current_lines, ["same hit"])

    def test_empty_refresh_clears_delta_without_forgetting_history(self) -> None:
        tracker = CombatLogTracker()
        tracker.update(SimpleNamespace(log=SimpleNamespace(lines=["first"])))

        tracker.update(SimpleNamespace(log=SimpleNamespace(lines=[])))

        self.assertEqual(tracker.current_lines, [])
        self.assertEqual(list(tracker.prev_lines), ["first"])


class BattleStateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_content_timeout_keeps_late_protocol_future_alive(self) -> None:
        driver = Mock()
        content: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        driver.page.get_content = Mock(return_value=content)
        store = BattleStateStore(driver)

        with self.assertRaises(TimeoutError):
            await store._get_content(timeout=0.001)

        self.assertFalse(content.cancelled())
        content.set_result("<html>late</html>")
        await asyncio.sleep(0)

    async def test_refresh_does_not_mutate_driver_connection_mapper(self) -> None:
        driver = Mock()
        sentinel = object()
        driver.page.mapper = {7: sentinel}
        driver.page.get_content = AsyncMock(return_value="<html></html>")
        snapshot = SimpleNamespace(monsters={}, log=SimpleNamespace(lines=[]))
        store = BattleStateStore(driver)

        with patch.object(state_module, "parse_snapshot", return_value=snapshot):
            await store.update()

        self.assertEqual(driver.page.mapper, {7: sentinel})
        self.assertIs(store.snap, snapshot)

    async def test_duplicate_id_retry_uses_only_remaining_content_budget(self) -> None:
        driver = Mock()
        driver.page.get_content = Mock(return_value=object())
        store = BattleStateStore(driver)
        observed_timeouts: list[float] = []
        observed_owners: list[object] = []

        async def bounded_content_read(
            _content: object,
            *,
            timeout: float,
            owner: object,
        ) -> str:
            observed_timeouts.append(timeout)
            observed_owners.append(owner)
            if len(observed_timeouts) == 1:
                await asyncio.sleep(0.01)
                raise state_module.ProtocolException("duplicate id")
            return "<html></html>"

        with patch.object(
            state_module,
            "wait_for_zendriver",
            side_effect=bounded_content_read,
        ):
            content = await store._get_content(timeout=1)

        self.assertEqual(content, "<html></html>")
        self.assertGreater(observed_timeouts[0], 0)
        self.assertLessEqual(observed_timeouts[0], 1)
        self.assertGreater(observed_timeouts[1], 0)
        self.assertLess(observed_timeouts[1], 1)
        self.assertEqual(observed_owners, [driver.page, driver.page])

    def test_reset_replaces_all_battle_scoped_state(self) -> None:
        store = BattleStateStore(Mock())
        previous_log = store.log_entries
        store.snap = Mock()
        store.overview_monsters.alive_monster = [2]
        store.log_entries.prev_lines.append("old")

        store.reset()

        self.assertIsNone(store.snap)
        self.assertEqual(store.overview_monsters.alive_monster, [])
        self.assertIsNot(store.log_entries, previous_log)
        self.assertEqual(list(store.log_entries.prev_lines), [])


if __name__ == "__main__":
    unittest.main()

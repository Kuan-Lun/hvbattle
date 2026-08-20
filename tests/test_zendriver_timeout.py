import asyncio
import unittest
from types import SimpleNamespace

from hvbrowser.runtime import (
    ZendriverOperationTimeout,
    wait_for_zendriver,
)
from zendriver import cdp
from zendriver.core.connection import Transaction


class ZendriverTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_does_not_cancel_late_protocol_transaction(self) -> None:
        transaction = Transaction(cdp.runtime.evaluate("1", return_by_value=True))
        owner = SimpleNamespace()

        with self.assertRaises(ZendriverOperationTimeout):
            await wait_for_zendriver(transaction, timeout=0, owner=owner)

        self.assertFalse(transaction.cancelled())

        transaction(result={"result": {"type": "number", "value": 1}})
        await asyncio.sleep(0)

        remote_object, exception_details = transaction.result()
        self.assertEqual(remote_object.value, 1)
        self.assertIsNone(exception_details)

    async def test_caller_cancellation_does_not_cancel_protocol_future(self) -> None:
        protocol_future: asyncio.Future[str] = (
            asyncio.get_running_loop().create_future()
        )
        waiter = asyncio.create_task(
            wait_for_zendriver(
                protocol_future,
                timeout=60,
                owner=SimpleNamespace(),
            )
        )
        await asyncio.sleep(0)

        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        self.assertFalse(protocol_future.cancelled())
        protocol_future.set_result("late result")
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()

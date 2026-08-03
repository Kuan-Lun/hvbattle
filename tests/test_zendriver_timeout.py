import asyncio
import unittest

from zendriver import cdp
from zendriver.core.connection import Transaction

from hvbattle._zendriver import (
    _BACKGROUND_ZENDRIVER_OPERATIONS,
    wait_for_zendriver,
)


class ZendriverTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_does_not_cancel_late_protocol_transaction(self) -> None:
        transaction = Transaction(cdp.runtime.evaluate("1", return_by_value=True))

        with self.assertRaises(TimeoutError):
            await wait_for_zendriver(transaction, timeout=0)

        self.assertFalse(transaction.cancelled())
        self.assertIn(transaction, _BACKGROUND_ZENDRIVER_OPERATIONS)

        transaction(result={"result": {"type": "number", "value": 1}})
        await asyncio.sleep(0)

        remote_object, exception_details = transaction.result()
        self.assertEqual(remote_object.value, 1)
        self.assertIsNone(exception_details)
        self.assertNotIn(transaction, _BACKGROUND_ZENDRIVER_OPERATIONS)

    async def test_caller_cancellation_does_not_cancel_protocol_future(self) -> None:
        protocol_future: asyncio.Future[str] = (
            asyncio.get_running_loop().create_future()
        )
        waiter = asyncio.create_task(wait_for_zendriver(protocol_future, timeout=60))
        await asyncio.sleep(0)

        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        self.assertFalse(protocol_future.cancelled())
        protocol_future.set_result("late result")
        await asyncio.sleep(0)
        self.assertNotIn(protocol_future, _BACKGROUND_ZENDRIVER_OPERATIONS)


if __name__ == "__main__":
    unittest.main()

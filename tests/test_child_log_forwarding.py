import unittest
from unittest.mock import Mock, patch

from hvbattle import _ponychart_store_child, _ponychart_worker_entry, control_panel


class ChildLogForwardingLifecycleTests(unittest.TestCase):
    def test_gui_child_uses_a_forwarded_namespace_when_run_as_a_script(
        self,
    ) -> None:
        self.assertEqual(control_panel.logger.name, "hvbattle.control_panel")

    def test_ponychart_worker_preserves_business_result_and_closes(self) -> None:
        events: list[str] = []
        with (
            patch.object(
                _ponychart_worker_entry,
                "configure_forwarded_logging",
                side_effect=lambda: events.append("configure"),
            ),
            patch.object(
                _ponychart_worker_entry,
                "main",
                side_effect=lambda _arguments: events.append("business") or 7,
            ),
            patch.object(
                _ponychart_worker_entry,
                "close_forwarded_logging",
                side_effect=lambda: events.append("close"),
            ),
        ):
            result = _ponychart_worker_entry._run_owned_child(("invalid",))

        self.assertEqual(result, 7)
        self.assertEqual(events, ["configure", "business", "close"])

    def test_store_child_closes_after_business_failure(self) -> None:
        business_failure = RuntimeError("store failed")
        close = Mock()
        with (
            patch.object(_ponychart_store_child, "configure_forwarded_logging"),
            patch.object(
                _ponychart_store_child,
                "main",
                side_effect=business_failure,
            ),
            patch.object(_ponychart_store_child, "close_forwarded_logging", close),
            self.assertRaises(RuntimeError) as caught,
        ):
            _ponychart_store_child._run_owned_child(())

        self.assertIs(caught.exception, business_failure)
        close.assert_called_once_with()

    def test_gui_child_close_failure_does_not_replace_success(self) -> None:
        with (
            patch.object(control_panel, "configure_forwarded_logging"),
            patch.object(control_panel, "_run_gui_child", return_value=0),
            patch.object(
                control_panel,
                "close_forwarded_logging",
                side_effect=RuntimeError("forwarding close failed"),
            ),
        ):
            result = control_panel._run_owned_gui_child(("1", "token"))

        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()

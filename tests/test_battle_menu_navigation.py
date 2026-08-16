import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hvbrowser import (
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
    Realm,
    RealmNavigator,
)

from hvbattle.battle_launcher import BattleLauncher


def _markers(**overrides: bool) -> dict[str, bool]:
    markers = {
        "challenge": False,
        "completion": False,
        "nextFloor": False,
        "active": False,
    }
    markers.update(overrides)
    return markers


class _Element:
    def __init__(self) -> None:
        self.mouse_move = AsyncMock()
        self.mouse_click = AsyncMock()


class _Page:
    def __init__(self, current_url: str) -> None:
        self.current_url = current_url
        self.markers = _markers()
        self.marker_results: list[object] = []
        self.battle_menu = _Element()
        self.route_element = _Element()
        self.has_battle_menu = True
        self.has_route_element = True
        self.xpath_calls: list[tuple[str, int]] = []
        self.select_calls: list[str] = []
        self.route_ready = {"ar": True, "rb": True, "gr": True}
        self.route_ready_scripts: list[str] = []

    async def select(self, selector: str) -> _Element | None:
        self.select_calls.append(selector)
        if selector != "#parent_Battle":
            raise AssertionError(f"Unexpected selector: {selector}")
        return self.battle_menu if self.has_battle_menu else None

    async def xpath(self, selector: str, timeout: int) -> list[_Element]:
        self.xpath_calls.append((selector, timeout))
        return [self.route_element] if self.has_route_element else []

    async def evaluate(self, script: str) -> object:
        if script == "window.location.href":
            return self.current_url
        if "nextFloor" in script and "battle_main" in script:
            result = (
                self.marker_results.pop(0)
                if self.marker_results
                else dict(self.markers)
            )
            if isinstance(result, BaseException):
                raise result
            return result
        if script.startswith("Boolean(document.getElementById("):
            self.route_ready_scripts.append(script)
            if "arena_tokens" in script:
                return self.route_ready["rb"]
            if "grindfest" in script:
                return self.route_ready["gr"]
            if "arena_list" in script:
                return self.route_ready["ar"]
        raise AssertionError(f"Unexpected evaluate script: {script!r}")


class _Browser:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.click_destination: str | None = None
        self.direct_destination: str | None = None
        self.direct_ready_route: str | None = None
        self.wait_error: Exception | None = None
        self.get_calls: list[str] = []
        self.wait_calls: list[tuple[bool, int]] = []

    async def wait(
        self,
        fun: object,
        ischangeurl: bool,
        sleeptime: int = 1,
    ) -> None:
        self.wait_calls.append((ischangeurl, sleeptime))
        await fun()  # type: ignore[operator]
        if self.click_destination is not None:
            self.page.current_url = self.click_destination
        if self.wait_error is not None:
            raise self.wait_error

    async def get(self, url: str) -> None:
        self.get_calls.append(url)
        self.page.current_url = self.direct_destination or url
        if self.direct_ready_route is not None:
            self.page.route_ready[self.direct_ready_route] = True


def _launcher(
    *,
    realm: Realm = Realm.PERSISTENT,
) -> tuple[BattleLauncher, _Browser, _Page]:
    root = (
        "https://hentaiverse.org/isekai/"
        if realm is Realm.ISEKAI
        else ("https://hentaiverse.org/")
    )
    page = _Page(root)
    browser = _Browser(page)
    realm_navigator = SimpleNamespace(current=AsyncMock(return_value=realm))
    launcher = BattleLauncher(browser, realm_navigator)  # type: ignore[arg-type]
    return launcher, browser, page


class BattleMenuNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_public_navigation_uses_its_clickable_route(self) -> None:
        cases = (
            ("goto_arena", "ar"),
            ("goto_ring_of_blood", "rb"),
            ("goto_grindfest", "gr"),
        )

        for method_name, route in cases:
            with self.subTest(method=method_name):
                launcher, browser, page = _launcher()
                browser.click_destination = (
                    f"https://hentaiverse.org/?s=Battle&ss={route}"
                )

                reached = await getattr(launcher, method_name)()

                self.assertTrue(reached)
                self.assertEqual(page.select_calls, ["#parent_Battle"])
                self.assertEqual(len(page.xpath_calls), 1)
                xpath, timeout = page.xpath_calls[0]
                self.assertEqual(timeout, 5)
                self.assertIn("//*[@id='child_Battle']", xpath)
                self.assertIn("@onclick", xpath)
                self.assertIn("@href", xpath)
                self.assertIn("s=Battle", xpath)
                self.assertIn(f"ss={route}", xpath)
                self.assertNotIn("contains(text()", xpath)
                page.battle_menu.mouse_move.assert_awaited_once_with()
                page.route_element.mouse_move.assert_awaited_once_with()
                page.route_element.mouse_click.assert_awaited_once_with()
                self.assertEqual(browser.wait_calls, [(True, 1)])
                self.assertEqual(browser.get_calls, [])
                self.assertEqual(len(page.route_ready_scripts), 1)
                self.assertIn(
                    (
                        "arena_tokens"
                        if route == "rb"
                        else "grindfest" if route == "gr" else "arena_list"
                    ),
                    page.route_ready_scripts[0],
                )

    async def test_click_noop_retries_once_through_direct_url(self) -> None:
        launcher, browser, page = _launcher()

        reached = await launcher.goto_ring_of_blood()

        self.assertTrue(reached)
        page.route_element.mouse_click.assert_awaited_once_with()
        self.assertEqual(
            browser.get_calls,
            ["https://hentaiverse.org/?s=Battle&ss=rb"],
        )

    async def test_correct_ring_url_without_structure_reloads_directly_once(
        self,
    ) -> None:
        launcher, browser, page = _launcher()
        browser.click_destination = "https://hentaiverse.org/?s=Battle&ss=rb"
        page.route_ready["rb"] = False
        browser.direct_ready_route = "rb"

        reached = await launcher.goto_ring_of_blood()

        self.assertTrue(reached)
        self.assertEqual(
            browser.get_calls,
            ["https://hentaiverse.org/?s=Battle&ss=rb"],
        )
        self.assertEqual(len(page.route_ready_scripts), 2)
        self.assertTrue(
            all("arena_tokens" in script for script in page.route_ready_scripts)
        )

    async def test_missing_menu_uses_realm_scoped_direct_urls(self) -> None:
        cases = (
            (Realm.PERSISTENT, "goto_arena", "https://hentaiverse.org/?s=Battle&ss=ar"),
            (
                Realm.ISEKAI,
                "goto_ring_of_blood",
                "https://hentaiverse.org/isekai/?s=Battle&ss=rb",
            ),
            (
                Realm.ISEKAI,
                "goto_grindfest",
                "https://hentaiverse.org/isekai/?s=Battle&ss=gr",
            ),
        )

        for realm, method_name, expected_url in cases:
            with self.subTest(realm=realm, method=method_name):
                launcher, browser, page = _launcher(realm=realm)
                page.has_battle_menu = False

                reached = await getattr(launcher, method_name)()

                self.assertTrue(reached)
                self.assertEqual(browser.get_calls, [expected_url])
                page.route_element.mouse_click.assert_not_awaited()

    async def test_wrong_realm_after_menu_click_fails_without_direct_retry(
        self,
    ) -> None:
        launcher, browser, _page = _launcher()
        browser.click_destination = "https://hentaiverse.org/isekai/?s=Battle&ss=rb"

        with self.assertRaisesRegex(RuntimeError, "wrong realm"):
            await launcher.goto_ring_of_blood()

        self.assertEqual(browser.get_calls, [])

    async def test_wait_error_after_wrong_realm_click_still_fails_closed(self) -> None:
        launcher, browser, _page = _launcher()
        browser.click_destination = "https://hentaiverse.org/isekai/?s=Battle&ss=rb"
        browser.wait_error = TimeoutError("URL transition wait failed")

        with self.assertRaisesRegex(RuntimeError, "wrong realm"):
            await launcher.goto_ring_of_blood()

        self.assertEqual(browser.get_calls, [])

    async def test_unexpected_path_after_menu_click_fails_without_direct_retry(
        self,
    ) -> None:
        launcher, browser, _page = _launcher()
        browser.click_destination = "https://hentaiverse.org/untrusted/?s=Battle&ss=ar"

        with self.assertRaisesRegex(RuntimeError, "unexpected path"):
            await launcher.goto_arena()

        self.assertEqual(browser.get_calls, [])

    async def test_untrusted_origin_after_menu_click_fails_without_direct_retry(
        self,
    ) -> None:
        launcher, browser, _page = _launcher()
        browser.click_destination = "https://example.invalid/?s=Battle&ss=gr"

        with self.assertRaisesRegex(RuntimeError, "Unable to verify"):
            await launcher.goto_grindfest()

        self.assertEqual(browser.get_calls, [])

    async def test_each_initial_battle_blocker_prevents_all_navigation(self) -> None:
        cases = (
            ("challenge", MaintenanceNavigationBlocker.CHALLENGE),
            ("completion", MaintenanceNavigationBlocker.COMPLETION),
            ("nextFloor", MaintenanceNavigationBlocker.NEXT_FLOOR),
            ("active", MaintenanceNavigationBlocker.ACTIVE),
        )

        for marker_name, expected_blocker in cases:
            with self.subTest(marker=marker_name):
                launcher, browser, page = _launcher()
                page.markers = _markers(**{marker_name: True})

                with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
                    await launcher.goto_arena()

                self.assertIs(raised.exception.blocker, expected_blocker)
                self.assertEqual(page.select_calls, [])
                self.assertEqual(page.xpath_calls, [])
                page.battle_menu.mouse_move.assert_not_awaited()
                page.route_element.mouse_move.assert_not_awaited()
                page.route_element.mouse_click.assert_not_awaited()
                self.assertEqual(browser.wait_calls, [])
                self.assertEqual(browser.get_calls, [])

    async def test_blocker_before_direct_fallback_prevents_direct_navigation(
        self,
    ) -> None:
        launcher, browser, page = _launcher()
        page.has_battle_menu = False
        page.marker_results = [
            _markers(),
            _markers(nextFloor=True),
        ]

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await launcher.goto_arena()

        self.assertIs(
            raised.exception.blocker,
            MaintenanceNavigationBlocker.NEXT_FLOOR,
        )
        self.assertEqual(page.select_calls, ["#parent_Battle"])
        self.assertEqual(page.xpath_calls, [])
        page.route_element.mouse_click.assert_not_awaited()
        self.assertEqual(browser.wait_calls, [])
        self.assertEqual(browser.get_calls, [])

    async def test_blocker_after_direct_landing_stops_without_more_navigation(
        self,
    ) -> None:
        launcher, browser, page = _launcher()
        page.has_battle_menu = False
        page.marker_results = [
            _markers(),
            _markers(),
            _markers(challenge=True),
        ]

        with self.assertRaises(MaintenanceNavigationBlockedError) as raised:
            await launcher.goto_ring_of_blood()

        self.assertIs(
            raised.exception.blocker,
            MaintenanceNavigationBlocker.CHALLENGE,
        )
        self.assertEqual(
            browser.get_calls,
            ["https://hentaiverse.org/?s=Battle&ss=rb"],
        )
        page.route_element.mouse_click.assert_not_awaited()
        self.assertEqual(page.route_ready_scripts, [])

    async def test_invalid_battle_marker_read_fails_closed_before_navigation(
        self,
    ) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("malformed", {"challenge": False}),
            ("evaluation-error", RuntimeError("marker evaluation failed")),
        )

        for case_name, marker_result in cases:
            with self.subTest(case=case_name):
                launcher, browser, page = _launcher()
                page.marker_results = [marker_result]

                with self.assertRaisesRegex(
                    RuntimeError,
                    "Unable to verify battle state before opening Battle menu",
                ):
                    await launcher.goto_grindfest()

                self.assertEqual(page.select_calls, [])
                self.assertEqual(page.xpath_calls, [])
                page.route_element.mouse_click.assert_not_awaited()
                self.assertEqual(browser.wait_calls, [])
                self.assertEqual(browser.get_calls, [])

    async def test_realm_lookup_error_fails_closed_before_navigation(self) -> None:
        page = _Page("https://hentaiverse.org/")
        browser = _Browser(page)
        realm_navigator = SimpleNamespace(
            current=AsyncMock(side_effect=RuntimeError("realm lookup failed"))
        )
        launcher = BattleLauncher(  # type: ignore[arg-type]
            browser,
            realm_navigator,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Unable to determine the current Battle navigation realm",
        ):
            await launcher.goto_arena()

        self.assertEqual(page.select_calls, [])
        self.assertEqual(page.xpath_calls, [])
        page.route_element.mouse_click.assert_not_awaited()
        self.assertEqual(browser.wait_calls, [])
        self.assertEqual(browser.get_calls, [])

    async def test_untrusted_start_url_fails_closed_before_navigation(self) -> None:
        page = _Page("https://example.invalid/?s=Battle&ss=ar")
        browser = _Browser(page)
        realm_navigator = RealmNavigator(browser)  # type: ignore[arg-type]
        launcher = BattleLauncher(browser, realm_navigator)  # type: ignore[arg-type]

        with self.assertRaisesRegex(
            RuntimeError,
            "Unable to determine the current Battle navigation realm",
        ):
            await launcher.goto_arena()

        self.assertEqual(page.select_calls, [])
        self.assertEqual(page.xpath_calls, [])
        page.route_element.mouse_click.assert_not_awaited()
        self.assertEqual(browser.wait_calls, [])
        self.assertEqual(browser.get_calls, [])

    async def test_wrong_route_falls_back_only_once_and_remains_fail_closed(
        self,
    ) -> None:
        launcher, browser, _page = _launcher()
        browser.click_destination = "https://hentaiverse.org/?s=Battle&ss=ar"
        browser.direct_destination = "https://hentaiverse.org/?s=Battle&ss=ar"

        with self.assertRaisesRegex(RuntimeError, "did not land"):
            await launcher.goto_ring_of_blood()

        self.assertEqual(
            browser.get_calls,
            ["https://hentaiverse.org/?s=Battle&ss=rb"],
        )


if __name__ == "__main__":
    unittest.main()

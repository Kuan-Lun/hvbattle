import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hvbrowser import (
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
    Realm,
)
from hvbrowser.runtime import LogPersistenceError, ZendriverOperationTimeout

from hvbattle import BattlePresence, BattleRouteReadinessError, BattleSession
from hvbattle import battle_launcher as battle_launcher_module
from hvbattle import session as session_module
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


def _observation(url: str, **markers: bool) -> dict[str, object]:
    return {"url": url, **_markers(**markers)}


def _route_observation(
    url: str,
    *,
    route_ready: bool,
    **markers: bool,
) -> dict[str, object]:
    return {
        "url": url,
        **_markers(**markers),
        "routeReady": route_ready,
    }


def _realm_root(realm: Realm) -> str:
    if realm is Realm.ISEKAI:
        return "https://hentaiverse.org/isekai/"
    return "https://hentaiverse.org/"


class _Page:
    def __init__(self, current_url: str) -> None:
        self.current_url = current_url
        self.markers = _markers()
        self.observation_results: list[object] = []
        self.route_observation_results: list[object] = []
        self.route_ready: dict[str, object] = {
            "ar": True,
            "rb": True,
            "gr": True,
        }
        self.observation_calls = 0
        self.route_observation_calls = 0
        self.route_readiness_scripts: list[str] = []
        self.select_calls: list[str] = []
        self.xpath_calls: list[str] = []

    async def select(self, selector: str, **_kwargs: object) -> object:
        self.select_calls.append(selector)
        raise AssertionError("Battle navigation must not use menu selection")

    async def xpath(self, selector: str, **_kwargs: object) -> object:
        self.xpath_calls.append(selector)
        raise AssertionError("Battle navigation must not use XPath menu lookup")

    async def evaluate(self, script: str) -> object:
        if "routeReady:" in script:
            self.route_observation_calls += 1
            self.route_readiness_scripts.append(script)
            if self.route_observation_results:
                result = self.route_observation_results.pop(0)
            elif "arena_tokens" in script:
                result = _route_observation(
                    self.current_url,
                    route_ready=self.route_ready["rb"] is True,
                    **self.markers,
                )
                if isinstance(self.route_ready["rb"], BaseException):
                    result = self.route_ready["rb"]
            elif "grindfest" in script:
                result = _route_observation(
                    self.current_url,
                    route_ready=self.route_ready["gr"] is True,
                    **self.markers,
                )
                if isinstance(self.route_ready["gr"], BaseException):
                    result = self.route_ready["gr"]
            elif "arena_list" in script:
                result = _route_observation(
                    self.current_url,
                    route_ready=self.route_ready["ar"] is True,
                    **self.markers,
                )
                if isinstance(self.route_ready["ar"], BaseException):
                    result = self.route_ready["ar"]
            else:
                raise AssertionError(f"Unexpected route script: {script!r}")
            if isinstance(result, BaseException):
                raise result
            return result
        if "url: window.location.href" in script and "nextFloor" in script:
            self.observation_calls += 1
            result = (
                self.observation_results.pop(0)
                if self.observation_results
                else {"url": self.current_url, **self.markers}
            )
            if isinstance(result, BaseException):
                raise result
            return result
        raise AssertionError(f"Unexpected evaluate script: {script!r}")


class _Browser:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.direct_destination: str | None = None
        self.get_error: Exception | None = None
        self.get_calls: list[str] = []
        self.wait_calls: list[object] = []
        self.diagnostic_calls: list[str] = []
        self.diagnostic_path: Path | None = None
        self.diagnostic_error: Exception | None = None

    async def get(self, url: str) -> None:
        self.get_calls.append(url)
        if self.get_error is not None:
            raise self.get_error
        self.page.current_url = self.direct_destination or url

    async def wait(self, *_args: object, **_kwargs: object) -> None:
        self.wait_calls.append((_args, _kwargs))
        raise AssertionError("Battle route navigation must not click menu elements")

    async def save_page_diagnostic(self, kind: str) -> Path | None:
        self.diagnostic_calls.append(kind)
        if self.diagnostic_error is not None:
            raise self.diagnostic_error
        return self.diagnostic_path


def _launcher(
    *,
    current_realm: Realm = Realm.PERSISTENT,
) -> tuple[BattleLauncher, _Browser, _Page]:
    page = _Page(_realm_root(current_realm))
    browser = _Browser(page)
    realm_navigator = SimpleNamespace(current=AsyncMock(return_value=current_realm))
    launcher = BattleLauncher(browser, realm_navigator)  # type: ignore[arg-type]
    return launcher, browser, page


def _session_with_launcher(
    *,
    current_realm: Realm = Realm.PERSISTENT,
) -> tuple[BattleSession, _Browser, _Page]:
    launcher, browser, page = _launcher(current_realm=current_realm)
    session = object.__new__(BattleSession)
    session._completion_observed = False
    session._launcher = launcher
    return session, browser, page


class BattleDirectNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_navigation_uses_expected_realm_canonical_get(self) -> None:
        cases = (
            (
                Realm.PERSISTENT,
                "goto_arena",
                "ar",
                "https://hentaiverse.org/?s=Battle&ss=ar",
            ),
            (
                Realm.PERSISTENT,
                "goto_ring_of_blood",
                "rb",
                "https://hentaiverse.org/?s=Battle&ss=rb",
            ),
            (
                Realm.PERSISTENT,
                "goto_grindfest",
                "gr",
                "https://hentaiverse.org/?s=Battle&ss=gr",
            ),
            (
                Realm.ISEKAI,
                "goto_arena",
                "ar",
                "https://hentaiverse.org/isekai/?s=Battle&ss=ar",
            ),
            (
                Realm.ISEKAI,
                "goto_ring_of_blood",
                "rb",
                "https://hentaiverse.org/isekai/?s=Battle&ss=rb",
            ),
            (
                Realm.ISEKAI,
                "goto_grindfest",
                "gr",
                "https://hentaiverse.org/isekai/?s=Battle&ss=gr",
            ),
        )

        for expected_realm, method_name, route, expected_url in cases:
            with self.subTest(realm=expected_realm, method=method_name):
                launcher, browser, page = _launcher(current_realm=expected_realm)

                reached = await getattr(launcher, method_name)(
                    expected_realm=expected_realm
                )

                self.assertTrue(reached)
                self.assertEqual(browser.get_calls, [expected_url])
                self.assertEqual(page.observation_calls, 1)
                self.assertEqual(page.route_observation_calls, 1)
                self.assertEqual(page.select_calls, [])
                self.assertEqual(page.xpath_calls, [])
                self.assertEqual(browser.wait_calls, [])
                self.assertEqual(len(page.route_readiness_scripts), 1)
                expected_marker = {
                    "ar": "arena_list",
                    "rb": "arena_tokens",
                    "gr": "grindfest",
                }[route]
                self.assertIn(expected_marker, page.route_readiness_scripts[0])

    def test_launcher_contains_no_menu_hover_click_or_separate_url_read(self) -> None:
        source = inspect.getsource(BattleLauncher)

        for forbidden in (
            ".select(",
            ".xpath(",
            "mouse_move",
            "mouse_click",
            "_open_battle_route_from_menu",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

        trust_boundary_source = "\n".join(
            inspect.getsource(method)
            for method in (
                BattleLauncher._goto_battle_route,
                BattleLauncher._observe_navigation,
                BattleLauncher._observe_battle_route_readiness,
                BattleLauncher._wait_for_battle_route_readiness,
                BattleLauncher.reconcile_startup_battle_route,
            )
        )
        self.assertNotIn(
            'evaluate("window.location.href")',
            trust_boundary_source,
        )
        self.assertIn("observe_maintenance_navigation", trust_boundary_source)
        readiness_script_source = inspect.getsource(
            battle_launcher_module._battle_route_readiness_script
        )
        self.assertIn("routeReady", readiness_script_source)
        self.assertIn("url: window.location.href", readiness_script_source)

    async def test_expected_realm_is_required_and_type_checked(self) -> None:
        launcher, browser, page = _launcher()

        with self.assertRaises(TypeError):
            await launcher.goto_arena()  # type: ignore[call-arg]
        with self.assertRaisesRegex(TypeError, "expected_realm must be a Realm"):
            await launcher.goto_arena(expected_realm="persistent")  # type: ignore[arg-type]

        self.assertEqual(browser.get_calls, [])
        self.assertEqual(page.observation_calls, 0)

    async def test_trusted_preflight_markers_surface_as_blockers(self) -> None:
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
                    await launcher.goto_arena(expected_realm=Realm.PERSISTENT)

                self.assertIs(raised.exception.blocker, expected_blocker)
                self.assertEqual(browser.get_calls, [])
                self.assertEqual(page.route_readiness_scripts, [])

    async def test_trusted_post_get_markers_surface_as_blockers(self) -> None:
        cases = (
            ("challenge", MaintenanceNavigationBlocker.CHALLENGE),
            ("completion", MaintenanceNavigationBlocker.COMPLETION),
            ("nextFloor", MaintenanceNavigationBlocker.NEXT_FLOOR),
            ("active", MaintenanceNavigationBlocker.ACTIVE),
        )

        for marker_name, expected_blocker in cases:
            with self.subTest(marker=marker_name):
                launcher, browser, page = _launcher()
                encounter_secret = f"secret-{marker_name}-must-not-leak"
                page.observation_results = [
                    _observation("https://hentaiverse.org/"),
                ]
                page.route_observation_results = [
                    _route_observation(
                        "https://hentaiverse.org/?s=Battle&ss=ba&encounter="
                        f"{encounter_secret}",
                        route_ready=False,
                        **{marker_name: True},
                    ),
                ]

                with (
                    patch.object(battle_launcher_module.logger, "debug") as debug,
                    patch.object(battle_launcher_module.logger, "warning") as warning,
                    self.assertRaises(MaintenanceNavigationBlockedError) as raised,
                ):
                    await launcher.goto_ring_of_blood(expected_realm=Realm.PERSISTENT)

                self.assertIs(raised.exception.blocker, expected_blocker)
                visible_output = (
                    f"{raised.exception!r} {debug.call_args_list!r} "
                    f"{warning.call_args_list!r}"
                )
                self.assertNotIn(encounter_secret, visible_output)
                self.assertEqual(
                    browser.get_calls,
                    ["https://hentaiverse.org/?s=Battle&ss=rb"],
                )
                self.assertEqual(page.route_observation_calls, 1)
                self.assertEqual(browser.diagnostic_calls, [])

    async def test_marker_never_wins_over_untrusted_preflight_identity(self) -> None:
        cases = (
            (
                "origin",
                "https://example.invalid/?s=Battle&ss=ar",
                Realm.PERSISTENT,
                "untrusted origin",
            ),
            (
                "realm",
                "https://hentaiverse.org/isekai/?s=Battle&ss=ar",
                Realm.PERSISTENT,
                "wrong realm",
            ),
            (
                "path",
                "https://hentaiverse.org/unexpected?s=Battle&ss=ar",
                Realm.PERSISTENT,
                "unexpected path",
            ),
        )

        for case_name, url, expected_realm, message in cases:
            with self.subTest(case=case_name):
                launcher, browser, page = _launcher()
                page.observation_results = [_observation(url, active=True)]

                with self.assertRaisesRegex(RuntimeError, message) as raised:
                    await launcher.goto_arena(expected_realm=expected_realm)

                self.assertNotIsInstance(
                    raised.exception,
                    MaintenanceNavigationBlockedError,
                )
                self.assertEqual(browser.get_calls, [])

    async def test_post_get_marker_requires_trusted_route_until_deadline(self) -> None:
        cases = (
            (
                "origin",
                "https://example.invalid/?s=Battle&ss=ar",
                "untrusted-origin",
            ),
            (
                "realm",
                "https://hentaiverse.org/isekai/?s=Battle&ss=ar",
                "wrong-realm",
            ),
            (
                "path",
                "https://hentaiverse.org/unexpected?s=Battle&ss=ar",
                "wrong-path",
            ),
        )

        for case_name, landed_url, expected_state in cases:
            with self.subTest(case=case_name):
                launcher, browser, page = _launcher()
                page.observation_results = [_observation("https://hentaiverse.org/")]
                browser.direct_destination = landed_url
                browser.diagnostic_path = Path("/tmp/battle_route_not_ready.html")
                page.markers = _markers(completion=True)

                with (
                    patch.object(
                        battle_launcher_module,
                        "_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS",
                        0.01,
                    ),
                    patch.object(
                        battle_launcher_module,
                        "_BATTLE_ROUTE_READINESS_POLL_SECONDS",
                        0.001,
                    ),
                    self.assertRaises(BattleRouteReadinessError) as raised,
                ):
                    await launcher.goto_arena(expected_realm=Realm.PERSISTENT)

                self.assertEqual(raised.exception.last_state, expected_state)
                self.assertNotIsInstance(
                    raised.exception,
                    MaintenanceNavigationBlockedError,
                )
                self.assertEqual(
                    browser.get_calls,
                    ["https://hentaiverse.org/?s=Battle&ss=ar"],
                )
                self.assertGreater(page.route_observation_calls, 1)
                self.assertEqual(
                    browser.diagnostic_calls,
                    ["battle_route_not_ready"],
                )

    async def test_marker_free_untrusted_preflight_fails_for_regular_goto(self) -> None:
        cases = (
            ("https://example.invalid/", "untrusted origin"),
            ("https://hentaiverse.org/isekai/", "wrong realm"),
            ("https://hentaiverse.org/unexpected", "unexpected path"),
        )

        for url, message in cases:
            with self.subTest(url=url):
                launcher, browser, page = _launcher()
                page.observation_results = [_observation(url)]

                with self.assertRaisesRegex(RuntimeError, message):
                    await launcher.goto_arena(expected_realm=Realm.PERSISTENT)

                self.assertEqual(browser.get_calls, [])

    async def test_transient_loading_dom_is_polled_until_ready(self) -> None:
        launcher, browser, page = _launcher()
        destination = "https://hentaiverse.org/?s=Battle&ss=ar"
        page.route_observation_results = [
            _route_observation(destination, route_ready=False),
            _route_observation(destination, route_ready=False),
            _route_observation(destination, route_ready=True),
        ]

        with patch.object(
            battle_launcher_module,
            "_BATTLE_ROUTE_READINESS_POLL_SECONDS",
            0,
        ):
            reached = await launcher.goto_arena(expected_realm=Realm.PERSISTENT)

        self.assertTrue(reached)
        self.assertEqual(page.route_observation_calls, 3)
        self.assertEqual(browser.diagnostic_calls, [])

    async def test_transient_unknown_documents_are_polled_until_trusted(self) -> None:
        launcher, browser, page = _launcher()
        page.route_observation_results = [
            _route_observation(
                "https://example.invalid/?s=Battle&ss=ar",
                route_ready=True,
                active=True,
            ),
            _route_observation(
                "https://hentaiverse.org/isekai/?s=Battle&ss=ar",
                route_ready=True,
                completion=True,
            ),
            _route_observation(
                "https://hentaiverse.org/?s=Battle&ss=rb",
                route_ready=True,
            ),
            _route_observation(
                "https://hentaiverse.org/?s=Battle&ss=ar",
                route_ready=True,
            ),
        ]

        with patch.object(
            battle_launcher_module,
            "_BATTLE_ROUTE_READINESS_POLL_SECONDS",
            0,
        ):
            reached = await launcher.goto_arena(expected_realm=Realm.PERSISTENT)

        self.assertTrue(reached)
        self.assertEqual(page.route_observation_calls, 4)
        self.assertEqual(browser.diagnostic_calls, [])

    async def test_destination_route_validation_waits_until_deadline(self) -> None:
        cases = (
            (
                "wrong-query",
                "https://hentaiverse.org/?s=Battle&ss=ar",
            ),
            (
                "missing-query",
                "https://hentaiverse.org/",
            ),
            (
                "extra-query",
                "https://hentaiverse.org/?s=Battle&ss=rb&foo=bar",
            ),
        )

        for case_name, destination in cases:
            with self.subTest(case=case_name):
                launcher, browser, page = _launcher()
                browser.direct_destination = destination
                browser.diagnostic_path = Path("/tmp/battle_route_not_ready.html")

                with (
                    patch.object(
                        battle_launcher_module,
                        "_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS",
                        0.01,
                    ),
                    patch.object(
                        battle_launcher_module,
                        "_BATTLE_ROUTE_READINESS_POLL_SECONDS",
                        0.001,
                    ),
                    self.assertRaises(BattleRouteReadinessError) as raised,
                ):
                    await launcher.goto_ring_of_blood(expected_realm=Realm.PERSISTENT)

                self.assertEqual(raised.exception.last_state, "wrong-query")
                self.assertEqual(
                    raised.exception.diagnostic_path,
                    "/tmp/battle_route_not_ready.html",
                )
                self.assertEqual(
                    browser.get_calls,
                    ["https://hentaiverse.org/?s=Battle&ss=rb"],
                )
                self.assertGreater(page.route_observation_calls, 1)
                self.assertEqual(
                    browser.diagnostic_calls,
                    ["battle_route_not_ready"],
                )

    async def test_missing_route_structure_waits_then_saves_diagnostic(self) -> None:
        launcher, browser, page = _launcher()
        page.route_ready["rb"] = False
        browser.diagnostic_path = Path("/tmp/battle_route_not_ready.html")

        with (
            patch.object(
                battle_launcher_module,
                "_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS",
                0.01,
            ),
            patch.object(
                battle_launcher_module,
                "_BATTLE_ROUTE_READINESS_POLL_SECONDS",
                0.001,
            ),
            self.assertRaises(BattleRouteReadinessError) as raised,
        ):
            await launcher.goto_ring_of_blood(expected_realm=Realm.PERSISTENT)

        self.assertEqual(raised.exception.last_state, "route-dom-missing")
        self.assertEqual(
            raised.exception.diagnostic_path,
            "/tmp/battle_route_not_ready.html",
        )
        self.assertEqual(
            browser.get_calls,
            ["https://hentaiverse.org/?s=Battle&ss=rb"],
        )
        self.assertGreater(page.route_observation_calls, 1)
        self.assertEqual(browser.diagnostic_calls, ["battle_route_not_ready"])

    async def test_ordinary_diagnostic_failure_does_not_mask_readiness(self) -> None:
        launcher, browser, page = _launcher()
        page.route_ready["ar"] = False
        browser.diagnostic_error = RuntimeError("diagnostic write failed")

        with (
            patch.object(
                battle_launcher_module,
                "_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS",
                0.005,
            ),
            patch.object(
                battle_launcher_module,
                "_BATTLE_ROUTE_READINESS_POLL_SECONDS",
                0.001,
            ),
            self.assertRaises(BattleRouteReadinessError) as raised,
        ):
            await launcher.goto_arena(expected_realm=Realm.PERSISTENT)

        self.assertEqual(
            raised.exception.diagnostic_error_type,
            "RuntimeError",
        )
        self.assertIsNone(raised.exception.diagnostic_path)
        self.assertEqual(
            browser.diagnostic_calls,
            ["battle_route_not_ready"],
        )

    async def test_fatal_diagnostic_errors_propagate_exactly(self) -> None:
        nested_sink_failure = RuntimeError("diagnostic wrapper")
        nested_sink_failure.__cause__ = LogPersistenceError(
            "emit",
            OSError("nested disk failure"),
        )
        cases = (
            LogPersistenceError("emit", OSError("disk failed")),
            nested_sink_failure,
            ZendriverOperationTimeout(timeout_seconds=5.0),
        )

        for diagnostic_error in cases:
            with self.subTest(error_type=type(diagnostic_error).__name__):
                launcher, browser, page = _launcher()
                page.route_ready["ar"] = False
                browser.diagnostic_error = diagnostic_error

                with (
                    patch.object(
                        battle_launcher_module,
                        "_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS",
                        0.005,
                    ),
                    patch.object(
                        battle_launcher_module,
                        "_BATTLE_ROUTE_READINESS_POLL_SECONDS",
                        0.001,
                    ),
                    self.assertRaises(type(diagnostic_error)) as raised,
                ):
                    await launcher.goto_arena(expected_realm=Realm.PERSISTENT)

                self.assertIs(raised.exception, diagnostic_error)
                self.assertEqual(
                    browser.diagnostic_calls,
                    ["battle_route_not_ready"],
                )

    async def test_nested_log_failure_in_readiness_observation_is_not_retried(
        self,
    ) -> None:
        launcher, browser, page = _launcher()
        wrapped = RuntimeError("readiness wrapper")
        wrapped.__cause__ = LogPersistenceError(
            "emit",
            OSError("nested log sink failure"),
        )
        page.route_observation_results = [wrapped]

        with self.assertRaises(RuntimeError) as raised:
            await launcher.goto_arena(expected_realm=Realm.PERSISTENT)

        self.assertIs(raised.exception, wrapped)
        self.assertEqual(page.route_observation_calls, 1)
        self.assertEqual(browser.diagnostic_calls, [])

    async def test_invalid_atomic_observation_fails_closed_before_get(self) -> None:
        launcher, browser, page = _launcher()
        page.observation_results = [
            {"url": "https://hentaiverse.org/", "challenge": False}
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "Unable to observe trusted Battle navigation state",
        ):
            await launcher.goto_grindfest(expected_realm=Realm.PERSISTENT)

        self.assertEqual(browser.get_calls, [])

    async def test_invalid_post_get_observation_is_retried(self) -> None:
        launcher, browser, page = _launcher()
        destination = "https://hentaiverse.org/?s=Battle&ss=gr"
        page.route_observation_results = [
            {"url": destination, "routeReady": True},
            _route_observation(destination, route_ready=True),
        ]

        with patch.object(
            battle_launcher_module,
            "_BATTLE_ROUTE_READINESS_POLL_SECONDS",
            0,
        ):
            reached = await launcher.goto_grindfest(expected_realm=Realm.PERSISTENT)

        self.assertTrue(reached)
        self.assertEqual(page.route_observation_calls, 2)
        self.assertEqual(browser.diagnostic_calls, [])

    async def test_ordinary_get_error_is_wrapped_without_retry(self) -> None:
        launcher, browser, _page = _launcher()
        browser.get_error = RuntimeError("navigation failed")

        with self.assertRaisesRegex(RuntimeError, "navigation outcome is unknown"):
            await launcher.goto_arena(expected_realm=Realm.PERSISTENT)

        self.assertEqual(
            browser.get_calls,
            ["https://hentaiverse.org/?s=Battle&ss=ar"],
        )

    async def test_generation_errors_propagate_without_retry(self) -> None:
        async def run_case(case_name: str) -> None:
            launcher, browser, page = _launcher()
            timeout = ZendriverOperationTimeout(timeout_seconds=5.0)
            if case_name == "pre-observation":
                page.observation_results = [timeout]
            elif case_name == "get":
                browser.get_error = timeout
            elif case_name == "post-observation":
                page.observation_results = [_observation("https://hentaiverse.org/")]
                page.route_observation_results = [timeout]
            elif case_name == "structure":
                page.route_ready["ar"] = timeout
            else:
                raise AssertionError(case_name)

            with self.assertRaises(ZendriverOperationTimeout) as raised:
                await launcher.goto_arena(expected_realm=Realm.PERSISTENT)

            self.assertIs(raised.exception, timeout)
            self.assertLessEqual(len(browser.get_calls), 1)
            self.assertLessEqual(page.route_observation_calls, 1)
            self.assertEqual(browser.diagnostic_calls, [])

        for case_name in (
            "pre-observation",
            "get",
            "post-observation",
            "structure",
        ):
            with self.subTest(case=case_name):
                await run_case(case_name)


class StartupBattlePresenceReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_trusted_current_marker_is_adopted_without_get(self) -> None:
        cases = (
            ("challenge", BattlePresence.ACTIVE),
            ("nextFloor", BattlePresence.ACTIVE),
            ("active", BattlePresence.ACTIVE),
            ("completion", BattlePresence.COMPLETION),
        )

        for marker_name, expected in cases:
            with self.subTest(marker=marker_name):
                session, browser, page = _session_with_launcher()
                page.observation_results = [
                    _observation(
                        "https://hentaiverse.org/?s=Battle&ss=rb",
                        **{marker_name: True},
                    )
                ]

                reconciled = await session.reconcile_startup_battle_presence(
                    expected_realm=Realm.PERSISTENT
                )

                self.assertIs(reconciled, expected)
                self.assertEqual(browser.get_calls, [])
                self.assertIs(
                    session.battle_completion_observed,
                    marker_name == "completion",
                )

    async def test_current_marker_requires_trusted_expected_identity(self) -> None:
        cases = (
            (
                "origin",
                "https://example.invalid/?s=Battle&ss=rb",
                "untrusted origin",
            ),
            (
                "realm",
                "https://hentaiverse.org/isekai/?s=Battle&ss=rb",
                "wrong realm",
            ),
            (
                "path",
                "https://hentaiverse.org/unexpected?s=Battle&ss=rb",
                "unexpected path",
            ),
        )

        for case_name, url, message in cases:
            with self.subTest(case=case_name):
                session, browser, page = _session_with_launcher()
                page.observation_results = [_observation(url, active=True)]

                with self.assertRaisesRegex(RuntimeError, message):
                    await session.reconcile_startup_battle_presence(
                        expected_realm=Realm.PERSISTENT
                    )

                self.assertEqual(browser.get_calls, [])

    async def test_marker_free_current_identity_may_be_reconciled_by_get(self) -> None:
        cases = (
            "https://example.invalid/",
            "https://hentaiverse.org/isekai/",
            "https://hentaiverse.org/unexpected",
        )

        for current_url in cases:
            with self.subTest(current_url=current_url):
                session, browser, page = _session_with_launcher()
                page.observation_results = [_observation(current_url)]

                reconciled = await session.reconcile_startup_battle_presence(
                    expected_realm=Realm.PERSISTENT
                )

                self.assertIs(reconciled, BattlePresence.ABSENT)
                self.assertEqual(
                    browser.get_calls,
                    ["https://hentaiverse.org/?s=Battle&ss=ar"],
                )
                self.assertEqual(page.route_observation_calls, 1)

    async def test_trusted_post_get_redirect_marker_is_adopted(self) -> None:
        cases = (
            ("challenge", BattlePresence.ACTIVE),
            ("nextFloor", BattlePresence.ACTIVE),
            ("active", BattlePresence.ACTIVE),
            ("completion", BattlePresence.COMPLETION),
        )

        for marker_name, expected in cases:
            with self.subTest(marker=marker_name):
                session, browser, page = _session_with_launcher(
                    current_realm=Realm.ISEKAI
                )
                encounter_secret = f"isekai-secret-{marker_name}-must-not-leak"
                page.observation_results = [
                    _observation("https://hentaiverse.org/isekai/"),
                ]
                page.route_observation_results = [
                    _route_observation(
                        "https://hentaiverse.org/isekai/"
                        f"?s=Battle&ss=ba&encounter={encounter_secret}",
                        route_ready=False,
                        **{marker_name: True},
                    ),
                ]

                with (
                    patch.object(battle_launcher_module.logger, "debug") as debug,
                    patch.object(session_module.logger, "debug") as session_debug,
                    patch.object(session_module.logger, "info") as session_info,
                ):
                    reconciled = await session.reconcile_startup_battle_presence(
                        expected_realm=Realm.ISEKAI
                    )

                self.assertIs(reconciled, expected)
                visible_output = (
                    f"{debug.call_args_list!r} {session_debug.call_args_list!r} "
                    f"{session_info.call_args_list!r}"
                )
                self.assertNotIn(encounter_secret, visible_output)
                self.assertEqual(
                    browser.get_calls,
                    ["https://hentaiverse.org/isekai/?s=Battle&ss=ar"],
                )
                self.assertEqual(browser.diagnostic_calls, [])

    async def test_post_get_startup_marker_requires_trusted_route(self) -> None:
        cases = (
            (
                "origin",
                "https://example.invalid/?s=Battle&ss=ar",
                "untrusted-origin",
            ),
            (
                "realm",
                "https://hentaiverse.org/isekai/?s=Battle&ss=ar",
                "wrong-realm",
            ),
            (
                "path",
                "https://hentaiverse.org/unexpected?s=Battle&ss=ar",
                "wrong-path",
            ),
        )

        for case_name, landed_url, expected_state in cases:
            with self.subTest(case=case_name):
                session, browser, page = _session_with_launcher()
                page.observation_results = [_observation("https://hentaiverse.org/")]
                browser.direct_destination = landed_url
                browser.diagnostic_path = Path("/tmp/battle_route_not_ready.html")
                page.markers = _markers(completion=True)

                with (
                    patch.object(
                        battle_launcher_module,
                        "_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS",
                        0.01,
                    ),
                    patch.object(
                        battle_launcher_module,
                        "_BATTLE_ROUTE_READINESS_POLL_SECONDS",
                        0.001,
                    ),
                    self.assertRaises(BattleRouteReadinessError) as raised,
                ):
                    await session.reconcile_startup_battle_presence(
                        expected_realm=Realm.PERSISTENT
                    )

                self.assertEqual(raised.exception.last_state, expected_state)
                self.assertFalse(session.battle_completion_observed)
                self.assertEqual(
                    browser.get_calls,
                    ["https://hentaiverse.org/?s=Battle&ss=ar"],
                )
                self.assertGreater(page.route_observation_calls, 1)
                self.assertEqual(
                    browser.diagnostic_calls,
                    ["battle_route_not_ready"],
                )

    async def test_confirmed_absence_logs_canonical_source_at_debug(self) -> None:
        session, browser, page = _session_with_launcher()

        with (
            patch.object(session_module.logger, "debug") as debug,
            patch.object(session_module.logger, "info") as info,
        ):
            reconciled = await session.reconcile_startup_battle_presence(
                expected_realm=Realm.PERSISTENT
            )

        self.assertIs(reconciled, BattlePresence.ABSENT)
        self.assertEqual(
            browser.get_calls,
            ["https://hentaiverse.org/?s=Battle&ss=ar"],
        )
        debug.assert_called_once_with(
            "Startup battle presence reconciled "
            "source=%s blocker=%s expected_realm=%s presence=%s",
            "canonical-battle-get",
            "none",
            "persistent",
            "absent",
        )
        info.assert_not_called()
        self.assertEqual(page.route_observation_calls, 1)

    async def test_adopted_marker_logs_atomic_source_and_blocker_at_info(self) -> None:
        session, browser, page = _session_with_launcher()
        page.observation_results = [
            _observation(
                "https://hentaiverse.org/?s=Battle&ss=rb",
                active=True,
            )
        ]

        with (
            patch.object(session_module.logger, "debug") as debug,
            patch.object(session_module.logger, "info") as info,
        ):
            reconciled = await session.reconcile_startup_battle_presence(
                expected_realm=Realm.PERSISTENT
            )

        self.assertIs(reconciled, BattlePresence.ACTIVE)
        self.assertEqual(browser.get_calls, [])
        debug.assert_not_called()
        info.assert_called_once_with(
            "Startup battle presence reconciled "
            "source=%s blocker=%s expected_realm=%s presence=%s",
            "current-document",
            "active",
            "persistent",
            "active",
        )

    async def test_startup_generation_errors_propagate_exactly(self) -> None:
        async def run_case(case_name: str) -> None:
            session, browser, page = _session_with_launcher()
            timeout = ZendriverOperationTimeout(timeout_seconds=5.0)
            if case_name == "current-observation":
                page.observation_results = [timeout]
            elif case_name == "get":
                browser.get_error = timeout
            elif case_name == "post-observation":
                page.observation_results = [_observation("https://hentaiverse.org/")]
                page.route_observation_results = [timeout]
            elif case_name == "route-structure":
                page.route_ready["ar"] = timeout
            else:
                raise AssertionError(case_name)

            with self.assertRaises(ZendriverOperationTimeout) as raised:
                await session.reconcile_startup_battle_presence(
                    expected_realm=Realm.PERSISTENT
                )

            self.assertIs(raised.exception, timeout)
            self.assertLessEqual(len(browser.get_calls), 1)
            self.assertEqual(browser.diagnostic_calls, [])

        for case_name in (
            "current-observation",
            "get",
            "post-observation",
            "route-structure",
        ):
            with self.subTest(case=case_name):
                await run_case(case_name)


if __name__ == "__main__":
    unittest.main()

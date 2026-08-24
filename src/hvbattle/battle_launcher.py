"""Explicit navigation and submission for application-selected battles."""

import asyncio
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from hvbrowser import (
    HENTAIVERSE_ROOT_URL,
    HVDriver,
    MaintenanceNavigationBlockedError,
    MaintenanceNavigationBlocker,
    MaintenanceNavigationObservation,
    Realm,
    RealmDetectionError,
    RealmNavigator,
    observe_maintenance_navigation,
    realm_from_url,
)
from hvbrowser.runtime import (
    is_browser_generation_error,
    setup_logger,
    wait_for_zendriver,
)

from ._failure_safety import contains_log_persistence_error
from ._page_lifecycle import MainFrameDOMContentLoadedWaiter
from ._timing import PROTOCOL_TIMEOUT_SECONDS, SemanticDeadline, protocol_timeout
from .contracts import (
    ArenaOption,
    GrindfestOption,
    RingOfBloodChallenge,
    RingOfBloodOption,
    RingOfBloodSnapshot,
    RingOfBloodStartOutcome,
)

logger = setup_logger(__name__)

_ARENA_ACTION_PATTERN = re.compile(
    r"init_battle\(\s*(\d+)\s*,\s*(\d+)\s*" r"(?:,\s*['\"]([^'\"]*)['\"]\s*)?\)\s*;?"
)
_RING_OF_BLOOD_ACTION_PATTERN = re.compile(
    r"init_battle\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*;?"
)
_EXP_MULTIPLIER_PATTERN = re.compile(
    r"[x\N{MULTIPLICATION SIGN}]\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RING_OF_BLOOD_BALANCE_PATTERN = re.compile(
    r"\b(\d[\d,]*)\s+tokens?\s+of\s+blood\b",
    re.IGNORECASE,
)
_TOKEN_COST_PATTERN = re.compile(
    r"\b(\d[\d,]*)\s+tokens?\b",
    re.IGNORECASE,
)
_RING_OF_BLOOD_ATOMIC_RESULTS = frozenset(
    {
        "submitted",
        "insufficient-tokens",
        "state-changed",
        "option-unavailable",
        "unexpected-page",
        "invalid-state",
        "missing-table",
        "missing-initid",
        "missing-initform",
        "missing-exact-action",
    }
)
_BATTLE_ROUTE_LABELS = {
    "ar": "The Arena",
    "rb": "Ring of Blood",
    "gr": "GrindFest",
}
_BATTLE_ROUTE_READY_EXPRESSIONS = {
    "ar": "Boolean(document.getElementById('arena_list'))",
    "rb": (
        "Boolean(document.getElementById('arena_list') "
        "&& document.getElementById('arena_tokens'))"
    ),
    "gr": "Boolean(document.getElementById('grindfest'))",
}
_NAVIGATION_READ_TIMEOUT_SECONDS = 5.0
_NAVIGATION_MUTATION_TIMEOUT_SECONDS = PROTOCOL_TIMEOUT_SECONDS
_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS = 10.0
_BATTLE_FORM_RECEIPT_DEADLINE_SECONDS = 15.0
_BATTLE_FORM_PRE_SUBMIT_DEADLINE_SECONDS = PROTOCOL_TIMEOUT_SECONDS
_BATTLE_ROUTE_READINESS_POLL_SECONDS = 0.2


class BattleRouteReadinessError(RuntimeError):
    """A canonical Battle route stayed unknown until its readiness deadline."""

    def __init__(
        self,
        *,
        route: str,
        expected_realm: Realm,
        deadline_seconds: float,
        observation_count: int,
        last_state: str,
    ) -> None:
        self.route = route
        self.expected_realm = expected_realm
        self.deadline_seconds = deadline_seconds
        self.observation_count = observation_count
        self.last_state = last_state

        super().__init__(
            f"{_BATTLE_ROUTE_LABELS[route]} did not become ready within "
            f"{deadline_seconds:g}s; last_state={last_state}"
        )


class BattleNavigationSafetyError(RuntimeError):
    """Battle state, origin, path, or realm could not be trusted."""


class BattleFormOutcomeUnknownError(RuntimeError):
    """A submitted form lacked a trusted battle-state receipt."""


class _BattleRouteReadinessState(StrEnum):
    """Sanitized result of one atomic post-GET route observation."""

    UNTRUSTED_ORIGIN = "untrusted-origin"
    WRONG_REALM = "wrong-realm"
    INVALID_URL = "invalid-url"
    WRONG_PATH = "wrong-path"
    WRONG_QUERY = "wrong-query"
    BLOCKED = "blocked"
    READY = "ready"
    ROUTE_DOM_MISSING = "route-dom-missing"
    INVALID_OBSERVATION = "invalid-observation"


class _StartupBattleRouteSource(StrEnum):
    """Where authoritative startup battle evidence was observed."""

    CURRENT_DOCUMENT = "current-document"
    CANONICAL_BATTLE_GET = "canonical-battle-get"


@dataclass(frozen=True, slots=True)
class _StartupBattleRouteResult:
    """Trusted startup route reconciliation returned to BattleSession."""

    source: _StartupBattleRouteSource
    blocker: MaintenanceNavigationBlocker | None


@dataclass(frozen=True, slots=True)
class _BattleRouteReadinessObservation:
    """One atomic route identity, blocker, and route-DOM observation."""

    url: str
    realm: Realm | None
    blocker: MaintenanceNavigationBlocker | None
    route_ready: bool


def _battle_route_readiness_script(route: str) -> str:
    route_ready_expression = _BATTLE_ROUTE_READY_EXPRESSIONS[route]
    return f"""
    (() => {{
        const completion = document.getElementById("pane_completion");
        return {{
            url: window.location.href,
            challenge: Boolean(document.getElementById("riddlesubmit")),
            completion: Boolean(
                completion
                && completion.querySelector('img[src*="finishbattle.png"]')
            ),
            nextFloor: Boolean(document.getElementById("btcp")),
            active: Boolean(document.getElementById("battle_main")),
            routeReady: {route_ready_expression},
        }};
    }})()
    """


def _parse_battle_route_readiness_observation(
    raw: object,
) -> _BattleRouteReadinessObservation:
    if not isinstance(raw, dict):
        raise RuntimeError("Invalid Battle route readiness observation payload")
    payload = cast(dict[object, object], raw)
    url = payload.get("url")
    if not isinstance(url, str):
        raise RuntimeError("Invalid Battle route readiness observation payload")
    marker_names = ("challenge", "completion", "nextFloor", "active")
    if any(type(payload.get(name)) is not bool for name in marker_names):
        raise RuntimeError("Invalid Battle route readiness observation payload")
    route_ready = payload.get("routeReady")
    if type(route_ready) is not bool:
        raise RuntimeError("Invalid Battle route readiness observation payload")

    blocker: MaintenanceNavigationBlocker | None = None
    if payload["challenge"]:
        blocker = MaintenanceNavigationBlocker.CHALLENGE
    elif payload["completion"]:
        blocker = MaintenanceNavigationBlocker.COMPLETION
    elif payload["nextFloor"]:
        blocker = MaintenanceNavigationBlocker.NEXT_FLOOR
    elif payload["active"]:
        blocker = MaintenanceNavigationBlocker.ACTIVE

    try:
        realm = realm_from_url(url)
    except RealmDetectionError:
        realm = None
    return _BattleRouteReadinessObservation(
        url=url,
        realm=realm,
        blocker=blocker,
        route_ready=route_ready,
    )


def _parse_optional_exp_multiplier(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    match = _EXP_MULTIPLIER_PATTERN.fullmatch(value.strip())
    return float(match.group(1)) if match is not None else None


def _parse_ring_integer(pattern: re.Pattern[str], value: Any, *, field: str) -> int:
    if not isinstance(value, str):
        raise RuntimeError(f"Ring of Blood page did not expose {field}")
    match = pattern.search(value)
    if match is None:
        raise RuntimeError(f"Unable to parse Ring of Blood {field}")
    return int(match.group(1).replace(",", ""))


def _parse_optional_ring_integer(
    pattern: re.Pattern[str],
    value: Any,
) -> int | None:
    if not isinstance(value, str):
        return None
    match = pattern.search(value)
    return int(match.group(1).replace(",", "")) if match is not None else None


def _ring_inspection_error(
    reason_code: str,
    message: str,
    *,
    row_index: int | None = None,
) -> RuntimeError:
    logger.warning(
        "Ring of Blood inspection rejected reason=%s row_index=%s",
        reason_code,
        row_index,
    )
    return RuntimeError(message)


class BattleLauncher:
    """List and submit battle choices without owning selection policy."""

    def __init__(self, browser: HVDriver, realm: RealmNavigator) -> None:
        self.browser = browser
        self.realm = realm

    @property
    def page(self) -> Any:
        return self.browser.page

    def _main_document_lifecycle(self) -> MainFrameDOMContentLoadedWaiter:
        return MainFrameDOMContentLoadedWaiter(self.page)

    async def _submit_battle_form(
        self,
        script: str,
        *,
        kind: str,
        battle_id: int,
        route: str,
        expected_realm: Realm,
    ) -> object:
        """Submit once and prove its battle receipt within one deadline."""

        deadline = SemanticDeadline.after(_BATTLE_FORM_RECEIPT_DEADLINE_SECONDS)
        pre_submit_deadline = deadline.capped(_BATTLE_FORM_PRE_SUBMIT_DEADLINE_SECONDS)
        lifecycle = self._main_document_lifecycle()
        mutation_started = False
        acknowledgement_error: Exception | None = None
        try:
            await lifecycle.enable(deadline=pre_submit_deadline)
            operation_timeout = protocol_timeout(
                min(
                    _NAVIGATION_MUTATION_TIMEOUT_SECONDS,
                    pre_submit_deadline.remaining(),
                )
            )
            lifecycle.trigger()
            mutation_started = True
            try:
                result = await wait_for_zendriver(
                    self.page.evaluate(script),
                    timeout=operation_timeout,
                    owner=self.page,
                )
            except Exception as error:
                if is_browser_generation_error(error):
                    raise
                acknowledgement_error = error
                result = None

            if acknowledgement_error is None and pre_submit_deadline.remaining() <= 0:
                acknowledgement_error = TimeoutError(
                    "Battle form submission acknowledgement arrived after its "
                    "pre-submit deadline"
                )

            if result == "submitted" or acknowledgement_error is not None:
                try:
                    await lifecycle.wait(deadline)
                    blocker = await self._confirm_battle_form_receipt(
                        route=route,
                        expected_realm=expected_realm,
                        deadline=deadline,
                    )
                except Exception as receipt_error:
                    if acknowledgement_error is not None:
                        raise acknowledgement_error from receipt_error
                    raise
                if acknowledgement_error is not None:
                    logger.warning(
                        "Battle form submission reconciled after acknowledgement "
                        "error kind=%s id=%s route=%s blocker=%s error_type=%s",
                        kind,
                        battle_id,
                        route,
                        blocker.value,
                        type(acknowledgement_error).__name__,
                    )
                deadline.require_remaining(
                    "Battle form submission receipt was accepted after its deadline"
                )
                return "submitted"
            pre_submit_deadline.require_remaining(
                "Battle form submission result arrived after its pre-submit deadline"
            )
            return result
        except Exception as error:
            if mutation_started:
                logger.error(
                    "Battle form submission outcome is unknown "
                    f"kind={kind} id=%s error_type=%s",
                    battle_id,
                    type(error).__name__,
                )
            raise
        finally:
            lifecycle.close()

    async def _confirm_battle_form_receipt(
        self,
        *,
        route: str,
        expected_realm: Realm,
        deadline: SemanticDeadline,
    ) -> MaintenanceNavigationBlocker:
        """Read one trusted post-navigation battle marker without a new deadline."""

        remaining = deadline.require_remaining(
            "Battle form receipt deadline expired before marker observation"
        )
        try:
            observation = await self._observe_battle_route_readiness(
                route,
                timeout_seconds=min(_NAVIGATION_READ_TIMEOUT_SECONDS, remaining),
            )
        except Exception as error:
            if is_browser_generation_error(error):
                raise
            raise BattleFormOutcomeUnknownError(
                "Battle form receipt observation was invalid"
            ) from error
        try:
            deadline.require_remaining(
                "Battle form receipt deadline expired during marker observation"
            )
        except TimeoutError as error:
            raise BattleFormOutcomeUnknownError(
                "Battle form receipt marker arrived after its deadline"
            ) from error
        state = self._classify_battle_route_readiness(
            observation,
            expected_realm,
            route,
        )
        if (
            state is not _BattleRouteReadinessState.BLOCKED
            or observation.blocker is None
        ):
            raise BattleFormOutcomeUnknownError(
                "Battle form receipt did not expose a trusted battle marker; "
                f"route={route}; state={state.value}"
            )
        try:
            deadline.require_remaining(
                "Battle form receipt classification completed after its deadline"
            )
        except TimeoutError as error:
            raise BattleFormOutcomeUnknownError(
                "Battle form receipt classification completed after its deadline"
            ) from error
        logger.debug(
            "Battle form receipt confirmed route=%s expected_realm=%s blocker=%s",
            route,
            expected_realm.value,
            observation.blocker.value,
        )
        return observation.blocker

    async def confirm_current_battle_receipt(
        self,
        *,
        expected_realm: Realm,
        timeout: float,
    ) -> MaintenanceNavigationBlocker:
        """Atomically verify the current URL identity and a positive battle marker.

        This is used after a caller-owned navigation deadline.  ``timeout`` is
        only the remaining budget for one current-document observation and may
        never exceed the protocol-command watchdog.
        """

        self._validate_expected_realm(expected_realm)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not 0 < float(timeout) <= PROTOCOL_TIMEOUT_SECONDS
        ):
            raise ValueError("battle receipt timeout must be in (0, 5] seconds")
        deadline = SemanticDeadline.after(float(timeout))
        return await self._confirm_battle_form_receipt(
            route="ar",
            expected_realm=expected_realm,
            deadline=deadline,
        )

    async def _goto_battle_route(
        self,
        route: str,
        *,
        expected_realm: Realm,
    ) -> bool:
        self._validate_expected_realm(expected_realm)
        deadline = SemanticDeadline.after(_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS)
        current = await self._observe_navigation(
            "before direct Battle navigation",
            deadline=deadline,
        )
        self._validate_observation_identity(
            current,
            expected_realm,
            "before direct Battle navigation",
        )
        self._raise_if_blocked(current)

        await self._get_battle_route(expected_realm, route, deadline=deadline)
        landed = await self._wait_for_battle_route_readiness(
            expected_realm=expected_realm,
            route=route,
            deadline=deadline,
        )
        if landed.blocker is not None:
            raise MaintenanceNavigationBlockedError(landed.blocker)
        deadline.require_remaining(
            "Battle route readiness was accepted after its deadline"
        )
        return True

    async def _get_battle_route(
        self,
        expected_realm: Realm,
        route: str,
        *,
        deadline: SemanticDeadline,
    ) -> None:
        direct_url = self._battle_route_url(expected_realm, route)
        lifecycle = MainFrameDOMContentLoadedWaiter(self.page)
        try:
            await lifecycle.enable(deadline=deadline)
            lifecycle.trigger()
            await self.browser.navigate_with_budget(
                direct_url,
                budget_seconds=deadline.require_remaining(
                    "Battle route deadline expired before browser navigation"
                ),
            )
            await lifecycle.wait(deadline)
            deadline.require_remaining(
                "Battle route navigation deadline expired at lifecycle receipt"
            )
        except Exception as error:
            if contains_log_persistence_error(error) or is_browser_generation_error(
                error
            ):
                raise
            raise BattleNavigationSafetyError(
                f"The direct {_BATTLE_ROUTE_LABELS[route]} navigation outcome is "
                "unknown"
            ) from error
        finally:
            lifecycle.close()

    async def _observe_navigation(
        self,
        context: str,
        *,
        deadline: SemanticDeadline,
    ) -> MaintenanceNavigationObservation:
        try:
            async with asyncio.timeout(deadline.protocol_timeout()):
                observation = await observe_maintenance_navigation(self.page)
            deadline.require_remaining(f"Battle navigation deadline expired {context}")
            return observation
        except Exception as error:
            if contains_log_persistence_error(error) or is_browser_generation_error(
                error
            ):
                raise
            raise BattleNavigationSafetyError(
                f"Unable to observe trusted Battle navigation state {context}"
            ) from error

    @staticmethod
    def _validate_expected_realm(expected_realm: Realm) -> None:
        if not isinstance(expected_realm, Realm):
            raise TypeError("expected_realm must be a Realm")

    @staticmethod
    def _validate_observation_identity(
        observation: MaintenanceNavigationObservation,
        expected_realm: Realm,
        context: str,
    ) -> None:
        if observation.realm is None:
            raise BattleNavigationSafetyError(
                f"Battle navigation has an untrusted origin {context}"
            )
        if observation.realm is not expected_realm:
            raise BattleNavigationSafetyError(
                f"Battle navigation is in the wrong realm {context}"
            )
        try:
            path = urlsplit(observation.url).path
        except ValueError as error:
            raise BattleNavigationSafetyError(
                f"Battle navigation URL is invalid {context}"
            ) from error
        expected_path = "/isekai/" if expected_realm is Realm.ISEKAI else "/"
        if path != expected_path:
            raise BattleNavigationSafetyError(
                f"Battle navigation landed on an unexpected path {context}"
            )

    @staticmethod
    def _raise_if_blocked(observation: MaintenanceNavigationObservation) -> None:
        if observation.blocker is not None:
            raise MaintenanceNavigationBlockedError(observation.blocker)

    @staticmethod
    def _battle_route_url(realm: Realm, route: str) -> str:
        path_prefix = "/isekai" if realm is Realm.ISEKAI else ""
        return f"{HENTAIVERSE_ROOT_URL}{path_prefix}/?s=Battle&ss={route}"

    @staticmethod
    def _classify_battle_route_readiness(
        observation: _BattleRouteReadinessObservation,
        expected_realm: Realm,
        route: str,
    ) -> _BattleRouteReadinessState:
        if observation.realm is None:
            return _BattleRouteReadinessState.UNTRUSTED_ORIGIN
        if observation.realm is not expected_realm:
            return _BattleRouteReadinessState.WRONG_REALM
        try:
            parsed_url = urlsplit(observation.url)
        except ValueError:
            return _BattleRouteReadinessState.INVALID_URL
        expected_path = "/isekai/" if expected_realm is Realm.ISEKAI else "/"
        if parsed_url.path != expected_path:
            return _BattleRouteReadinessState.WRONG_PATH
        if observation.blocker is not None:
            return _BattleRouteReadinessState.BLOCKED
        try:
            query = parse_qs(parsed_url.query, keep_blank_values=True)
        except ValueError:
            return _BattleRouteReadinessState.INVALID_URL
        expected_query = {
            "s": ["Battle"],
            "ss": [route],
        }
        if query != expected_query:
            return _BattleRouteReadinessState.WRONG_QUERY
        if observation.route_ready:
            return _BattleRouteReadinessState.READY
        return _BattleRouteReadinessState.ROUTE_DOM_MISSING

    async def _observe_battle_route_readiness(
        self,
        route: str,
        *,
        timeout_seconds: float,
    ) -> _BattleRouteReadinessObservation:
        raw = await wait_for_zendriver(
            self.page.evaluate(_battle_route_readiness_script(route)),
            timeout=protocol_timeout(timeout_seconds),
            owner=self.page,
        )
        return _parse_battle_route_readiness_observation(raw)

    async def _wait_for_battle_route_readiness(
        self,
        *,
        expected_realm: Realm,
        route: str,
        deadline: SemanticDeadline,
    ) -> _BattleRouteReadinessObservation:
        observation_count = 0
        last_state = _BattleRouteReadinessState.INVALID_OBSERVATION
        last_error: Exception | None = None

        while True:
            remaining = deadline.remaining()
            if remaining <= 0:
                break
            try:
                observation = await self._observe_battle_route_readiness(
                    route,
                    timeout_seconds=min(
                        _NAVIGATION_READ_TIMEOUT_SECONDS,
                        remaining,
                    ),
                )
            except Exception as observation_error:
                if contains_log_persistence_error(
                    observation_error
                ) or is_browser_generation_error(observation_error):
                    raise
                last_error = observation_error
                last_state = _BattleRouteReadinessState.INVALID_OBSERVATION
            else:
                observation_count += 1
                if deadline.remaining() <= 0:
                    break
                last_error = None
                last_state = self._classify_battle_route_readiness(
                    observation,
                    expected_realm,
                    route,
                )
                if deadline.remaining() <= 0:
                    break
                if last_state in {
                    _BattleRouteReadinessState.BLOCKED,
                    _BattleRouteReadinessState.READY,
                }:
                    logger.debug(
                        "Battle route readiness resolved "
                        "route=%s expected_realm=%s state=%s observations=%d",
                        route,
                        expected_realm.value,
                        last_state.value,
                        observation_count,
                    )
                    return observation

            remaining = deadline.remaining()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_BATTLE_ROUTE_READINESS_POLL_SECONDS, remaining))

        logger.warning(
            "Battle route readiness deadline exhausted "
            "route=%s expected_realm=%s state=%s observations=%d",
            route,
            expected_realm.value,
            last_state.value,
            observation_count,
        )
        readiness_error = BattleRouteReadinessError(
            route=route,
            expected_realm=expected_realm,
            deadline_seconds=_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS,
            observation_count=observation_count,
            last_state=last_state.value,
        )
        if last_error is not None:
            raise readiness_error from last_error
        raise readiness_error

    async def goto_arena(self, *, expected_realm: Realm) -> bool:
        return await self._goto_battle_route("ar", expected_realm=expected_realm)

    async def goto_ring_of_blood(self, *, expected_realm: Realm) -> bool:
        return await self._goto_battle_route("rb", expected_realm=expected_realm)

    async def goto_grindfest(self, *, expected_realm: Realm) -> bool:
        return await self._goto_battle_route("gr", expected_realm=expected_realm)

    async def reconcile_startup_battle_route(
        self,
        *,
        expected_realm: Realm,
    ) -> _StartupBattleRouteResult:
        """Reconcile startup markers against one explicit realm trust boundary."""

        self._validate_expected_realm(expected_realm)
        deadline = SemanticDeadline.after(_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS)
        current = await self._observe_navigation(
            "in the current startup document",
            deadline=deadline,
        )
        if current.blocker is not None:
            self._validate_observation_identity(
                current,
                expected_realm,
                "in the current startup document",
            )
            return _StartupBattleRouteResult(
                _StartupBattleRouteSource.CURRENT_DOCUMENT,
                current.blocker,
            )

        current_realm = "untrusted" if current.realm is None else current.realm.value
        logger.debug(
            "Startup battle presence is provisional "
            "source=current-document blocker=none expected_realm=%s "
            "current_realm=%s",
            expected_realm.value,
            current_realm,
        )

        await self._get_battle_route(expected_realm, "ar", deadline=deadline)
        landed = await self._wait_for_battle_route_readiness(
            expected_realm=expected_realm,
            route="ar",
            deadline=deadline,
        )
        if landed.blocker is not None:
            return _StartupBattleRouteResult(
                _StartupBattleRouteSource.CANONICAL_BATTLE_GET,
                landed.blocker,
            )
        return _StartupBattleRouteResult(
            _StartupBattleRouteSource.CANONICAL_BATTLE_GET,
            None,
        )

    async def list_arena_options(self) -> tuple[ArenaOption, ...]:
        """Return Arena choices without selecting one for the client."""
        row_payloads = await wait_for_zendriver(
            self.page.evaluate(r"""
            (() => {
                const table = document.getElementById('arena_list');
                if (!table) return [];
                const rows = Array.from(table.querySelectorAll('tr'));
                const normalize = (value) =>
                    (value || '').replace(/\s+/g, ' ').trim();
                const headers = Array.from(
                    rows[0]?.querySelectorAll('th') || []
                ).map((cell) => normalize(cell.textContent).toLowerCase());
                const challengeIndex = headers.indexOf('challenge');
                const expIndex = headers.indexOf('exp mod');
                return rows.slice(1).flatMap((row) => {
                    const action = row.querySelector(
                        'img[onclick*="init_battle"]'
                    );
                    if (!action) return [];
                    const cells = Array.from(row.children).filter(
                        (cell) => cell.tagName === 'TD'
                    );
                    return [{
                        onclick: action.getAttribute('onclick') || '',
                        challengeName: challengeIndex >= 0
                            ? normalize(cells[challengeIndex]?.textContent)
                            : '',
                        expText: expIndex >= 0
                            ? normalize(cells[expIndex]?.textContent)
                            : '',
                    }];
                });
            })()
            """),
            timeout=_NAVIGATION_READ_TIMEOUT_SECONDS,
            owner=self.page,
        )
        options: list[ArenaOption] = []
        for payload in row_payloads or ():
            if isinstance(payload, str):
                onclick = payload
                challenge_name = None
                exp_multiplier = None
            elif isinstance(payload, dict):
                onclick_value = payload.get("onclick")
                onclick = onclick_value if isinstance(onclick_value, str) else ""
                challenge_value = payload.get("challengeName")
                challenge_name = (
                    challenge_value.strip()
                    if isinstance(challenge_value, str) and challenge_value.strip()
                    else None
                )
                exp_multiplier = _parse_optional_exp_multiplier(payload.get("expText"))
            else:
                onclick = ""
                challenge_name = None
                exp_multiplier = None
            match = _ARENA_ACTION_PATTERN.fullmatch(onclick.strip())
            if match is None:
                logger.debug(
                    "Arena action did not match expected shape: length=%d",
                    len(onclick),
                )
                continue
            options.append(
                ArenaOption(
                    battle_id=int(match.group(1)),
                    token=match.group(3),
                    challenge_name=challenge_name,
                    exp_multiplier=exp_multiplier,
                )
            )
        return tuple(options)

    async def inspect_ring_of_blood(self) -> RingOfBloodSnapshot:
        """Inspect every listed Ring challenge and tokens without selecting one."""
        payload = await wait_for_zendriver(
            self.page.evaluate(r"""
            (() => {
                const table = document.getElementById('arena_list');
                const tokenContainer = document.getElementById('arena_tokens');
                if (!table || !tokenContainer) return null;
                const rows = Array.from(table.querySelectorAll('tr'));
                const normalize = (value) =>
                    (value || '').replace(/\s+/g, ' ').trim();
                const headers = Array.from(
                    rows[0]?.querySelectorAll('th') || []
                ).map((cell) => normalize(cell.textContent).toLowerCase());
                const challengeIndex = headers.indexOf('challenge');
                const expIndex = headers.indexOf('exp mod');
                const entryCostIndex = headers.indexOf('entry cost');
                if (
                    challengeIndex < 0
                    || expIndex < 0
                    || entryCostIndex < 0
                ) {
                    return {tokenText: normalize(tokenContainer.textContent), rows: null};
                }
                return {
                    tokenText: normalize(tokenContainer.textContent),
                    rows: rows.slice(1).flatMap((row) => {
                        const action = row.querySelector(
                            'img[onclick*="init_battle"]'
                        );
                        const cells = Array.from(row.children).filter(
                            (cell) => cell.tagName === 'TD'
                        );
                        if (!cells[challengeIndex]) return [];
                        return [{
                            onclick: action
                                ? (action.getAttribute('onclick') || '')
                                : null,
                            challengeName: normalize(
                                cells[challengeIndex]?.textContent
                            ),
                            expText: normalize(cells[expIndex]?.textContent),
                            entryCostText: normalize(
                                cells[entryCostIndex]?.textContent
                            ),
                        }];
                    }),
                };
            })()
            """),
            timeout=_NAVIGATION_READ_TIMEOUT_SECONDS,
            owner=self.page,
        )
        if not isinstance(payload, dict):
            raise _ring_inspection_error(
                "ring.structure-invalid",
                "Ring of Blood page did not expose expected structure",
            )
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise _ring_inspection_error(
                "ring.columns-missing",
                "Ring of Blood challenge columns are missing",
            )
        try:
            tokens_of_blood = _parse_ring_integer(
                _RING_OF_BLOOD_BALANCE_PATTERN,
                payload.get("tokenText"),
                field="token balance",
            )
        except RuntimeError:
            raise _ring_inspection_error(
                "ring.token-invalid",
                "Unable to parse Ring of Blood token balance",
            ) from None
        options: list[RingOfBloodOption] = []
        challenges: list[RingOfBloodChallenge] = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise _ring_inspection_error(
                    "ring.row-invalid",
                    "Ring of Blood challenge row is malformed",
                    row_index=row_index,
                )
            challenge_name = row.get("challengeName")
            if not isinstance(challenge_name, str) or not challenge_name.strip():
                raise _ring_inspection_error(
                    "ring.challenge-name-missing",
                    "Ring of Blood challenge name is missing",
                    row_index=row_index,
                )
            onclick = row.get("onclick")
            option: RingOfBloodOption | None = None
            if onclick is None:
                exp_multiplier = _parse_optional_exp_multiplier(row.get("expText"))
                entry_cost = _parse_optional_ring_integer(
                    _TOKEN_COST_PATTERN,
                    row.get("entryCostText"),
                )
            else:
                if not isinstance(onclick, str):
                    raise _ring_inspection_error(
                        "ring.action-invalid",
                        "Ring of Blood challenge action is malformed",
                        row_index=row_index,
                    )
                action_match = _RING_OF_BLOOD_ACTION_PATTERN.fullmatch(onclick.strip())
                if action_match is None:
                    raise _ring_inspection_error(
                        "ring.action-invalid",
                        "Ring of Blood challenge action is malformed",
                        row_index=row_index,
                    )
                exp_multiplier = _parse_optional_exp_multiplier(row.get("expText"))
                if exp_multiplier is None:
                    raise _ring_inspection_error(
                        "ring.exp-invalid",
                        "Unable to parse Ring of Blood EXP multiplier",
                        row_index=row_index,
                    )
                try:
                    entry_cost = _parse_ring_integer(
                        _TOKEN_COST_PATTERN,
                        row.get("entryCostText"),
                        field="entry cost",
                    )
                except RuntimeError:
                    raise _ring_inspection_error(
                        "ring.entry-cost-invalid",
                        "Unable to parse Ring of Blood entry cost",
                        row_index=row_index,
                    ) from None
                action_cost = int(action_match.group(2))
                if action_cost != entry_cost:
                    raise _ring_inspection_error(
                        "ring.entry-cost-mismatch",
                        "Ring of Blood challenge entry cost is inconsistent",
                        row_index=row_index,
                    )
                option = RingOfBloodOption(
                    battle_id=int(action_match.group(1)),
                    challenge_name=challenge_name.strip(),
                    exp_multiplier=exp_multiplier,
                    entry_cost=entry_cost,
                )
                options.append(option)
            challenges.append(
                RingOfBloodChallenge(
                    challenge_name=challenge_name.strip(),
                    exp_multiplier=exp_multiplier,
                    entry_cost=entry_cost,
                    start_action=option,
                )
            )
        logger.debug(
            "Ring of Blood inspection complete tokens=%s challenges=%s "
            "start_actions=%s",
            tokens_of_blood,
            len(challenges),
            len(options),
        )
        for challenge in challenges:
            option = challenge.start_action
            logger.debug(
                "Ring of Blood challenge observed challenge=%r startable=%s "
                "id=%s entry_cost=%s",
                challenge.challenge_name,
                challenge.startable,
                None if option is None else option.battle_id,
                challenge.entry_cost,
            )
        return RingOfBloodSnapshot(
            tokens_of_blood,
            tuple(options),
            tuple(challenges),
        )

    async def start_ring_of_blood(
        self,
        option: RingOfBloodOption,
        *,
        expected_before: RingOfBloodSnapshot,
        expected_realm: Realm,
    ) -> RingOfBloodStartOutcome:
        """Submit one Ring challenge after revalidating its inspected state."""
        if not isinstance(option, RingOfBloodOption):
            raise TypeError("option must be a RingOfBloodOption")
        if not isinstance(expected_before, RingOfBloodSnapshot):
            raise TypeError("expected_before must be a RingOfBloodSnapshot")
        self._validate_expected_realm(expected_realm)
        if option not in expected_before.options:
            return RingOfBloodStartOutcome.STATE_CHANGED

        expected_snapshot = {
            "tokens": expected_before.tokens_of_blood,
            "challenges": [
                {
                    "challengeName": challenge.challenge_name,
                    "expMultiplier": challenge.exp_multiplier,
                    "entryCost": challenge.entry_cost,
                    "actionId": (
                        None
                        if challenge.start_action is None
                        else challenge.start_action.battle_id
                    ),
                }
                for challenge in expected_before.challenges
            ],
        }
        expected_url_js = json.dumps(self._battle_route_url(expected_realm, "rb"))
        expected_snapshot_js = json.dumps(expected_snapshot)
        atomic_result = await self._submit_battle_form(
            rf"""
                (() => {{
                    const expectedUrl = {expected_url_js};
                    if (window.location.href !== expectedUrl) {{
                        return 'unexpected-page';
                    }}
                    const expectedId = {option.battle_id};
                    const expectedCost = {option.entry_cost};
                    const expectedSnapshot = {expected_snapshot_js};
                    const table = document.getElementById('arena_list');
                    const tokenContainer = document.getElementById('arena_tokens');
                    const initid = document.getElementById('initid');
                    const initform = document.getElementById('initform');
                    if (!table) return 'missing-table';
                    if (!tokenContainer) return 'invalid-state';
                    if (!initid) return 'missing-initid';
                    if (!initform) return 'missing-initform';
                    const normalize = (value) =>
                        (value || '').replace(/\s+/g, ' ').trim();
                    const parseInteger = (pattern, value) => {{
                        const match = pattern.exec(normalize(value));
                        return match ? Number(match[1].replaceAll(',', '')) : null;
                    }};
                    const rows = Array.from(table.querySelectorAll('tr'));
                    const headers = Array.from(
                        rows[0]?.querySelectorAll('th') || []
                    ).map((cell) => normalize(cell.textContent).toLowerCase());
                    const challengeIndex = headers.indexOf('challenge');
                    const expIndex = headers.indexOf('exp mod');
                    const entryCostIndex = headers.indexOf('entry cost');
                    if (
                        challengeIndex < 0
                        || expIndex < 0
                        || entryCostIndex < 0
                    ) return 'invalid-state';
                    const tokens = parseInteger(
                        /\b(\d[\d,]*)\s+tokens?\s+of\s+blood\b/i,
                        tokenContainer.textContent,
                    );
                    if (tokens === null) return 'invalid-state';
                    const challenges = [];
                    for (const row of rows.slice(1)) {{
                        const cells = Array.from(row.children).filter(
                            (cell) => cell.tagName === 'TD'
                        );
                        const challengeName = normalize(
                            cells[challengeIndex]?.textContent
                        );
                        if (!challengeName) return 'invalid-state';
                        const expMatch = /^[x×]\s*(\d+(?:\.\d+)?)$/i.exec(
                            normalize(cells[expIndex]?.textContent)
                        );
                        const expMultiplier = expMatch
                            ? Number(expMatch[1])
                            : null;
                        const entryCost = parseInteger(
                            /\b(\d[\d,]*)\s+tokens?\b/i,
                            cells[entryCostIndex]?.textContent,
                        );
                        const action = row.querySelector(
                            'img[onclick*="init_battle"]'
                        );
                        let actionId = null;
                        if (action) {{
                            const match = /^init_battle\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*;?$/.exec(
                                (action.getAttribute('onclick') || '').trim()
                            );
                            if (
                                !match
                                || expMultiplier === null
                                || entryCost === null
                                || Number(match[2]) !== entryCost
                            ) return 'invalid-state';
                            actionId = Number(match[1]);
                        }}
                        challenges.push({{
                            challengeName,
                            expMultiplier,
                            entryCost,
                            actionId,
                        }});
                    }}
                    const currentSnapshot = {{tokens, challenges}};
                    const selected = challenges.find(
                        (challenge) => challenge.actionId === expectedId
                    );
                    if (!selected) return 'option-unavailable';
                    if (JSON.stringify(currentSnapshot) !== JSON.stringify(
                        expectedSnapshot
                    )) return 'state-changed';
                    if (selected.entryCost !== expectedCost) return 'state-changed';
                    if (tokens < expectedCost) return 'insufficient-tokens';
                    initid.value = String(expectedId);
                    initform.submit();
                    return 'submitted';
                }})()
            """,
            kind="ring-of-blood",
            battle_id=option.battle_id,
            route="rb",
            expected_realm=expected_realm,
        )
        result = (
            atomic_result
            if isinstance(atomic_result, str)
            and atomic_result in _RING_OF_BLOOD_ATOMIC_RESULTS
            else "unexpected-result"
        )
        logger.debug(
            "Ring of Blood atomic submission check id=%s result=%s",
            option.battle_id,
            result,
        )
        if result == "insufficient-tokens":
            logger.debug(
                "Ring of Blood tokens are insufficient id=%s required=%s available=%s",
                option.battle_id,
                option.entry_cost,
                expected_before.tokens_of_blood,
            )
            return RingOfBloodStartOutcome.INSUFFICIENT_TOKENS
        if result == "state-changed":
            logger.debug(
                "Ring of Blood state changed before submission id=%s",
                option.battle_id,
            )
            return RingOfBloodStartOutcome.STATE_CHANGED
        if result == "option-unavailable":
            logger.debug(
                "Ring of Blood option is unavailable id=%s",
                option.battle_id,
            )
            return RingOfBloodStartOutcome.OPTION_UNAVAILABLE
        if result == "unexpected-page":
            logger.debug(
                "Ring of Blood pre-submit check id=%s reason=unexpected-page",
                option.battle_id,
            )
            return RingOfBloodStartOutcome.OPTION_UNAVAILABLE
        if result != "submitted":
            logger.warning(
                "Battle form was not submitted kind=ring-of-blood id=%s reason=%s",
                option.battle_id,
                result,
            )
            return RingOfBloodStartOutcome.OPTION_UNAVAILABLE

        logger.debug("Submitted Ring of Blood battle form id=%s", option.battle_id)
        return RingOfBloodStartOutcome.SUBMITTED

    async def start_arena(
        self,
        option: ArenaOption,
        *,
        expected_realm: Realm,
    ) -> bool:
        """Submit one Arena option explicitly selected by the caller."""
        if not isinstance(option, ArenaOption):
            raise TypeError("option must be an ArenaOption")
        self._validate_expected_realm(expected_realm)
        expected_url_js = json.dumps(self._battle_route_url(expected_realm, "ar"))
        token_js = json.dumps(option.token)
        result = await self._submit_battle_form(
            rf"""
                (() => {{
                    const expectedUrl = {expected_url_js};
                    if (window.location.href !== expectedUrl) {{
                        return 'unexpected-page';
                    }}
                    const expectedId = {option.battle_id};
                    const expectedToken = {token_js};
                    const table = document.getElementById('arena_list');
                    const initid = document.getElementById('initid');
                    const initform = document.getElementById('initform');
                    if (!table || !initid || !initform) return 'form-unavailable';
                    const hasExactAction = Array.from(table.querySelectorAll(
                        'img[onclick*="init_battle"]'
                    )).some((element) => {{
                        const match = /^init_battle\(\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*(['"])([^'"]*)\3\s*)?\)\s*;?$/.exec(
                            (element.getAttribute('onclick') || '').trim()
                        );
                        return match !== null
                            && Number(match[1]) === expectedId
                            && (
                                expectedToken === null
                                    ? match[4] === undefined
                                    : match[4] === expectedToken
                            );
                    }});
                    if (!hasExactAction) return 'option-unavailable';
                    initid.value = String(expectedId);
                    if (expectedToken !== null) {{
                        const inittoken = document.getElementById('inittoken');
                        if (!inittoken) return 'form-unavailable';
                        inittoken.value = expectedToken;
                    }}
                    initform.submit();
                    return 'submitted';
                }})()
            """,
            kind="arena",
            battle_id=option.battle_id,
            route="ar",
            expected_realm=expected_realm,
        )
        if result == "unexpected-page":
            logger.debug(
                "Battle form submission skipped kind=arena id=%s "
                "reason=unexpected-page",
                option.battle_id,
            )
            return False
        if result != "submitted":
            logger.warning(
                "Battle form was not submitted kind=arena id=%s reason=%s",
                option.battle_id,
                result if isinstance(result, str) else "unexpected-result",
            )
            return False

        logger.info("Submitted Arena battle form id=%s", option.battle_id)
        return True

    async def list_grindfest_options(self) -> tuple[GrindfestOption, ...]:
        """Return GrindFest choices without selecting one for the client."""
        onclick_list = await wait_for_zendriver(
            self.page.evaluate("""
            (() => {
                const imgs = document.querySelectorAll(
                    '#grindfest img[onclick*="init_battle"]'
                );
                return Array.from(imgs).map(
                    (el) => el.getAttribute('onclick') || ''
                );
            })()
            """),
            timeout=_NAVIGATION_READ_TIMEOUT_SECONDS,
            owner=self.page,
        )
        options: list[GrindfestOption] = []
        for onclick in onclick_list or ():
            match = re.match(r"init_battle\(\s*(\d+)\s*\)", onclick)
            if match is None:
                logger.debug(
                    "GrindFest action did not match expected shape: length=%d",
                    len(onclick),
                )
                continue
            options.append(GrindfestOption(battle_id=int(match.group(1))))
        return tuple(options)

    async def start_grindfest(
        self,
        option: GrindfestOption,
        *,
        expected_realm: Realm,
    ) -> bool:
        """Submit one GrindFest option explicitly selected by the caller."""
        if not isinstance(option, GrindfestOption):
            raise TypeError("option must be a GrindfestOption")
        self._validate_expected_realm(expected_realm)
        expected_url_js = json.dumps(self._battle_route_url(expected_realm, "gr"))
        result = await self._submit_battle_form(
            rf"""
                (() => {{
                    const expectedUrl = {expected_url_js};
                    if (window.location.href !== expectedUrl) {{
                        return 'unexpected-page';
                    }}
                    const expectedId = {option.battle_id};
                    const container = document.getElementById('grindfest');
                    const initid = document.getElementById('initid');
                    const initform = document.getElementById('initform');
                    if (!container || !initid || !initform) {{
                        return 'form-unavailable';
                    }}
                    const hasExactAction = Array.from(container.querySelectorAll(
                        'img[onclick*="init_battle"]'
                    )).some((element) => {{
                        const match = /^init_battle\(\s*(\d+)\s*\)\s*;?$/.exec(
                            (element.getAttribute('onclick') || '').trim()
                        );
                        return match !== null && Number(match[1]) === expectedId;
                    }});
                    if (!hasExactAction) return 'option-unavailable';
                    initid.value = String(expectedId);
                    initform.submit();
                    return 'submitted';
                }})()
            """,
            kind="grindfest",
            battle_id=option.battle_id,
            route="gr",
            expected_realm=expected_realm,
        )
        if result == "unexpected-page":
            logger.debug(
                "Battle form submission skipped kind=grindfest id=%s "
                "reason=unexpected-page",
                option.battle_id,
            )
            return False
        if result != "submitted":
            logger.warning(
                "Battle form was not submitted kind=grindfest id=%s reason=%s",
                option.battle_id,
                result if isinstance(result, str) else "unexpected-result",
            )
            return False

        logger.info("Submitted GrindFest battle form id=%s", option.battle_id)
        return True


__all__ = [
    "BattleFormOutcomeUnknownError",
    "BattleLauncher",
    "BattleNavigationSafetyError",
    "BattleRouteReadinessError",
]

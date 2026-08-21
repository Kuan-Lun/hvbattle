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
    LogPersistenceError,
    is_browser_generation_error,
    setup_logger,
    wait_for_zendriver,
)

from ._failure_safety import contains_log_persistence_error
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
        "unexpected-page",
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
_NAVIGATION_MUTATION_TIMEOUT_SECONDS = 15.0
_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS = 10.0
_BATTLE_ROUTE_READINESS_POLL_SECONDS = 0.2
_BATTLE_ROUTE_DIAGNOSTIC_KIND = "battle_route_not_ready"


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
        diagnostic_path: str | None,
        diagnostic_error_type: str | None,
    ) -> None:
        self.route = route
        self.expected_realm = expected_realm
        self.deadline_seconds = deadline_seconds
        self.observation_count = observation_count
        self.last_state = last_state
        self.diagnostic_path = diagnostic_path
        self.diagnostic_error_type = diagnostic_error_type

        message = (
            f"{_BATTLE_ROUTE_LABELS[route]} did not become ready within "
            f"{deadline_seconds:g}s; last_state={last_state}"
        )
        if diagnostic_path is not None:
            message += f"; diagnostic saved to {diagnostic_path}"
        elif diagnostic_error_type is not None:
            message += f"; diagnostic capture failed ({diagnostic_error_type})"
        super().__init__(message)


class _BattleNavigationSafetyError(RuntimeError):
    """Battle state, origin, path, or realm could not be trusted."""


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

    async def _path_prefix(self) -> str:
        return "/isekai" if await self.realm.current() is Realm.ISEKAI else ""

    async def _goto_battle_route(
        self,
        route: str,
        *,
        expected_realm: Realm,
    ) -> bool:
        self._validate_expected_realm(expected_realm)
        current = await self._observe_navigation("before direct Battle navigation")
        self._validate_observation_identity(
            current,
            expected_realm,
            "before direct Battle navigation",
        )
        self._raise_if_blocked(current)

        await self._get_battle_route(expected_realm, route)
        landed = await self._wait_for_battle_route_readiness(
            expected_realm=expected_realm,
            route=route,
        )
        if landed.blocker is not None:
            raise MaintenanceNavigationBlockedError(landed.blocker)
        return True

    async def _get_battle_route(
        self,
        expected_realm: Realm,
        route: str,
    ) -> None:
        direct_url = self._battle_route_url(expected_realm, route)
        try:
            await self.browser.get(direct_url)
        except Exception as error:
            if contains_log_persistence_error(error) or is_browser_generation_error(
                error
            ):
                raise
            raise _BattleNavigationSafetyError(
                f"The direct {_BATTLE_ROUTE_LABELS[route]} navigation outcome is "
                "unknown"
            ) from error

    async def _observe_navigation(
        self,
        context: str,
    ) -> MaintenanceNavigationObservation:
        try:
            return await observe_maintenance_navigation(self.page)
        except Exception as error:
            if contains_log_persistence_error(error) or is_browser_generation_error(
                error
            ):
                raise
            raise _BattleNavigationSafetyError(
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
            raise _BattleNavigationSafetyError(
                f"Battle navigation has an untrusted origin {context}"
            )
        if observation.realm is not expected_realm:
            raise _BattleNavigationSafetyError(
                f"Battle navigation is in the wrong realm {context}"
            )
        try:
            path = urlsplit(observation.url).path
        except ValueError as error:
            raise _BattleNavigationSafetyError(
                f"Battle navigation URL is invalid {context}"
            ) from error
        expected_path = "/isekai/" if expected_realm is Realm.ISEKAI else "/"
        if path != expected_path:
            raise _BattleNavigationSafetyError(
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
            timeout=timeout_seconds,
            owner=self.page,
        )
        return _parse_battle_route_readiness_observation(raw)

    async def _capture_route_readiness_diagnostic(
        self,
    ) -> tuple[str | None, str | None]:
        try:
            path = await self.browser.save_page_diagnostic(
                _BATTLE_ROUTE_DIAGNOSTIC_KIND
            )
            return (str(path) if path is not None else None), None
        except LogPersistenceError:
            raise
        except Exception as error:
            if contains_log_persistence_error(error) or is_browser_generation_error(
                error
            ):
                raise
            logger.warning(
                "Battle route diagnostic capture failed " "kind=%s error_type=%s",
                _BATTLE_ROUTE_DIAGNOSTIC_KIND,
                type(error).__name__,
            )
            logger.debug(
                "Battle route diagnostic capture traceback",
                exc_info=True,
            )
            return None, type(error).__name__

    async def _wait_for_battle_route_readiness(
        self,
        *,
        expected_realm: Realm,
        route: str,
    ) -> _BattleRouteReadinessObservation:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BATTLE_ROUTE_READINESS_DEADLINE_SECONDS
        observation_count = 0
        last_state = _BattleRouteReadinessState.INVALID_OBSERVATION
        last_error: Exception | None = None

        while True:
            remaining = deadline - loop.time()
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
                last_error = None
                last_state = self._classify_battle_route_readiness(
                    observation,
                    expected_realm,
                    route,
                )
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

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_BATTLE_ROUTE_READINESS_POLL_SECONDS, remaining))

        diagnostic_path, diagnostic_error_type = (
            await self._capture_route_readiness_diagnostic()
        )
        logger.warning(
            "Battle route readiness deadline exhausted "
            "route=%s expected_realm=%s state=%s observations=%d "
            "diagnostic_saved=%s diagnostic_error_type=%s",
            route,
            expected_realm.value,
            last_state.value,
            observation_count,
            diagnostic_path is not None,
            diagnostic_error_type or "none",
        )
        readiness_error = BattleRouteReadinessError(
            route=route,
            expected_realm=expected_realm,
            deadline_seconds=_BATTLE_ROUTE_READINESS_DEADLINE_SECONDS,
            observation_count=observation_count,
            last_state=last_state.value,
            diagnostic_path=diagnostic_path,
            diagnostic_error_type=diagnostic_error_type,
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
        current = await self._observe_navigation("in the current startup document")
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

        await self._get_battle_route(expected_realm, "ar")
        landed = await self._wait_for_battle_route_readiness(
            expected_realm=expected_realm,
            route="ar",
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
    ) -> RingOfBloodStartOutcome:
        """Submit one Ring challenge after revalidating its inspected state."""
        if not isinstance(option, RingOfBloodOption):
            raise TypeError("option must be a RingOfBloodOption")
        if not isinstance(expected_before, RingOfBloodSnapshot):
            raise TypeError("expected_before must be a RingOfBloodSnapshot")

        ring_url = f"{HENTAIVERSE_ROOT_URL}{await self._path_prefix()}/?s=Battle&ss=rb"
        current_url = await wait_for_zendriver(
            self.page.evaluate("window.location.href"),
            timeout=_NAVIGATION_READ_TIMEOUT_SECONDS,
            owner=self.page,
        )
        if current_url != ring_url:
            logger.debug(
                "Ring of Blood pre-submit check id=%s reason=unexpected-page",
                option.battle_id,
            )
            return RingOfBloodStartOutcome.OPTION_UNAVAILABLE

        current = await self.inspect_ring_of_blood()
        current_by_id = {
            current_option.battle_id: current_option
            for current_option in current.options
        }
        current_option = current_by_id.get(option.battle_id)
        logger.debug(
            "Ring of Blood pre-submit check id=%s action_present=%s "
            "snapshot_matches=%s required=%s available=%s",
            option.battle_id,
            current_option is not None,
            current == expected_before,
            option.entry_cost,
            current.tokens_of_blood,
        )
        if current_option is None:
            logger.debug(
                "Ring of Blood option is unavailable id=%s",
                option.battle_id,
            )
            return RingOfBloodStartOutcome.OPTION_UNAVAILABLE
        if current_option != option or current != expected_before:
            logger.debug(
                "Ring of Blood state changed before submission id=%s",
                option.battle_id,
            )
            return RingOfBloodStartOutcome.STATE_CHANGED
        if current.tokens_of_blood < current_option.entry_cost:
            logger.debug(
                "Ring of Blood tokens are insufficient id=%s required=%s available=%s",
                option.battle_id,
                current_option.entry_cost,
                current.tokens_of_blood,
            )
            return RingOfBloodStartOutcome.INSUFFICIENT_TOKENS

        ring_url_js = json.dumps(ring_url)
        try:
            atomic_result = await wait_for_zendriver(
                self.page.evaluate(rf"""
                (() => {{
                    const expectedUrl = {ring_url_js};
                    if (window.location.href !== expectedUrl) return 'unexpected-page';
                    const expectedId = {current_option.battle_id};
                    const expectedCost = {current_option.entry_cost};
                    const table = document.getElementById('arena_list');
                    const initid = document.getElementById('initid');
                    const initform = document.getElementById('initform');
                    if (!table) return 'missing-table';
                    if (!initid) return 'missing-initid';
                    if (!initform) return 'missing-initform';
                    const hasExactAction = Array.from(table.querySelectorAll(
                        'img[onclick*="init_battle"]'
                    )).some((element) => {{
                        const onclick = (
                            element.getAttribute('onclick') || ''
                        ).trim();
                        const match = /^init_battle\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*;?$/.exec(
                            onclick
                        );
                        return match !== null
                            && Number(match[1]) === expectedId
                            && Number(match[2]) === expectedCost;
                    }});
                    if (!hasExactAction) return 'missing-exact-action';
                    initid.value = String(expectedId);
                    initform.submit();
                    return 'submitted';
                }})()
                """),
                timeout=_NAVIGATION_MUTATION_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            logger.error(
                "Battle form submission outcome is unknown "
                "kind=ring-of-blood id=%s error_type=%s",
                option.battle_id,
                type(error).__name__,
            )
            raise
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
        if result != "submitted":
            logger.warning(
                "Battle form was not submitted kind=ring-of-blood id=%s reason=%s",
                option.battle_id,
                result,
            )
            return RingOfBloodStartOutcome.OPTION_UNAVAILABLE

        logger.debug("Submitted Ring of Blood battle form id=%s", option.battle_id)
        return RingOfBloodStartOutcome.SUBMITTED

    async def start_arena(self, option: ArenaOption) -> bool:
        """Submit one Arena option explicitly selected by the caller."""
        arena_url = f"{HENTAIVERSE_ROOT_URL}{await self._path_prefix()}/?s=Battle&ss=ar"
        current_url = await wait_for_zendriver(
            self.page.evaluate("window.location.href"),
            timeout=_NAVIGATION_READ_TIMEOUT_SECONDS,
            owner=self.page,
        )
        if current_url != arena_url:
            logger.debug(
                "Battle form submission skipped kind=arena id=%s "
                "reason=unexpected-page expected=%s current=%s",
                option.battle_id,
                arena_url,
                current_url,
            )
            return False

        token_js = json.dumps(option.token)
        try:
            submitted = await wait_for_zendriver(
                self.page.evaluate(f"""
                (() => {{
                    const initid = document.getElementById('initid');
                    const initform = document.getElementById('initform');
                    if (!initid || !initform) return false;
                    initid.value = '{option.battle_id}';
                    const tokenVal = {token_js};
                    if (tokenVal !== null) {{
                        const inittoken = document.getElementById('inittoken');
                        if (inittoken) inittoken.value = tokenVal;
                    }}
                    initform.submit();
                    return true;
                }})()
                """),
                timeout=_NAVIGATION_MUTATION_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            logger.error(
                "Battle form submission outcome is unknown kind=arena id=%s "
                "error_type=%s",
                option.battle_id,
                type(error).__name__,
            )
            raise
        if submitted is not True:
            logger.warning(
                "Battle form was not submitted kind=arena id=%s",
                option.battle_id,
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

    async def start_grindfest(self, option: GrindfestOption) -> bool:
        """Submit one GrindFest option explicitly selected by the caller."""
        grindfest_url = (
            f"{HENTAIVERSE_ROOT_URL}{await self._path_prefix()}/?s=Battle&ss=gr"
        )
        current_url = await wait_for_zendriver(
            self.page.evaluate("window.location.href"),
            timeout=_NAVIGATION_READ_TIMEOUT_SECONDS,
            owner=self.page,
        )
        if current_url != grindfest_url:
            logger.debug(
                "Battle form submission skipped kind=grindfest id=%s "
                "reason=unexpected-page expected=%s current=%s",
                option.battle_id,
                grindfest_url,
                current_url,
            )
            return False

        try:
            submitted = await wait_for_zendriver(
                self.page.evaluate(f"""
                (() => {{
                    const initid = document.getElementById('initid');
                    const initform = document.getElementById('initform');
                    if (!initid || !initform) return false;
                    initid.value = '{option.battle_id}';
                    initform.submit();
                    return true;
                }})()
                """),
                timeout=_NAVIGATION_MUTATION_TIMEOUT_SECONDS,
                owner=self.page,
            )
        except Exception as error:
            logger.error(
                "Battle form submission outcome is unknown kind=grindfest id=%s "
                "error_type=%s",
                option.battle_id,
                type(error).__name__,
            )
            raise
        if submitted is not True:
            logger.warning(
                "Battle form was not submitted kind=grindfest id=%s",
                option.battle_id,
            )
            return False

        logger.info("Submitted GrindFest battle form id=%s", option.battle_id)
        return True


__all__ = ["BattleLauncher"]

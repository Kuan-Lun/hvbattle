"""Explicit navigation and submission for application-selected battles."""

import json
import re
from typing import Any

from hvbrowser import HENTAIVERSE_ROOT_URL, HVDriver
from hvbrowser.runtime import setup_logger

from .contracts import (
    ArenaOption,
    GrindfestOption,
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


class BattleLauncher:
    """List and submit battle choices without owning selection policy."""

    def __init__(self, browser_client: HVDriver) -> None:
        self.browser_client = browser_client

    @property
    def page(self) -> Any:
        return self.browser_client.page

    async def _path_prefix(self) -> str:
        return "/isekai" if await self.browser_client.is_isekai else ""

    async def _goto_via_battle_menu(self, label: str) -> bool:
        battle_menu = await self.page.select("#parent_Battle")
        target_elements = await self.page.xpath(
            f"//div[contains(text(), '{label}')]", timeout=5
        )
        if not target_elements:
            logger.warning("Unable to find %r in the Battle menu", label)
            return False

        await battle_menu.mouse_move()
        await target_elements[0].mouse_move()
        await self.browser_client.wait(
            target_elements[0].mouse_click,
            ischangeurl=True,
        )
        return True

    async def goto_arena(self) -> bool:
        return await self._goto_via_battle_menu("The Arena")

    async def goto_ring_of_blood(self) -> bool:
        return await self._goto_via_battle_menu("Ring of Blood")

    async def goto_grindfest(self) -> bool:
        return await self._goto_via_battle_menu("GrindFest")

    async def list_arena_options(self) -> tuple[ArenaOption, ...]:
        """Return Arena choices without selecting one for the client."""
        row_payloads = await self.page.evaluate(r"""
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
            """)
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
        """Inspect available Ring challenges and tokens without selecting one."""
        payload = await self.page.evaluate(r"""
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
                        if (!action) return [];
                        const cells = Array.from(row.children).filter(
                            (cell) => cell.tagName === 'TD'
                        );
                        return [{
                            onclick: action.getAttribute('onclick') || '',
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
            """)
        if not isinstance(payload, dict):
            raise RuntimeError("Ring of Blood page did not expose expected structure")
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise RuntimeError("Ring of Blood challenge columns are missing")
        tokens_of_blood = _parse_ring_integer(
            _RING_OF_BLOOD_BALANCE_PATTERN,
            payload.get("tokenText"),
            field="token balance",
        )
        options: list[RingOfBloodOption] = []
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("Ring of Blood challenge row is malformed")
            challenge_name = row.get("challengeName")
            if not isinstance(challenge_name, str) or not challenge_name.strip():
                raise RuntimeError("Ring of Blood challenge name is missing")
            exp_multiplier = _parse_optional_exp_multiplier(row.get("expText"))
            if exp_multiplier is None:
                raise RuntimeError("Unable to parse Ring of Blood EXP multiplier")
            entry_cost = _parse_ring_integer(
                _TOKEN_COST_PATTERN,
                row.get("entryCostText"),
                field="entry cost",
            )
            onclick = row.get("onclick")
            if not isinstance(onclick, str):
                raise RuntimeError("Ring of Blood challenge action is missing")
            action_match = _RING_OF_BLOOD_ACTION_PATTERN.fullmatch(onclick.strip())
            if action_match is None:
                raise RuntimeError("Ring of Blood challenge action is malformed")
            action_cost = int(action_match.group(2))
            if action_cost != entry_cost:
                raise RuntimeError("Ring of Blood challenge entry cost is inconsistent")
            options.append(
                RingOfBloodOption(
                    battle_id=int(action_match.group(1)),
                    challenge_name=challenge_name.strip(),
                    exp_multiplier=exp_multiplier,
                    entry_cost=entry_cost,
                )
            )
        return RingOfBloodSnapshot(tokens_of_blood, tuple(options))

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
        current_url = await self.page.evaluate("window.location.href")
        if current_url != ring_url:
            logger.debug(
                "Battle form submission skipped kind=ring-of-blood id=%s "
                "reason=unexpected-page expected=%s",
                option.battle_id,
                ring_url,
            )
            return RingOfBloodStartOutcome.OPTION_UNAVAILABLE

        current = await self.inspect_ring_of_blood()
        current_by_id = {
            current_option.battle_id: current_option
            for current_option in current.options
        }
        current_option = current_by_id.get(option.battle_id)
        if current_option is None:
            logger.info(
                "Ring of Blood option is unavailable id=%s",
                option.battle_id,
            )
            return RingOfBloodStartOutcome.OPTION_UNAVAILABLE
        if current_option != option or current != expected_before:
            logger.info(
                "Ring of Blood state changed before submission id=%s",
                option.battle_id,
            )
            return RingOfBloodStartOutcome.STATE_CHANGED
        if current.tokens_of_blood < current_option.entry_cost:
            logger.info(
                "Ring of Blood tokens are insufficient id=%s required=%s available=%s",
                option.battle_id,
                current_option.entry_cost,
                current.tokens_of_blood,
            )
            return RingOfBloodStartOutcome.INSUFFICIENT_TOKENS

        ring_url_js = json.dumps(ring_url)
        try:
            submitted = await self.page.evaluate(rf"""
                (() => {{
                    const expectedUrl = {ring_url_js};
                    if (window.location.href !== expectedUrl) return false;
                    const expectedId = {current_option.battle_id};
                    const expectedCost = {current_option.entry_cost};
                    const table = document.getElementById('arena_list');
                    const initid = document.getElementById('initid');
                    const initform = document.getElementById('initform');
                    if (!table || !initid || !initform) return false;
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
                    if (!hasExactAction) return false;
                    initid.value = String(expectedId);
                    initform.submit();
                    return true;
                }})()
                """)
        except Exception as error:
            logger.error(
                "Battle form submission outcome is unknown "
                "kind=ring-of-blood id=%s error_type=%s",
                option.battle_id,
                type(error).__name__,
            )
            raise
        if submitted is not True:
            logger.warning(
                "Battle form was not submitted kind=ring-of-blood id=%s",
                option.battle_id,
            )
            return RingOfBloodStartOutcome.OPTION_UNAVAILABLE

        logger.info("Submitted Ring of Blood battle form id=%s", option.battle_id)
        return RingOfBloodStartOutcome.SUBMITTED

    async def start_arena(self, option: ArenaOption) -> bool:
        """Submit one Arena option explicitly selected by the caller."""
        arena_url = f"{HENTAIVERSE_ROOT_URL}{await self._path_prefix()}/?s=Battle&ss=ar"
        current_url = await self.page.evaluate("window.location.href")
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
            submitted = await self.page.evaluate(f"""
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
                """)
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
        onclick_list = await self.page.evaluate("""
            (() => {
                const imgs = document.querySelectorAll(
                    '#grindfest img[onclick*="init_battle"]'
                );
                return Array.from(imgs).map(
                    (el) => el.getAttribute('onclick') || ''
                );
            })()
            """)
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
        current_url = await self.page.evaluate("window.location.href")
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
            submitted = await self.page.evaluate(f"""
                (() => {{
                    const initid = document.getElementById('initid');
                    const initform = document.getElementById('initform');
                    if (!initid || !initform) return false;
                    initid.value = '{option.battle_id}';
                    initform.submit();
                    return true;
                }})()
                """)
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

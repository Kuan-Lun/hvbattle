"""Explicit navigation and submission for application-selected battles."""

import json
import re
from typing import Any

from hvbrowser import HENTAIVERSE_ROOT_URL, HVDriver
from hvbrowser.runtime import setup_logger

from .contracts import ArenaOption, GrindfestOption

logger = setup_logger(__name__)


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

    async def goto_grindfest(self) -> bool:
        return await self._goto_via_battle_menu("GrindFest")

    async def list_arena_options(self) -> tuple[ArenaOption, ...]:
        """Return Arena choices without selecting one for the client."""
        onclick_list = await self.page.evaluate("""
            (() => {
                const imgs = document.querySelectorAll(
                    'img[onclick*="init_battle"]'
                );
                return Array.from(imgs).map(
                    (el) => el.getAttribute('onclick') || ''
                );
            })()
            """)
        options: list[ArenaOption] = []
        for onclick in onclick_list or ():
            match = re.match(
                r"init_battle\(\s*(\d+)\s*,\s*(\d+)\s*"
                r"(?:,\s*['\"]([^'\"]*)['\"]\s*)?\)",
                onclick,
            )
            if match is None:
                logger.debug(
                    "Arena action did not match expected shape: length=%d",
                    len(onclick),
                )
                continue
            options.append(
                ArenaOption(battle_id=int(match.group(1)), token=match.group(3))
            )
        return tuple(options)

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

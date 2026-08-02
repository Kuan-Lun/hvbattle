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
                logger.debug("Arena action did not match: %s", onclick)
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
        except Exception:
            logger.exception("Arena battle form submission outcome is unknown")
            raise
        if submitted is not True:
            logger.warning("Arena battle form was not submitted")
            return False

        logger.info(
            "Started Arena battle id=%s token=%s",
            option.battle_id,
            "<present>" if option.token else "<none>",
        )
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
                logger.debug("GrindFest action did not match: %s", onclick)
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
        except Exception:
            logger.exception("GrindFest battle form submission outcome is unknown")
            raise
        if submitted is not True:
            logger.warning("GrindFest battle form was not submitted")
            return False

        logger.info("Started GrindFest id=%s", option.battle_id)
        return True


__all__ = ["BattleLauncher"]

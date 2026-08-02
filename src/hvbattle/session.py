"""State inspection and atomic actions for one HentaiVerse battle session."""

import asyncio
import json
import re
from typing import Any, cast

from hv_bie.types import BattleSnapshot
from hvbrowser import HENTAIVERSE_ROOT_URL, HVDriver
from hvbrowser.runtime import is_connection_error, setup_logger
from zendriver import cdp

from .contracts import ArenaOption, GrindfestOption
from .hv_battle_action_manager import ElementActionManager
from .hv_battle_buff_manager import BuffManager
from .hv_battle_item_provider import ItemProvider
from .hv_battle_observer_pattern import BattleDashboard
from .hv_battle_ponychart import PonyChart, preload_ponychart_classifier
from .hv_battle_skill_manager import SkillManager

logger = setup_logger(__name__)

_BATTLE_PHASE_ACTIVE = "active"
_BATTLE_PHASE_COMPLETE = "complete"
_BATTLE_PHASE_NEXT_FLOOR = "next-floor"
_BATTLE_PHASE_JS = r"""
(() => {
    const pane = document.getElementById("pane_completion");
    if (
        pane
        && pane.querySelector('img[src*="finishbattle.png"]')
    ) {
        return "complete";
    }
    if (document.getElementById("btcp")) return "next-floor";
    return "active";
})()
"""


class BattleSession(HVDriver):
    """Reusable battle state and action facade without a concrete policy."""

    def __init__(
        self,
        *args: Any,
        auto_accept_dialogs: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.auto_accept_dialogs = auto_accept_dialogs
        self.battle_dashboard: BattleDashboard | None = None
        self.element_action_manager: ElementActionManager | None = None
        self._item_provider: ItemProvider | None = None
        self._skill_manager: SkillManager | None = None
        self._buff_manager: BuffManager | None = None
        self._completion_observed = False
        self._last_dialog_category: str | None = None
        self._missing_round_metadata_logged = False
        self.turn = -1
        self.round = -1

    async def _init_browser(self) -> None:
        await asyncio.to_thread(preload_ponychart_classifier)
        await super()._init_browser()
        if self.auto_accept_dialogs:
            await self._setup_alert_handler()
        self._initialize_battle_components()

    def _initialize_battle_components(self) -> None:
        """Create battle adapters without parsing a non-battle login page."""
        dashboard = BattleDashboard(self)
        self.battle_dashboard = dashboard
        self.element_action_manager = ElementActionManager(self, dashboard)
        self._item_provider = ItemProvider(self, dashboard)
        self._skill_manager = SkillManager(self, dashboard)
        self._buff_manager = BuffManager(self, dashboard)

    async def _setup_alert_handler(self) -> None:
        async def dialog_handler(
            event: cdp.page.JavascriptDialogOpening,
        ) -> None:
            message = str(getattr(event, "message", ""))
            normalized = message.casefold()
            if "server communication failed" in normalized:
                category = "server-communication-failed"
            elif "login" in normalized or "session" in normalized:
                category = "session-or-login"
            else:
                category = "other"
            self._last_dialog_category = category
            logger.warning(
                "Auto-accepting JavaScript dialog category=%s message_length=%d",
                category,
                len(message),
            )
            await self.page.send(cdp.page.handle_java_script_dialog(accept=True))

        self.page.add_handler(cdp.page.JavascriptDialogOpening, dialog_handler)

    def _dashboard(self) -> BattleDashboard:
        if self.battle_dashboard is None:
            raise RuntimeError("Battle components have not been initialized")
        return self.battle_dashboard

    def _actions(self) -> ElementActionManager:
        if self.element_action_manager is None:
            raise RuntimeError("Battle components have not been initialized")
        return self.element_action_manager

    def _items(self) -> ItemProvider:
        if self._item_provider is None:
            raise RuntimeError("Battle components have not been initialized")
        return self._item_provider

    def _skills(self) -> SkillManager:
        if self._skill_manager is None:
            raise RuntimeError("Battle components have not been initialized")
        return self._skill_manager

    def _buffs(self) -> BuffManager:
        if self._buff_manager is None:
            raise RuntimeError("Battle components have not been initialized")
        return self._buff_manager

    @property
    def snapshot(self) -> BattleSnapshot:
        snapshot = self._dashboard().snap
        if snapshot is None:
            raise RuntimeError("No battle snapshot is available before prepare_turn()")
        return cast(BattleSnapshot, snapshot)

    @property
    def alive_monster_ids(self) -> tuple[int, ...]:
        return tuple(self._dashboard().overview_monsters.alive_monster)

    @property
    def alive_monster_ids_by_name(self) -> dict[str, int]:
        return dict(self._dashboard().overview_monsters.alive_monster_name)

    def monster_ids_with_buff(self, buff: str) -> tuple[int, ...]:
        return tuple(
            self._dashboard().overview_monsters.alive_monster_with_buff.get(buff, ())
        )

    @property
    def current_round(self) -> int:
        return self._dashboard().log_entries.current_round

    @property
    def total_rounds(self) -> int:
        return self._dashboard().log_entries.total_round

    def reset_battle_tracking(self) -> None:
        self.turn = -1
        self.round = -1
        self._completion_observed = False
        self._missing_round_metadata_logged = False
        if self.battle_dashboard is not None:
            self.battle_dashboard.reset()

    def _round_progress_text(self) -> str:
        """Render unavailable resumed-battle metadata without inventing round zero."""
        if self.current_round <= 0 or self.total_rounds <= 0:
            return "Round   ? / ?  "
        return f"Round {self.current_round:>3} / {self.total_rounds:<3}"

    async def prepare_turn(self) -> tuple[str, ...] | None:
        """Refresh one actionable turn, or return ``None`` at a transition."""
        if not await self._has_battle_marker():
            return None

        phase = await self._read_battle_phase()
        if phase == _BATTLE_PHASE_COMPLETE:
            self._completion_observed = True
            logger.info("Final battle completion control is ready.")
            return None
        if phase == _BATTLE_PHASE_NEXT_FLOOR:
            return self._prepare_round_transition()
        if await self.is_ponychart_present():
            return None

        try:
            await self._dashboard().update()
        except Exception as error:
            if is_connection_error(error):
                raise
            if await self.is_ponychart_present():
                return None
            phase = await self._read_battle_phase()
            if phase == _BATTLE_PHASE_COMPLETE:
                self._completion_observed = True
                logger.info("Final battle completion control appeared while parsing.")
                return None
            if phase == _BATTLE_PHASE_NEXT_FLOOR:
                return self._prepare_round_transition()
            raise

        phase = await self._read_battle_phase()
        if phase == _BATTLE_PHASE_COMPLETE:
            self._completion_observed = True
            logger.info("Final battle completion control appeared after parsing.")
            return None
        if phase == _BATTLE_PHASE_NEXT_FLOOR:
            return self._prepare_round_transition()
        if self.snapshot.warnings:
            logger.warning(
                "Battle parser warnings: %s",
                ", ".join(self.snapshot.warnings[:5]),
            )
        if not self.alive_monster_ids:
            raise TimeoutError(
                "Battle DOM has no monsters, round transition, or final "
                "completion marker"
            )
        self.turn += 1
        self.round = self.current_round
        turn_text = f"Turn {self.turn:>5}"
        round_text = self._round_progress_text()
        if "?" in round_text and not getattr(
            self, "_missing_round_metadata_logged", False
        ):
            logger.info(
                "Round metadata is unavailable on this active page; it will "
                "become available after a later round initialization is observed."
            )
            self._missing_round_metadata_logged = True
        lines = tuple(
            f"{turn_text} {round_text} {line}"
            for line in self._dashboard().log_entries.current_lines
        )
        for line in lines:
            logger.info(line)
        return lines

    def _prepare_round_transition(self) -> tuple[str, ...]:
        """Expose a transition as a strategy decision without parsing monsters."""
        self.turn += 1
        logger.info("Turn %5d: next-round control is ready.", self.turn)
        return ()

    async def is_ponychart_present(self) -> bool:
        """Inspect challenge presence without parsing ordinary battle HTML."""
        return await PonyChart(self).is_present()

    async def _read_battle_phase(self) -> str:
        """Read final and next-floor controls atomically, with final priority."""
        phase = await self.page.evaluate(_BATTLE_PHASE_JS)
        if phase not in {
            _BATTLE_PHASE_ACTIVE,
            _BATTLE_PHASE_COMPLETE,
            _BATTLE_PHASE_NEXT_FLOOR,
        }:
            raise RuntimeError(f"Invalid battle phase payload: {phase!r}")
        return str(phase)

    async def _has_battle_completion_marker(self) -> bool:
        """Recognize the game's explicit final-battle image control."""
        return await self._read_battle_phase() == _BATTLE_PHASE_COMPLETE

    async def _has_battle_marker(self) -> bool:
        return bool(await self.page.xpath("//*[@id='battle_main']", timeout=2))

    def get_stat_percent(self, stat: str) -> float:
        match stat.lower():
            case "hp":
                value = self.snapshot.player.hp_percent
            case "mp":
                value = self.snapshot.player.mp_percent
            case "sp":
                value = self.snapshot.player.sp_percent
            case "overcharge":
                value = self.snapshot.player.overcharge_value
            case _:
                raise ValueError(f"Unknown stat: {stat}")
        return float(value)

    async def cast_skill(self, key: str) -> bool:
        """Cast a non-targeted skill and wait for the resulting game action."""
        return await self._skills().cast(key, iswait=True)

    async def select_targeted_skill(self, key: str) -> bool:
        """Select a targeted skill without claiming a completed game action."""
        return await self._skills().cast(key, iswait=False)

    async def use_item(self, key: str) -> bool:
        return await self._items().use(key)

    async def apply_buff(self, key: str, *, force: bool = False) -> bool:
        return await self._buffs().apply_buff(key=key, force=force)

    def is_buff_action_needed(self, key: str, *, force: bool = False) -> bool:
        """Inspect whether applying a named buff would be useful now."""
        return self._buffs().is_action_needed(key, force=force)

    def has_buff(self, key: str) -> bool:
        return self._buffs().has_buff(key)

    def get_buff_remaining_turns(self, key: str) -> int | float:
        return self._buffs().get_buff_remaining_turns(key)

    def get_buff_observed_max_turns(self, key: str) -> int:
        return self._buffs().get_observed_max_turns(key)

    def get_max_skill_mp_cost(self, key: str) -> int:
        return self._skills().get_max_skill_mp_cost_by_name(key)

    async def attack_monster(self, slot: int) -> bool:
        selector = f'[id="mkey_{slot}"]'
        elements = await self.page.query_selector_all(selector)
        if not elements:
            return False
        await self._actions().click_and_wait_log_locator(selector)
        return True

    async def attack_monster_by_skill(self, slot: int, skill_name: str) -> bool:
        if not await self.select_targeted_skill(skill_name):
            return False
        return await self.attack_monster(slot)

    @property
    def battle_completion_observed(self) -> bool:
        """Whether the game's explicit final-battle control was observed."""
        return self._completion_observed

    async def resolve_ponychart(self) -> bool:
        return await PonyChart(self).check()

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
        await self.wait(target_elements[0].mouse_click, ischangeurl=True)
        return True

    async def goto_arena(self) -> bool:
        return await self._goto_via_battle_menu("The Arena")

    async def goto_grindfest(self) -> bool:
        return await self._goto_via_battle_menu("GrindFest")

    async def go_next_floor(self) -> bool:
        elements = await self.page.query_selector_all("#btcp")
        if not elements:
            return False
        await self._actions().click_and_wait_transition_locator("#btcp")
        return True

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
            if not match:
                logger.debug("Arena action did not match: %s", onclick)
                continue
            options.append(
                ArenaOption(battle_id=int(match.group(1)), token=match.group(3))
            )
        return tuple(options)

    async def start_arena(self, option: ArenaOption) -> bool:
        """Submit one Arena option explicitly selected by the caller."""
        path_prefix = await self._get_path_prefix()
        arena_url = f"{HENTAIVERSE_ROOT_URL}{path_prefix}/?s=Battle&ss=ar"
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
            if not match:
                logger.debug("GrindFest action did not match: %s", onclick)
                continue
            options.append(GrindfestOption(battle_id=int(match.group(1))))
        return tuple(options)

    async def start_grindfest(self, option: GrindfestOption) -> bool:
        """Submit one GrindFest option explicitly selected by the caller."""
        path_prefix = await self._get_path_prefix()
        grindfest_url = f"{HENTAIVERSE_ROOT_URL}{path_prefix}/?s=Battle&ss=gr"
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

    async def is_in_battle(self) -> bool:
        """Return whether the current page represents an active battle."""
        if await self.is_ponychart_present():
            return True
        phase = await self._read_battle_phase()
        if phase == _BATTLE_PHASE_COMPLETE:
            self._completion_observed = True
            return False
        if phase == _BATTLE_PHASE_NEXT_FLOOR:
            return True
        if not await self._has_battle_marker():
            if await self.is_ponychart_present():
                return True
            logger.info("No active battle detected.")
            return False

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                snapshot = await self._dashboard().inspect()
                if any(monster.alive for monster in snapshot.monsters.values()):
                    return True
                if await self.is_ponychart_present():
                    return True
                phase = await self._read_battle_phase()
                if phase == _BATTLE_PHASE_COMPLETE:
                    self._completion_observed = True
                    return False
                if phase == _BATTLE_PHASE_NEXT_FLOOR:
                    return True
                warnings = ", ".join(snapshot.warnings[:5]) or "none"
                raise RuntimeError(
                    "Battle marker is present without monsters, a transition, "
                    "or completion evidence; parser warnings=" + warnings
                )
            except Exception as error:
                if is_connection_error(error):
                    raise
                last_error = error
                if await self.is_ponychart_present():
                    return True
                phase = await self._read_battle_phase()
                if phase == _BATTLE_PHASE_COMPLETE:
                    self._completion_observed = True
                    return False
                if phase == _BATTLE_PHASE_NEXT_FLOOR:
                    return True
                if isinstance(error, TimeoutError):
                    raise
                if attempt == 0:
                    logger.info(
                        "Battle state parse failed, retrying once after idle wait."
                    )
                    await self.page.wait(1)

        phase = await self._read_battle_phase()
        if phase == _BATTLE_PHASE_COMPLETE:
            self._completion_observed = True
            return False
        if phase == _BATTLE_PHASE_NEXT_FLOOR:
            return True
        if await self._has_battle_marker():
            logger.error(
                "On battle page but failed to parse battle state twice; "
                "refusing to report 'not in battle'."
            )
            assert last_error is not None
            raise last_error

        logger.info("No active battle detected.")
        return False

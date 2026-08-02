# HVBattle

HVBattle provides reusable HentaiVerse battle-domain APIs. It builds on
`hvbrowser` for the authenticated Hentaiverse browser session and keeps the
private command-line runner in a separate application workspace.

The package exposes a policy-neutral `BattleSession`, atomic battle actions,
and a `BattleRunner` that runs exactly one already-active battle using a
client-supplied `BattleStrategy`. It never repairs equipment, recovers stamina,
or starts Arena/GrindFest on its own. Campaign policy and post-battle work belong
to the calling application.

`BattleSession` preloads the PonyChart classifier and ONNX model before opening
the browser, so a timed challenge never pays the first-load cost. The runner
checks for and resolves PonyChart before parsing an ordinary battle turn or
calling client strategy code.

```python
import asyncio

from hvbattle import BattleCompleted, BattleRunner, BattleSession, TurnDecision


class MyStrategy:
    async def take_turn(self, session: BattleSession, /) -> TurnDecision:
        if await session.go_next_floor():
            return TurnDecision.ACTED
        if not session.alive_monster_ids:
            return TurnDecision.IDLE
        if await session.attack_monster(session.alive_monster_ids[0]):
            return TurnDecision.ACTED
        return TurnDecision.IDLE


async def after_battle(session: BattleSession, completed: BattleCompleted) -> None:
    print(completed)


async def main() -> None:
    async with BattleSession(headless=True) as session:
        result = await BattleRunner(session, MyStrategy()).run_current()
        if isinstance(result, BattleCompleted):
            await after_battle(session, result)


asyncio.run(main())
```

The example requires credentials through the normal `EH_USERNAME` and
`EH_PASSWORD` indirection and an already-active server battle; otherwise
`run_current()` returns `None`. `TurnDecision.STOP` deliberately returns a
`BattleStopped` result. Leaving the battle page without positive final-round
completion evidence raises `BattleInterruptedError`, so callers cannot mistake
an expired login or unexpected navigation for a completed battle.

`BattleDriver` is only a transitional name alias for `BattleSession`, not an
API-compatible implementation of the old driver. Migrate constructor strategy
settings into a `BattleStrategy`, replace `driver.battle()` with
`BattleRunner(driver, strategy).run_current()`, and perform maintenance,
post-battle tasks, and next-battle selection after the returned
`BattleCompleted`. Arena choice follows the same boundary:
`list_arena_options()` returns data and `start_arena(option)` starts only the
option explicitly selected by the caller.
GrindFest uses the equivalent `list_grindfest_options()` and
`start_grindfest(option)` pair; the package does not silently choose the first
or last server option.

`BaseControlPanel`, `ControlPanel`, and `NullControlPanel` provide reusable
pause, named-action, named-toggle, and validated integer mechanisms. The
`set_actions()` API is the generic spelling; the older `set_skills()` spelling
remains available as a compatibility alias. Integer edits become live only
after Apply or Enter, so partially typed mutation amounts are never published.
Closing the interactive window sets the pause flag before the GUI exits.

The package does not register campaign choices or choose their defaults: a
calling application owns the control names, labels, initial values, and the
policy that reads their committed state. Importing `hvbattle` does not import
Tk or start a GUI process.

## Development

Build a clean environment backed by PyPI releases:

```bash
bash scripts/rebuild-env.sh
```

For coordinated local development before all dependent releases are on PyPI,
overlay editable checkouts in dependency order:

```bash
uv pip install --python .venv/bin/python --reinstall --no-deps --editable \
  /Users/kuanlun_wang/Desktop/git-repo/hbrowser.clone
uv pip install --python .venv/bin/python --reinstall --no-deps --editable \
  /Users/kuanlun_wang/Desktop/git-repo/hvbrowser.clone
```

Commands that must preserve these editable overlays use `uv run --no-sync`.

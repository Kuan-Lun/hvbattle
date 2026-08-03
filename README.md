# HVBattle

HVBattle provides reusable HentaiVerse battle-domain APIs. It builds on
`hvbrowser` for the authenticated Hentaiverse browser session and keeps the
private command-line runner in a separate application workspace.

The package exposes a policy-neutral `BattleSession`, atomic battle actions,
and a `BattleRunner` that runs exactly one already-active battle using a
client-supplied `BattleStrategy`. It never repairs equipment, recovers stamina,
or starts Arena/GrindFest on its own. Campaign policy and post-battle work belong
to the calling application.

`BattleSession` is a facade, not an `HVDriver` subclass. It composes an explicit
`browser_client`, a battle-scoped state store, a battle launcher, and shared
action/item/skill/buff collaborators. Callers that also need non-battle
`hvbrowser` operations use `session.browser_client` explicitly; this prevents
maintenance APIs from leaking into the battle-domain surface. The legacy
`battle_dashboard` name remains a compatibility alias for the state store.

Version 0.2.7 restores the game's final completion acknowledgement as a
runner-owned safety step. `BattleRunner` captures the immutable completion and
round summary before clicking the exact `finishbattle.png` control at most
once, revalidating that the selected control still belongs to the observed
completion document first. It returns `BattleCompleted` only after a new,
ready document on the same realm has no battle, finish, next-floor, or
PonyChart controls. A click or navigation error is reconciled through read-only
state probes and is never resent; missing positive exit evidence raises
`BattleInterruptedError`.

Turn preparation uses `BattleTurnState` and `BattleTurnPhase` to distinguish an
active turn, next-floor transition, PonyChart challenge, positive completion,
and an absent battle page. `BattleRunner` consumes this typed state directly;
the former sentinel-returning `prepare_turn()` remains only as a compatibility
adapter.

`BattleSession` preloads the PonyChart classifier and ONNX model before opening
the browser, so a timed challenge never pays the first-load cost. The runner
checks for and resolves PonyChart before parsing an ordinary battle turn or
calling client strategy code. Classifier screenshots are temporary and removed
after every attempt. Failure artifacts are retained only when
`ponychart_diagnostic_directory` is explicitly configured, and that directory
is bounded by `ponychart_diagnostic_file_limit`.

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

The dependency-free Lean model in `formal/` covers the safety-critical action
and transition evidence predicates, the final-completion acknowledgement click
bound, error-record ordering, and supervisor no-retry exit policy. Run it
separately from the Python checks:

```bash
(cd formal && lake build)
```

This is a proved model of the observable decision boundary, not an automatic
translation of the Python, browser, network, or log-sink implementations. The
offline Python and shell tests cover those implementation boundaries.

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

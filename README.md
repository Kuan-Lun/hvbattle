# HVBattle

HVBattle provides reusable HentaiVerse battle-domain APIs. It builds on
`hvbrowser` for the authenticated Hentaiverse browser session and keeps the
private command-line runner in a separate application workspace.

The package exposes a policy-neutral `BattleSession`, atomic battle actions,
and a `BattleRunner` that runs exactly one already-active battle using a
client-supplied `BattleStrategy`. It never repairs equipment, recovers stamina,
or starts Arena, Ring of Blood, or GrindFest on its own. Campaign policy and
post-battle work belong to the calling application.

`BattleSession` is a facade, not an `HVDriver` subclass. It composes an explicit
`HentaiVerseSession`, a battle-scoped state store, a battle launcher, and shared
action/item/skill/buff collaborators. The raw browser used by battle components
is `session.hentaiverse.browser`; non-battle operations remain grouped under
the other `session.hentaiverse` services instead of leaking into the
battle-domain surface. The legacy `battle_dashboard` name remains a
compatibility alias for the state store.

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

Next-floor transition DOM is never accepted over a retained duplicate XHR
receipt: a matching monitor must be either unsent with count zero or sent once
with count one, even when the next round is already visible. If transition
evidence remains unknown, recovery evidence freezes its XHR fields from that
same retained monitor, so a duplicate cannot be hidden behind an unrelated
count-one record.

An ambiguous submitted action has two exact same-browser recovery classes. A
turn or next-floor action may bind the sanitized
`server-communication-failed` dialog to its action token and carry either the
precise terminal status-zero error or an incomplete zero/one-send receipt. A
turn action is also recoverable without a dialog when the browser observes one
XHR remaining pending for at least five seconds on the same known document,
with null status/outcome. For this stalled-XHR class, unknown documents,
duplicate sends, other dialogs, younger requests, and completed requests fail
closed.

For the stalled-XHR class, the coordinator reads the document once more to
avoid racing an automatic navigation, then reloads the current page at most
once if that document is still unchanged. After that reload it polls for at
most ten seconds, ending the polling window immediately once the required
stable state is present rather than sleeping for the full duration. Recovery never replays the
cached action. It accepts only a new, ready document on the expected
persistent/Isekai realm after at least two stable state signatures. Complete
and next-floor controls take priority over PonyChart; an active phase
additionally needs its log/action-control markers and must parse with a live
monster. The runner then returns to turn preparation and asks strategy for a
fresh decision. A second ambiguity before a confirmed `ACTED` or next-floor
receipt exhausts that browser's recovery budget. Only this typed exhaustion
may open one fresh authenticated browser, whose own same-browser budget starts
unused; it can never open a third browser. Final-completion acknowledgement
ambiguity never enters that fresh-browser path.

A `ZendriverOperationTimeout` is not treated like an ordinary retryable
`TimeoutError`, because its CDP command deliberately remains live. A live
timeout while arming or cleaning an action monitor, or while parsing session
state, interrupts and closes the current browser without retry. After a click,
it leaves the post-click document unobservable, so that unknown action is not
eligible for first-use recovery. If it occurs inside recovery after incident
evidence was already established, the failed old-browser reconciliation is
typed exhaustion and may use only the one fresh-browser stage described above.

`BattleSession` preloads the PonyChart classifier and ONNX model before opening
the browser, so a timed challenge never pays the first-load cost. The runner
checks for and resolves PonyChart before parsing an ordinary battle turn or
calling client strategy code. Set `ponychart_image_directory` to retain the
image captured for every detected challenge, including successful resolutions
and failures. Each capture receives a collision-resistant `pony_chart_*.png`
name, and callers own retention for that directory. Without an image directory,
classifier screenshots remain temporary and are removed after each attempt.

```python
import asyncio

from hvbrowser import HentaiVerseSession
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
    hentaiverse = HentaiVerseSession(headless=True)
    async with BattleSession(
        hentaiverse=hentaiverse,
        ponychart_image_directory="pony_chart",
    ) as session:
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
`goto_ring_of_blood()` and `inspect_ring_of_blood()` expose the currently
startable named challenges, EXP modifiers, entry costs, and live Tokens of
Blood balance. `start_ring_of_blood(option, expected_before=snapshot)`
revalidates the page and sanitized snapshot before submitting the existing
form. It returns a typed submitted, insufficient-tokens, unavailable, or
state-changed outcome; it never chooses a challenge or reads hidden form
credentials for the caller.
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
and transition evidence predicates, the guarded same-browser recovery budget
and no-replay boundary, the final-completion acknowledgement click bound,
error-record ordering, and supervisor no-retry exit policy. Run it separately
from the Python checks:

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

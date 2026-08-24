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
battle-domain surface.

Every `BattleInterruptedError`, including each subclass, requires a keyword-only
`diagnostic_code`. The code is a validated 1–128 character lowercase ASCII
machine identifier matching `[a-z0-9][a-z0-9_.:-]*`; durable records and public
failure classification should use it. The exception message remains private,
human-oriented diagnostic detail.

The game's final completion acknowledgement is a runner-owned safety step.
`BattleRunner` captures the immutable completion and round summary before
clicking the exact `finishbattle.png` control at most once, revalidating that
the selected control still belongs to the observed completion document first.
It returns `BattleCompleted` only after a new, ready document on the same realm
has no battle, finish, next-floor, or PonyChart controls. A click or navigation
error is reconciled through read-only state probes and is never resent; missing
positive exit evidence raises `BattleInterruptedError`.

Battle presence and turn readiness are separate safety boundaries.
`BattlePresence.ACTIVE` means positive evidence forbids another navigation or
submission; a `#battle_main` shell is sufficient and is never parsed merely to
prove presence. Turn preparation then uses `BattleTurnState` and
`BattleTurnPhase` to distinguish `NOT_READY`, an actionable active turn,
next-floor transition, PonyChart challenge, positive completion, and an absent
battle page. `BattleRunner` defers a `NOT_READY` document without invoking
client lifecycle or strategy code. If it remains unready through the bounded
deadline, the runner saves one bounded, redacted `battle_state_not_ready` page
diagnostic and raises `BattleStateReadinessError` rather than reporting the
battle absent or resubmitting it.

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
stable state is present rather than sleeping for the full duration. Recovery
never replays the cached action. It accepts only a new, ready document on the expected
persistent/Isekai realm after at least two stable state signatures. Complete
and next-floor controls take priority over PonyChart; an active phase
additionally needs its log/action-control markers and must parse with a live
monster. The runner then returns to turn preparation and asks strategy for a
fresh decision. A second ambiguity before a confirmed `ACTED` or next-floor
receipt exhausts that browser's recovery budget and raises
`BattleRecoveryExhaustedError` with diagnostic code
`battle.action-recovery-exhausted`. An unmatched action ambiguity raises
`BattleInterruptedError` with `battle.action-outcome-unknown`. HVBattle does
not open a replacement browser or choose a worker-restart budget; the calling
application owns that policy and must continue to honor the no-replay receipt
guards.

A `ZendriverOperationTimeout` is not treated like an ordinary retryable
`TimeoutError`, because its CDP command deliberately remains live. A live
timeout while arming or cleaning an action monitor, or while parsing session
state interrupts the runner. The calling application must retire that browser
generation instead of retrying the still-live operation in place. After a
click, the timeout leaves the post-click document unobservable, so that unknown
action is not eligible for first-use recovery. If it occurs inside recovery
after incident evidence was already established, the failed old-browser
reconciliation is reported as typed recovery exhaustion. Any subsequent
browser replacement or worker restart remains a calling-application decision.

`BattleSession` preloads the PonyChart classifier and ONNX model before opening
the browser, so a timed challenge never pays the first-load cost. The runner
checks for and resolves PonyChart before parsing an ordinary battle turn or
calling client strategy code. The displayed image's byte-exact CDP Network
response body stays in memory through classification and challenge handling;
an unavailable or invalid response aborts without a screenshot or re-request
fallback. Set `ponychart_image_directory` to
retain every detected challenge, including successful resolutions and failures;
the captured bytes are written once after each attempt under a
collision-resistant `pony_chart_*` name with its native PNG, JPEG, or WebP
suffix, and callers own retention for that directory. Without an image
directory, challenge images are never written to the filesystem.

PonyChart model files are stored as immutable, content-addressed generations.
Each update downloads both files over verified TLS into a same-filesystem
staging directory with finite socket and whole-bundle deadlines, exact
`Content-Length` accounting, and artifact size limits. It confirms that each
available response ETag agrees with HEAD checks before and after the transfer,
loads the pair through `PonyChartClassifier`, and fsyncs it before one atomic
`current.json` pointer replacement commits it. A local interrupted, partial, or
corrupt update therefore cannot advance the pointer to a partial or old/new
local pair, and old committed generations are retained. ETags are opaque cache
metadata rather than a cryptographic server-side bundle manifest. A fixed
advisory lock serializes pointer decisions across processes, preventing a
slower updater from replacing a newer commit. With no committed pointer, the
complete remote bundle is always fetched; uncommitted canonical cache files are
not adopted.

`refresh_ponychart_classifier()` first adopts an already committed generation
from another process, then checks remote metadata and downloads only when
needed. It returns `PonyChartRefreshOutcome.UPDATED` or
`PonyChartRefreshOutcome.CURRENT`; unreachable metadata, transport failures,
validation failures, and commit failures raise instead of being reported as
current. Any failure leaves the previously published predictor-generation pair
available, while each prediction already in flight finishes on the exact
snapshot with which it started.

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

`BattleSession` is not an API-compatible implementation of the old driver.
Migrate constructor strategy settings into a `BattleStrategy`, replace
`driver.battle()` with `BattleRunner(session, strategy).run_current()`, and
perform maintenance, post-battle tasks, and next-battle selection after the
returned `BattleCompleted`. Arena choice follows the same boundary:
`list_arena_options()` returns data and `start_arena(option)` starts only the
option explicitly selected by the caller.
The three `goto_*()` listing operations require an explicit `expected_realm`,
issue one canonical realm-scoped Battle URL GET, and then wait for an explicit
ten-second readiness deadline. Each readiness observation atomically reads URL
identity, all four battle markers, and the route-specific DOM. A blocker is
accepted when that same observation proves the trusted origin, expected realm,
and exact realm root path. A marker-free listing additionally requires the
exact canonical Battle query and its route DOM. Loading or otherwise unknown
documents are observed again until the deadline; the GET itself is never
retried. The operations do not hover, expand, inspect, or click the site menu.

If readiness remains unknown at the deadline, `hbrowser` captures one bounded,
private HTML diagnostic named `battle_route_not_ready` under
`HBROWSER_LOG_DIR`. Encounter query values are redacted by `hbrowser`, and its
file-size and retention limits apply. The raised `BattleRouteReadinessError`
exposes the resulting `diagnostic_path` for the application log without placing
HTML or raw URL queries in ordinary logs. A diagnostic capture failure is
recorded on the readiness error when the browser generation and log sink remain
usable. Browser-generation and log-persistence failures remain fatal and
propagate unchanged.

`inspect_battle_presence()` reports only what the current document represents;
its `ABSENT` result can describe a stale pre-battle tab and is therefore not a
startup decision. Account orchestration must use
`reconcile_startup_battle_presence(expected_realm=...)`. A current-document
marker wins only when the same atomic observation proves the expected trusted
realm and root path. A marker-free current document, including a non-HV page,
causes one canonical Arena-listing GET for the explicit realm. A trusted
redirect blocker for PonyChart, active battle, or next-floor is adopted as
`ACTIVE`, and the final completion blocker is adopted as `COMPLETION`. A marker
in the current document on an untrusted origin, wrong realm, or unexpected path
is a navigation safety error, never battle evidence. After the canonical GET,
those identities remain unknown and are polled until the deadline. A trusted
battle redirect may instead use a route such as `ss=ba` with a private
`encounter` value, so its blocker is accepted without requiring the original
listing query. Neither that query nor the encounter value is emitted to the
ordinary log. The exact listing query remains mandatory when no blocker exists.
`ABSENT` is accepted only after the deadline observer reaches the validated
realm-scoped route with its Arena DOM and no blocker.

This atomic observation contract is the `hvbattle` 0.13 / `hvbrowser` 0.9
package line; the dependency range intentionally rejects older or newer minor
lines with different navigation contracts.

`goto_ring_of_blood()` and `inspect_ring_of_blood()` expose every listed named
challenge, including rows without a current start action, together with EXP
modifiers, entry costs, and the live Tokens of Blood balance. The snapshot's
`options` tuple contains only submit-capable actions, while `challenges` keeps
the complete row list; unavailable rows use `None` when EXP or entry-cost
metadata is not exposed. `start_ring_of_blood(option, expected_before=snapshot)`
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

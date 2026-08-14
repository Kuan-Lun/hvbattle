# HVBattle Lean safety model

This directory contains a dependency-free Lean 4 model of HVBattle's
safety-critical evidence decisions.

It covers:

- next-round transition evidence and document readiness;
- authoritative XHR/action receipts;
- immutable action/dialog-token evidence for the exact communication-failure
  recovery class and browser-observed request-age evidence for a stalled
  single-XHR turn;
- same-browser reconciliation requiring a new, ready, stable same-realm
  document and a fresh prepare/strategy decision rather than action replay;
- a consecutive-recovery budget reset only by confirmed `ACTED` or next-floor
  receipts;
- final-completion acknowledgement with at most one click and a new, ready,
  same-realm out-of-battle document receipt;
- fail-closed unknown outcomes;
- manager/runner/browser-driver/application error-record obligations; and
- the application's exit 2/3/4/5 terminal stops at both shell retry layers,
  including checked-sink precedence over idle timeout.

Run the checker from this directory:

```bash
lake build
```

The model proves properties of the decision boundary represented by observable
battle snapshots. It does not translate or verify Python automatically, and it
does not prove that Chromium, the DOM probe, the network, Python logging
handlers, or a calling application's persistent log sink faithfully reports
those observations. Python tests remain necessary to keep the production
implementation aligned with the model and to test those runtime boundaries.

The exit policy models `main.py` and `main.sh` in the private battle workspace,
plus `should_retry_battle` in the outer `battle.zsh` launcher. It proves that a
configuration failure exits with 2, post-battle task failure exits with 3, and
an uncertain battle exits with 4. With a healthy `tee`, the logging wrappers
preserve every child status. If `tee` fails, they preserve only 2/3/4 and map
all other child statuses, including 0 and 1, to dedicated logging-failure exit
5. Neither supervisor may retry 2, 3, 4, or 5 for any retry counter or limit.
The checked-sink marker is proved to take precedence whenever it coincides with
an idle-timeout observation. A replaced log-path identity is modeled as a sink
failure and is therefore proved to stop before idle classification. Failure to
append a supervisor decision uses the same fail-closed status matrix.
For an unknown transition handled without an application fresh-reconciliation
context, the application-level audit records are additionally proved to end
with the exact ordered error-record suffix: manager, runner, then browser-driver
context exit, then application. A shell supervisor may append its own later
lifecycle record.

For a changed document or battle node, both `interactive` and `complete` are
accepted only when the round has advanced or initialized and the new battle is
actionable. `loading` and `unknown` are rejected. A same-document AJAX round
advance is instead justified by the round/log evidence and does not depend on
document load readiness. The actual transition-selection path additionally
checks the retained matching XHR monitor: an absent monitor is allowed, while a
present one must be exactly unsent/count-zero or sent/count-one. A known
duplicate dispatch is rejected even when the advanced DOM has otherwise
positive transition evidence. The unknown-transition path freezes all recovery
XHR fields from that same retained monitor; the formal API cannot pair a
duplicate transition receipt with independently injected count-one recovery
evidence.

An ambiguous submitted action has two modeled recovery incidents. The first
requires known pre/post document identities and binds the sanitized
`server-communication-failed` dialog token to the same turn or next-floor
action token. Its terminal XHR observation must either be exactly one completed
status-zero network-error request, or be incomplete with null status/outcome
and exactly either zero unsent requests or one sent request. The second is a
turn-only stalled request: browser-observed request age proves that its XHR has
remained pending for at least five seconds, its known pre/post document is
unchanged, no dialog was observed, and exactly one sent XHR remains incomplete
with null status/outcome. This age guard prevents a slow click from making a
recently sent request look stalled.

The model makes the runtime's strict field types structural: action kind is a
closed type, click/XHR flags are booleans, and send count is a natural number,
so a boolean cannot be accepted as integer count `1`. The coordinator
additionally requires an available recovery budget and a new document on the
expected persistent/Isekai realm whose `interactive` or `complete` state has
the same full signature across at least two reads and final verification. Phase
classification follows runtime priority: complete, next floor, PonyChart, then
active. PonyChart remains valid without the ordinary battle container; active
requires log/action-control markers plus a successful parse with a live
monster. Accepted recovery performs at most one manual reload, clears
page/action and session caches, drops the cached submitted action, resets parser
state unless the accepted phase is complete, and leads only to fresh turn
preparation. Runtime reconciliation polls for at most ten seconds after the
reload and ends that wait as soon as the stable-state guards hold; this wall-clock
liveness bound is outside the transition model. The model has no transition
from recovery to cached-action replay.

Another unknown before a confirmed `ACTED` or next-floor receipt produces typed
recovery exhaustion; either confirmed receipt restores the current browser's
budget. Failed reconciliation of matching incident evidence is also typed
exhaustion, while an unmatched first unknown is an ordinary terminal
interruption.

At the application boundary, only typed same-browser recovery exhaustion may
open one fresh authenticated browser for current-battle reconciliation.
Opening it consumes the application-level attempt but initializes the new
browser's own same-browser recovery budget as unused. Unrelated interruptions
and final-completion acknowledgement ambiguity never open it, and an ambiguity
in that fresh browser cannot open a third browser. The model covers this
decision and state threading; browser close/login mechanics remain outside the
formal boundary and are exercised by runtime tests.

`ZendriverOperationTimeout` is classified before entering this model because
its non-cancelled CDP operation remains live. Monitor-arm, monitor-cleanup, and
session-parse occurrences become ordinary terminal interruptions: the model
proves exit 4, no fresh reconciliation, and no supervisor retry, while runtime
tests cover actual browser closure. A post-click occurrence is represented by
an unobservable post-click document and is proved unable to match the recovery
incident predicate. A recovery probe, reload, active parse, or recovery-cleanup
occurrence is represented by failure of the stable coordinator guard; when
incident evidence had already matched, this produces typed exhaustion and may
enter the sole fresh-browser stage. Final-acknowledgement live ambiguity remains
inside the separately proved terminal no-fresh path.

Final completion is modeled separately from next-floor progression. The exact
final control is revalidated in its original document before it may be clicked
at most once. Success requires a changed document on the expected realm whose
ready state is `interactive` or `complete`, with the battle, finish,
next-floor, and PonyChart controls all absent. Missing or ambiguous evidence is
a terminal interruption, is not eligible for fresh-browser reconciliation, and
is never eligible for either supervisor retry.

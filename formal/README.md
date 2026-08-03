# HVBattle Lean safety model

This directory contains a dependency-free Lean 4 model of HVBattle's
safety-critical evidence decisions.

It covers:

- next-round transition evidence and document readiness;
- authoritative XHR/action receipts;
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
For an unknown transition, the application-level audit records are additionally
proved to end with the exact ordered error-record suffix: manager, runner, then
browser-driver context exit, then application. A shell supervisor may append
its own later lifecycle record.

For a changed document or battle node, both `interactive` and `complete` are
accepted only when the round has advanced or initialized and the new battle is
actionable. `loading` and `unknown` are rejected. A same-document AJAX round
advance is instead justified by the round/log evidence and does not depend on
document load readiness.

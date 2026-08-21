import Std

/-!
# HVBattle safety model

This module is a dependency-free Lean model of the safety-critical evidence
selection performed by `hv_battle_action_manager.py` and of the unknown-outcome
mapping through `runner.py`, the browser-driver context exit, and the calling
application.

It intentionally models observable snapshots and receipts. Browser, network,
DOM, and Python runtime correctness remain assumptions at this boundary.
-/

namespace HVBattle

inductive BattlePresence where
  | absent
  | active
  | completion
  deriving DecidableEq, Repr

inductive BattleTurnPhase where
  | absent
  | challenge
  | notReady
  | active
  | nextFloor
  | complete
  deriving DecidableEq, Repr

inductive ReadinessBudgetOutcome where
  | defer
  | failClosed
  deriving DecidableEq, Repr

/-!
Presence protects navigation and submission; turn readiness protects strategy
execution. A marker-only battle shell is therefore positive active presence but
does not authorize policy code. Readiness-budget exhaustion has no transition
to absence or resubmission: it can only fail closed.
-/
def inspectCurrentBattlePresence
    (challenge completion nextFloor battleMarker : Bool) : BattlePresence :=
  if completion then .completion
  else if challenge || nextFloor || battleMarker then .active
  else .absent

def strategyAuthorized : BattleTurnPhase → Bool
  | .active => true
  | _ => false

def readinessBudgetOutcome
    (elapsedSeconds deadlineSeconds : Nat) : ReadinessBudgetOutcome :=
  if elapsedSeconds < deadlineSeconds then .defer else .failClosed

theorem markerOnlyProvesActivePresence :
    inspectCurrentBattlePresence false false false true = .active := by
  native_decide

theorem markerOnlyReadinessCannotAuthorizeStrategy :
    strategyAuthorized .notReady = false := by
  rfl

theorem onlyParsedActiveStateAuthorizesStrategy
    (phase : BattleTurnPhase)
    (authorized : strategyAuthorized phase = true) :
    phase = .active := by
  cases phase <;> simp_all [strategyAuthorized]

theorem readinessDeadlineExhaustionFailsClosed
    (elapsedSeconds deadlineSeconds : Nat)
    (exhausted : deadlineSeconds ≤ elapsedSeconds) :
    readinessBudgetOutcome elapsedSeconds deadlineSeconds = .failClosed := by
  simp [readinessBudgetOutcome, Nat.not_lt.mpr exhausted]

inductive StartupNavigationBlocker where
  | challenge
  | completion
  | nextFloor
  | active
  deriving DecidableEq, Repr

inductive StartupNavigationIdentity where
  | expected
  | wrongRealm
  | wrongPath
  | untrusted
  deriving DecidableEq, Repr

inductive StartupReconciliationDecision where
  | navigateCanonical
  | resolved (presence : BattlePresence)
  | rejected
  deriving DecidableEq, Repr

/-!
URL identity and all battle markers belong to one atomic observation. A marker
may become positive state evidence only after that same observation proves the
trusted origin, expected realm, and exact realm root path. A marker-free current
document, including an untrusted document, authorizes only a canonical GET. Its
canonical result may use a blocker after identity verification even when a real
battle redirect no longer has the listing query. Marker-free absence requires
both the exact listing query and the expected route DOM.
-/
def startupBlockerPresence : StartupNavigationBlocker → BattlePresence
  | .completion => .completion
  | .challenge | .nextFloor | .active => .active

def reconcileCurrentStartupObservation
    (identity : StartupNavigationIdentity)
    (blocker : Option StartupNavigationBlocker) :
    StartupReconciliationDecision :=
  match blocker with
  | none => .navigateCanonical
  | some blocker =>
      if identity = .expected then
        .resolved (startupBlockerPresence blocker)
      else
        .rejected

def reconcileCanonicalStartupObservation
    (identity : StartupNavigationIdentity)
    (blocker : Option StartupNavigationBlocker)
    (routeQueryValidated routeDomReady : Bool) :
    StartupReconciliationDecision :=
  if identity ≠ .expected then
    .rejected
  else
    match blocker with
    | some blocker =>
        .resolved (startupBlockerPresence blocker)
    | none => if routeQueryValidated then
        if routeDomReady then .resolved .absent else .rejected
      else
        .rejected

theorem markerFreeCurrentDocumentOnlyNavigates
    (identity : StartupNavigationIdentity) :
    reconcileCurrentStartupObservation identity none =
      .navigateCanonical := by
  rfl

theorem untrustedCurrentMarkerIsRejected
    (blocker : StartupNavigationBlocker) :
    reconcileCurrentStartupObservation .untrusted (some blocker) =
      .rejected := by
  simp [reconcileCurrentStartupObservation]

theorem wrongRealmCurrentMarkerIsRejected
    (blocker : StartupNavigationBlocker) :
    reconcileCurrentStartupObservation .wrongRealm (some blocker) =
      .rejected := by
  simp [reconcileCurrentStartupObservation]

theorem wrongPathCurrentMarkerIsRejected
    (blocker : StartupNavigationBlocker) :
    reconcileCurrentStartupObservation .wrongPath (some blocker) =
      .rejected := by
  simp [reconcileCurrentStartupObservation]

theorem trustedCurrentMarkerIsAdopted
    (blocker : StartupNavigationBlocker) :
    reconcileCurrentStartupObservation .expected (some blocker) =
      .resolved (startupBlockerPresence blocker) := by
  simp [reconcileCurrentStartupObservation]

theorem canonicalWrongIdentityMarkerIsRejected
    (identity : StartupNavigationIdentity)
    (blocker : StartupNavigationBlocker)
    (wrong : identity ≠ .expected) :
    reconcileCanonicalStartupObservation identity (some blocker) true true =
      .rejected := by
  simp [reconcileCanonicalStartupObservation, wrong]

theorem canonicalRedirectMarkerDoesNotRequireListingQuery
    (blocker : StartupNavigationBlocker) :
    reconcileCanonicalStartupObservation .expected (some blocker) false true =
      .resolved (startupBlockerPresence blocker) := by
  simp [reconcileCanonicalStartupObservation]

theorem trustedCanonicalRedirectIsAdopted
    (blocker : StartupNavigationBlocker) :
    reconcileCanonicalStartupObservation .expected (some blocker) false false =
      .resolved (startupBlockerPresence blocker) := by
  simp [reconcileCanonicalStartupObservation]

theorem canonicalAbsenceRequiresValidatedRoute :
    reconcileCanonicalStartupObservation .expected none true true =
      .resolved .absent ∧
    reconcileCanonicalStartupObservation .expected none true false =
      .rejected ∧
    reconcileCanonicalStartupObservation .expected none false true =
      .rejected := by
  exact ⟨by simp [reconcileCanonicalStartupObservation],
    by simp [reconcileCanonicalStartupObservation],
    by simp [reconcileCanonicalStartupObservation]⟩

inductive DocumentReadiness where
  | loading
  | interactive
  | complete
  | unknown
  deriving DecidableEq, Repr

def readyEnough : DocumentReadiness → Bool
  | .interactive | .complete => true
  | .loading | .unknown => false

inductive ResponseOutcome where
  | load
  | abort
  | timeout
  | networkError
  deriving DecidableEq, Repr

structure ActionMonitor where
  sent : Bool
  sentCount : Nat
  completed : Bool
  status : Option Nat
  outcome : Option ResponseOutcome
  responseParseOk : Bool
  responseHasError : Bool
  responseHasReload : Bool
  responseHasLogin : Bool
  responseHasTextLog : Bool
  responseHasPaneCompletion : Bool
  logMutations : Nat
  deriving DecidableEq, Repr

structure BattleSnapshot where
  document : Nat
  battleNode : Option Nat
  readiness : DocumentReadiness
  round : Option Nat
  battlePresent : Bool
  logRevision : Option Nat
  completionRevision : Option Nat
  completionPresent : Bool
  battleCompletePresent : Bool
  nextFloorPresent : Bool
  ponychartPresent : Bool
  actionControls : Nat
  monitor : Option ActionMonitor := none
  deriving DecidableEq, Repr

def ponychartAppeared (before current : BattleSnapshot) : Bool :=
  current.ponychartPresent && !before.ponychartPresent

def battleCompletionAppeared (before current : BattleSnapshot) : Bool :=
  current.battleCompletePresent && !before.battleCompletePresent

def generationChanged (before current : BattleSnapshot) : Bool :=
  decide (current.document ≠ before.document) ||
    decide (current.battleNode ≠ before.battleNode)

def roundAdvanced (before current : BattleSnapshot) : Bool :=
  match before.round, current.round with
  | some beforeRound, some currentRound => decide (beforeRound < currentRound)
  | _, _ => false

def roundInitialized (before current : BattleSnapshot) : Bool :=
  before.nextFloorPresent && before.round.isNone && current.round.isSome

def actionableBattle (current : BattleSnapshot) : Bool :=
  current.battlePresent && current.logRevision.isSome &&
    !current.nextFloorPresent && decide (0 < current.actionControls)

def logRevisionChanged (before current : BattleSnapshot) : Bool :=
  decide (current.logRevision ≠ before.logRevision)

inductive TransitionEvidenceKind where
  | ponychartPresent
  | battleCompletionPresent
  | battleGenerationRoundAdvanced
  | battleRoundAdvanced
  | battleGenerationRoundInitialized
  | battleRoundInitialized
  deriving DecidableEq, Repr

/-!
`confirmedTransitionEvidence` mirrors `_confirmed_transition_evidence` after
the readiness correction: a changed document or battle node may be accepted at
`interactive` or `complete`, but never at `loading` or `unknown`. Same-document
AJAX progress is established by round/log evidence and does not depend on a
document load state.
-/
def confirmedTransitionEvidence
    (before current : BattleSnapshot) : Option TransitionEvidenceKind :=
  if ponychartAppeared before current then
    some .ponychartPresent
  else if battleCompletionAppeared before current then
    some .battleCompletionPresent
  else if roundAdvanced before current && actionableBattle current then
    if generationChanged before current then
      if readyEnough current.readiness then
        some .battleGenerationRoundAdvanced
      else
        none
    else
      some .battleRoundAdvanced
  else if roundInitialized before current && actionableBattle current then
    if generationChanged before current then
      if readyEnough current.readiness then
        some .battleGenerationRoundInitialized
      else
        none
    else if logRevisionChanged before current then
      some .battleRoundInitialized
    else
      none
  else
    none

/-!
The manager retains the last monitor associated with the submitted next-floor
action even if navigation removes it from the current document. DOM progress
may confirm the transition only when that retained monitor proves at most one
dispatch. An absent monitor is not evidence of duplication and remains allowed;
a present monitor must be exactly unsent/count-zero or sent/count-one.
-/
def transitionReceiptHasAtMostOneDispatch
    (retainedMonitor : Option ActionMonitor) : Bool :=
  match retainedMonitor with
  | none => true
  | some monitor =>
      (!monitor.sent && decide (monitor.sentCount = 0)) ||
        (monitor.sent && decide (monitor.sentCount = 1))

def acceptedTransitionEvidence
    (before current : BattleSnapshot)
    (retainedMonitor : Option ActionMonitor) : Option TransitionEvidenceKind :=
  if transitionReceiptHasAtMostOneDispatch retainedMonitor then
    confirmedTransitionEvidence before current
  else
    none

theorem acceptedTransitionRequiresAtMostOneDispatch
    (before current : BattleSnapshot)
    (retainedMonitor : Option ActionMonitor)
    (evidence : TransitionEvidenceKind)
    (accepted :
      acceptedTransitionEvidence before current retainedMonitor = some evidence) :
    transitionReceiptHasAtMostOneDispatch retainedMonitor = true ∧
      confirmedTransitionEvidence before current = some evidence := by
  cases receiptCase :
      transitionReceiptHasAtMostOneDispatch retainedMonitor <;>
    simp_all [acceptedTransitionEvidence]

theorem duplicateTransitionDispatchIsNeverAccepted
    (before current : BattleSnapshot)
    (retainedMonitor : Option ActionMonitor)
    (duplicate :
      transitionReceiptHasAtMostOneDispatch retainedMonitor = false) :
    acceptedTransitionEvidence before current retainedMonitor = none := by
  simp [acceptedTransitionEvidence, duplicate]

theorem interactiveGeneratedRoundAdvanceAccepted
    (before current : BattleSnapshot)
    (noPonychart : ponychartAppeared before current = false)
    (noCompletion : battleCompletionAppeared before current = false)
    (advanced : roundAdvanced before current = true)
    (actionable : actionableBattle current = true)
    (changed : generationChanged before current = true)
    (interactive : current.readiness = .interactive) :
    confirmedTransitionEvidence before current =
      some .battleGenerationRoundAdvanced := by
  simp [confirmedTransitionEvidence, noPonychart, noCompletion, advanced,
    actionable, changed, interactive, readyEnough]

theorem loadingGeneratedRoundAdvanceRejected
    (before current : BattleSnapshot)
    (noPonychart : ponychartAppeared before current = false)
    (noCompletion : battleCompletionAppeared before current = false)
    (advanced : roundAdvanced before current = true)
    (actionable : actionableBattle current = true)
    (changed : generationChanged before current = true)
    (loading : current.readiness = .loading) :
    confirmedTransitionEvidence before current = none := by
  simp [confirmedTransitionEvidence, noPonychart, noCompletion, advanced,
    actionable, changed, loading, readyEnough]

theorem interactiveGeneratedRoundInitializationAccepted
    (before current : BattleSnapshot)
    (noPonychart : ponychartAppeared before current = false)
    (noCompletion : battleCompletionAppeared before current = false)
    (notAdvanced : roundAdvanced before current = false)
    (initialized : roundInitialized before current = true)
    (actionable : actionableBattle current = true)
    (changed : generationChanged before current = true)
    (interactive : current.readiness = .interactive) :
    confirmedTransitionEvidence before current =
      some .battleGenerationRoundInitialized := by
  simp [confirmedTransitionEvidence, noPonychart, noCompletion, notAdvanced,
    initialized, actionable, changed, interactive, readyEnough]

theorem loadingGeneratedRoundInitializationRejected
    (before current : BattleSnapshot)
    (noPonychart : ponychartAppeared before current = false)
    (noCompletion : battleCompletionAppeared before current = false)
    (notAdvanced : roundAdvanced before current = false)
    (initialized : roundInitialized before current = true)
    (actionable : actionableBattle current = true)
    (changed : generationChanged before current = true)
    (loading : current.readiness = .loading) :
    confirmedTransitionEvidence before current = none := by
  simp [confirmedTransitionEvidence, noPonychart, noCompletion, notAdvanced,
    initialized, actionable, changed, loading, readyEnough]

/-!
This theorem is readiness-value independent: every changed-generation state
whose readiness is rejected by `readyEnough` remains unknown even if it has
otherwise positive round and actionable evidence. It covers both `loading` and
`unknown` without enumerating them in the theorem statement.
-/
theorem unreadyGeneratedPositiveRoundRejected
    (before current : BattleSnapshot)
    (noPonychart : ponychartAppeared before current = false)
    (noCompletion : battleCompletionAppeared before current = false)
    (positiveRound :
      (roundAdvanced before current = true ∨
        roundInitialized before current = true) ∧
      actionableBattle current = true)
    (changed : generationChanged before current = true)
    (unready : readyEnough current.readiness = false) :
    confirmedTransitionEvidence before current = none := by
  cases advancedCase : roundAdvanced before current <;>
    cases initializedCase : roundInitialized before current <;>
    cases actionableCase : actionableBattle current <;>
    simp_all [confirmedTransitionEvidence]

theorem ponychartIsExplicitTransitionEvidence
    (before current : BattleSnapshot)
    (appeared : ponychartAppeared before current = true) :
    confirmedTransitionEvidence before current = some .ponychartPresent := by
  simp [confirmedTransitionEvidence, appeared]

theorem completionIsExplicitTransitionEvidence
    (before current : BattleSnapshot)
    (noPonychart : ponychartAppeared before current = false)
    (appeared : battleCompletionAppeared before current = true) :
    confirmedTransitionEvidence before current =
      some .battleCompletionPresent := by
  simp [confirmedTransitionEvidence, noPonychart, appeared]

/-! A concrete encoding of the reported Round 21 → 22 incident. -/
def observedRound21 : BattleSnapshot where
  document := 21
  battleNode := some 21
  readiness := .complete
  round := some 21
  battlePresent := true
  logRevision := some 21
  completionRevision := some 21
  completionPresent := true
  battleCompletePresent := false
  nextFloorPresent := true
  ponychartPresent := false
  actionControls := 0

def observedRound22Interactive : BattleSnapshot where
  document := 22
  battleNode := some 22
  readiness := .interactive
  round := some 22
  battlePresent := true
  logRevision := some 22
  completionRevision := some 22
  completionPresent := false
  battleCompletePresent := false
  nextFloorPresent := false
  ponychartPresent := false
  actionControls := 3

def observedRound22Loading : BattleSnapshot :=
  { observedRound22Interactive with readiness := .loading }

def observedDuplicateTransitionMonitor : ActionMonitor where
  sent := true
  sentCount := 2
  completed := false
  status := none
  outcome := none
  responseParseOk := false
  responseHasError := false
  responseHasReload := false
  responseHasLogin := false
  responseHasTextLog := false
  responseHasPaneCompletion := false
  logMutations := 0

theorem observedRound21To22InteractiveIsAccepted :
    confirmedTransitionEvidence observedRound21 observedRound22Interactive =
      some .battleGenerationRoundAdvanced := by
  native_decide

theorem observedRound21To22LoadingIsRejected :
    confirmedTransitionEvidence observedRound21 observedRound22Loading = none := by
  native_decide

theorem observedRound21To22DuplicateDispatchIsRejected :
    acceptedTransitionEvidence observedRound21 observedRound22Interactive
      (some observedDuplicateTransitionMonitor) = none := by
  native_decide

/-!
This is the principal transition soundness theorem: every accepted transition
has an explicit challenge/completion marker, or both an advanced/initialized
round and an actionable battle snapshot. In particular, generation/readiness
alone can never establish success.
-/
theorem transitionEvidenceRequiresPositiveNextPhase
    (before current : BattleSnapshot)
    (evidence : TransitionEvidenceKind)
    (selected : confirmedTransitionEvidence before current = some evidence) :
    ponychartAppeared before current = true ∨
      battleCompletionAppeared before current = true ∨
      ((roundAdvanced before current = true ∨
          roundInitialized before current = true) ∧
        actionableBattle current = true) := by
  cases ponychartCase : ponychartAppeared before current <;>
    cases completionCase : battleCompletionAppeared before current <;>
    cases advancedCase : roundAdvanced before current <;>
    cases initializedCase : roundInitialized before current <;>
    cases actionableCase : actionableBattle current <;>
    simp_all [confirmedTransitionEvidence]

def normalActionResponse (monitor : ActionMonitor) : Bool :=
  monitor.sent && decide (monitor.sentCount = 1) && monitor.completed &&
    decide (monitor.status = some 200) &&
    decide (monitor.outcome = some .load) && monitor.responseParseOk &&
    !monitor.responseHasError && !monitor.responseHasReload &&
    !monitor.responseHasLogin

def sameDocument (before current : BattleSnapshot) : Bool :=
  decide (current.document = before.document)

def completionPaneChanged (before current : BattleSnapshot) : Bool :=
  decide (current.completionRevision ≠ before.completionRevision) ||
    (current.completionPresent && !before.completionPresent)

inductive ActionEvidenceKind where
  | xhrAckCombatLogMutation
  | xhrAckCombatLogRevision
  | xhrAckBattleCompletion
  | xhrAckRoundCompletion
  | xhrAckCompletionPane
  deriving DecidableEq, Repr

/-!
The action selector requires a receipt from the same document and a single,
parsed HTTP 200 `load` response without server error/reload/login markers.
-/
def confirmedActionEvidence
    (before current : BattleSnapshot) : Option ActionEvidenceKind :=
  match current.monitor with
  | none => none
  | some monitor =>
      if sameDocument before current && normalActionResponse monitor then
        if monitor.responseHasTextLog && decide (0 < monitor.logMutations) then
          some .xhrAckCombatLogMutation
        else if monitor.responseHasTextLog &&
            logRevisionChanged before current then
          some .xhrAckCombatLogRevision
        else if monitor.responseHasPaneCompletion &&
            completionPaneChanged before current then
          if current.battleCompletePresent then
            some .xhrAckBattleCompletion
          else if current.nextFloorPresent then
            some .xhrAckRoundCompletion
          else
            some .xhrAckCompletionPane
        else
          none
      else
        none

theorem actionEvidenceRequiresMatchedNormalReceipt
    (before current : BattleSnapshot)
    (evidence : ActionEvidenceKind)
    (selected : confirmedActionEvidence before current = some evidence) :
    sameDocument before current = true ∧
      ∃ monitor,
        current.monitor = some monitor ∧ normalActionResponse monitor = true := by
  cases monitorCase : current.monitor with
  | none =>
      simp [confirmedActionEvidence, monitorCase] at selected
  | some monitor =>
      cases sameCase : sameDocument before current <;>
        cases normalCase : normalActionResponse monitor <;>
        simp_all [confirmedActionEvidence]

theorem normalCombatLogMutationIsAccepted
    (before current : BattleSnapshot)
    (monitor : ActionMonitor)
    (observed : current.monitor = some monitor)
    (same : sameDocument before current = true)
    (normal : normalActionResponse monitor = true)
    (hasTextLog : monitor.responseHasTextLog = true)
    (mutated : 0 < monitor.logMutations) :
    confirmedActionEvidence before current =
      some .xhrAckCombatLogMutation := by
  simp [confirmedActionEvidence, observed, same, normal, hasTextLog, mutated]

theorem malformedReceiptIsNeverAccepted
    (before current : BattleSnapshot)
    (monitor : ActionMonitor)
    (observed : current.monitor = some monitor)
    (malformed : normalActionResponse monitor = false) :
    confirmedActionEvidence before current = none := by
  simp [confirmedActionEvidence, observed, malformed]

inductive BattleRealm where
  | persistent
  | isekai
  | outside
  deriving DecidableEq, Repr

def validExpectedRealm : BattleRealm → Bool
  | .persistent | .isekai => true
  | .outside => false

/-!
## Same-browser reconciliation of an ambiguous submitted action

The action manager freezes the evidence below before the exception leaves its
exactly-once submission boundary. Tokens model the immutable association
between the submitted action and any sanitized JavaScript-dialog observation.
The request-age flag distinguishes an XHR that has actually remained pending
for at least five seconds from a request sent only after a slow browser click.
Known pre/post document identities are required even though the post-click
document may still be the old one; the coordinator separately proves that the
accepted server state belongs to a new document.

The field types are part of the boundary: `actionKind` has no untyped fallback,
click/XHR flags are actual `Bool` values, and `xhrSentCount` is a `Nat`. Thus a
boolean cannot masquerade as send count `1`, matching the runtime's strict
Python type validation before this structure is constructed.
-/

inductive BattleActionKind where
  | turn
  | nextFloor
  deriving DecidableEq, Repr

inductive DialogCategory where
  | serverCommunicationFailed
  | sessionOrLogin
  | other
  deriving DecidableEq, Repr

structure ActionRecoveryEvidence where
  actionToken : Nat
  actionKind : BattleActionKind
  selectorPresent : Bool
  clickStarted : Bool
  xhrPendingAtLeastFiveSeconds : Bool
  preDocument : Option Nat
  postDocument : Option Nat
  dialogActionToken : Option Nat
  dialogCategory : Option DialogCategory
  xhrSent : Bool
  xhrSentCount : Nat
  xhrCompleted : Bool
  xhrStatus : Option Nat
  xhrOutcome : Option ResponseOutcome
  deriving DecidableEq, Repr

/-!
The runtime freezes recovery evidence from the same retained matching monitor
that gates next-floor transition acceptance. The transition kind and every XHR
field, including the five-second stalled-turn flag, are fixed below from that
boundary rather than trusted from the caller. A duplicate transition receipt
therefore cannot be hidden by injecting a turn-shaped count-one recovery
record.
-/
def freezeTransitionRecoveryEvidence
    (envelope : ActionRecoveryEvidence)
    (retainedMonitor : Option ActionMonitor) : ActionRecoveryEvidence :=
  match retainedMonitor with
  | none =>
      { envelope with
        actionKind := .nextFloor
        xhrPendingAtLeastFiveSeconds := false
        xhrSent := false
        xhrSentCount := 0
        xhrCompleted := false
        xhrStatus := none
        xhrOutcome := none }
  | some monitor =>
      { envelope with
        actionKind := .nextFloor
        xhrPendingAtLeastFiveSeconds := false
        xhrSent := monitor.sent
        xhrSentCount := monitor.sentCount
        xhrCompleted := monitor.completed
        xhrStatus := monitor.status
        xhrOutcome := monitor.outcome }

theorem frozenTransitionEvidenceUsesExactRetainedReceipt
    (envelope : ActionRecoveryEvidence)
    (monitor : ActionMonitor) :
    let frozen := freezeTransitionRecoveryEvidence envelope (some monitor)
    frozen.xhrSent = monitor.sent ∧
      frozen.xhrSentCount = monitor.sentCount ∧
      frozen.xhrCompleted = monitor.completed ∧
      frozen.xhrStatus = monitor.status ∧
      frozen.xhrOutcome = monitor.outcome := by
  simp [freezeTransitionRecoveryEvidence]

theorem frozenTransitionEvidenceForcesNextFloorEnvelope
    (envelope : ActionRecoveryEvidence)
    (monitor : Option ActionMonitor) :
    let frozen := freezeTransitionRecoveryEvidence envelope monitor
    frozen.actionKind = .nextFloor ∧
      frozen.xhrPendingAtLeastFiveSeconds = false := by
  cases monitor <;> simp [freezeTransitionRecoveryEvidence]

def exactStatusZeroErrorReceipt (evidence : ActionRecoveryEvidence) : Bool :=
  evidence.xhrCompleted && evidence.xhrSent &&
    decide (evidence.xhrSentCount = 1) &&
    decide (evidence.xhrStatus = some 0) &&
    decide (evidence.xhrOutcome = some .networkError)

def terminalReceiptUnavailable (evidence : ActionRecoveryEvidence) : Bool :=
  !evidence.xhrCompleted &&
    decide (evidence.xhrStatus = none) &&
    decide (evidence.xhrOutcome = none) &&
    ((!evidence.xhrSent && decide (evidence.xhrSentCount = 0)) ||
      (evidence.xhrSent && decide (evidence.xhrSentCount = 1)))

def exactPendingSingleXhr (evidence : ActionRecoveryEvidence) : Bool :=
  evidence.xhrSent && decide (evidence.xhrSentCount = 1) &&
    !evidence.xhrCompleted && decide (evidence.xhrStatus = none) &&
    decide (evidence.xhrOutcome = none)

def actionEvidenceEnvelopePresent (evidence : ActionRecoveryEvidence) : Bool :=
  decide (0 < evidence.actionToken) && evidence.selectorPresent &&
    evidence.clickStarted && evidence.preDocument.isSome &&
    evidence.postDocument.isSome

def actionDialogEvidenceBound (evidence : ActionRecoveryEvidence) : Bool :=
  actionEvidenceEnvelopePresent evidence &&
    decide (evidence.dialogActionToken = some evidence.actionToken) &&
    decide (evidence.dialogCategory = some .serverCommunicationFailed)

def matchesServerCommunicationFailure
    (evidence : ActionRecoveryEvidence) : Bool :=
  actionDialogEvidenceBound evidence &&
    (exactStatusZeroErrorReceipt evidence ||
      terminalReceiptUnavailable evidence)

def matchesStalledSingleXhr (evidence : ActionRecoveryEvidence) : Bool :=
  actionEvidenceEnvelopePresent evidence &&
    decide (evidence.actionKind = .turn) &&
    evidence.xhrPendingAtLeastFiveSeconds &&
    decide (evidence.preDocument = evidence.postDocument) &&
    decide (evidence.dialogActionToken = none) &&
    decide (evidence.dialogCategory = none) && exactPendingSingleXhr evidence

def matchesReloadRecoveryIncident (evidence : ActionRecoveryEvidence) : Bool :=
  matchesServerCommunicationFailure evidence ||
    matchesStalledSingleXhr evidence

theorem frozenTransitionEvidenceCannotMatchStalledSingleXhr
    (envelope : ActionRecoveryEvidence)
    (monitor : Option ActionMonitor) :
    matchesStalledSingleXhr
        (freezeTransitionRecoveryEvidence envelope monitor) = false := by
  cases monitor <;>
    simp [freezeTransitionRecoveryEvidence, matchesStalledSingleXhr]

/-!
A post-click Zendriver operation timeout leaves the protocol command alive, so
the runtime deliberately does not issue another document probe. At this model
boundary that fact is represented by an unobservable post-click document.
-/
theorem unobservablePostDocumentCannotMatchRecoveryIncident
    (evidence : ActionRecoveryEvidence)
    (unobservable : evidence.postDocument = none) :
    matchesReloadRecoveryIncident evidence = false := by
  simp [matchesReloadRecoveryIncident, matchesServerCommunicationFailure,
    matchesStalledSingleXhr, actionDialogEvidenceBound,
    actionEvidenceEnvelopePresent, unobservable]

inductive RecoveryPhase where
  | active
  | nextFloor
  | complete
  | challenge
  | ineligible
  deriving DecidableEq, Repr

/-!
PonyChart temporarily replaces the ordinary battle container. The runtime
therefore accepts the visible challenge before falling through to an
ineligible missing-container state, while complete and next-floor controls on
an existing battle container retain their higher priority.
-/
def classifyRecoverySurface
    (battlePresent finishControlPresent nextFloorControlPresent
      ponychartPresent activeLogRevisionPresent
      activeActionControlsPresent : Bool) :
    RecoveryPhase :=
  if battlePresent && finishControlPresent then
    .complete
  else if battlePresent && nextFloorControlPresent then
    .nextFloor
  else if ponychartPresent then
    .challenge
  else if battlePresent && activeLogRevisionPresent &&
      activeActionControlsPresent then
    .active
  else
    .ineligible

theorem ponychartWithoutBattleContainerIsChallenge :
    classifyRecoverySurface false false false true false false = .challenge := by
  native_decide

theorem completionControlHasPriorityOverPonychart :
    classifyRecoverySurface true true false true true true = .complete := by
  native_decide

theorem nextFloorControlHasPriorityOverPonychart :
    classifyRecoverySurface true false true true true true = .nextFloor := by
  native_decide

theorem activeRecoveryRequiresLogAndActionControlMarkers :
    classifyRecoverySurface true false false false true true = .active ∧
      classifyRecoverySurface true false false false false true = .ineligible ∧
      classifyRecoverySurface true false false false true false = .ineligible := by
  native_decide

structure RecoveryProbe where
  document : Nat
  realm : BattleRealm
  readiness : DocumentReadiness
  phase : RecoveryPhase
  activeMarkersPresent : Bool
  signature : Nat
  deriving DecidableEq, Repr

/-!
`signature` abstracts the runtime tuple containing document, realm, readiness,
phase, log/completion revisions, and action-control count. The explicit field
equalities below keep the proof boundary readable and prevent an accidental
weakening if the concrete fingerprint representation changes.
-/
def sameRecoveryState (left right : RecoveryProbe) : Bool :=
  decide (left.document = right.document) &&
    decide (left.realm = right.realm) &&
    decide (left.readiness = right.readiness) &&
    decide (left.phase = right.phase) &&
    decide (left.activeMarkersPresent = right.activeMarkersPresent) &&
    decide (left.signature = right.signature)

def eligibleRecoveryPhase
    (phase : RecoveryPhase) (activeMarkersPresent activeParsedAlive : Bool) : Bool :=
  match phase with
  | .active => activeMarkersPresent && activeParsedAlive
  | .nextFloor | .complete | .challenge => true
  | .ineligible => false

structure RecoveryObservation where
  first : RecoveryProbe
  second : RecoveryProbe
  final : RecoveryProbe
  stableReadCount : Nat
  activeParsedAlive : Bool
  cleanupDocument : Nat
  manualReloadCount : Nat
  deriving DecidableEq, Repr

def stableSameRealmNewDocument
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation) : Bool :=
  match evidence.preDocument with
  | none => false
  | some preDocument =>
      validExpectedRealm expectedRealm &&
        decide (observed.first.document ≠ preDocument) &&
        decide (observed.first.realm = expectedRealm) &&
        readyEnough observed.first.readiness &&
        decide (2 ≤ observed.stableReadCount) &&
        sameRecoveryState observed.first observed.second &&
        sameRecoveryState observed.second observed.final &&
        eligibleRecoveryPhase observed.first.phase
          observed.first.activeMarkersPresent observed.activeParsedAlive &&
        decide (observed.cleanupDocument = observed.first.document) &&
        decide (observed.manualReloadCount ≤ 1)

structure RecoveryBudget where
  awaitingAuthoritativeReceipt : Bool
  deriving DecidableEq, Repr

def recoveryBudgetAvailable (budget : RecoveryBudget) : Bool :=
  !budget.awaitingAuthoritativeReceipt

inductive RecoveryBudgetEvent where
  | recoveryAccepted
  | confirmedActed
  | confirmedNextFloor
  | noAuthoritativeReceipt
  deriving DecidableEq, Repr

def updateRecoveryBudget
    (budget : RecoveryBudget) (event : RecoveryBudgetEvent) : RecoveryBudget :=
  match event with
  | .recoveryAccepted => ⟨true⟩
  | .confirmedActed | .confirmedNextFloor => ⟨false⟩
  | .noAuthoritativeReceipt => budget

inductive UnknownActionResolution where
  | recoverToPrepare
  | recoveryExhausted
  | interrupt
  deriving DecidableEq, Repr

def recoveryEligible
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget) : Bool :=
  validExpectedRealm expectedRealm &&
    matchesReloadRecoveryIncident evidence &&
    stableSameRealmNewDocument expectedRealm evidence observed &&
    recoveryBudgetAvailable budget

def classifyUnknownAction
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget) : UnknownActionResolution :=
  if !validExpectedRealm expectedRealm then
    .interrupt
  else if !recoveryBudgetAvailable budget then
    .recoveryExhausted
  else if !matchesReloadRecoveryIncident evidence then
    .interrupt
  else if stableSameRealmNewDocument expectedRealm evidence observed then
    .recoverToPrepare
  else
    .recoveryExhausted

def budgetAfterResolution
    (before : RecoveryBudget)
    (resolution : UnknownActionResolution) : RecoveryBudget :=
  match resolution with
  | .recoverToPrepare => updateRecoveryBudget before .recoveryAccepted
  | .recoveryExhausted | .interrupt => before

structure UnknownActionDecision where
  resolution : UnknownActionResolution
  budgetAfter : RecoveryBudget
  deriving DecidableEq, Repr

def decideUnknownAction
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget) : UnknownActionDecision :=
  let resolution := classifyUnknownAction expectedRealm evidence observed budget
  ⟨resolution, budgetAfterResolution budget resolution⟩

def resolveUnknownAction
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget) : UnknownActionResolution :=
  (decideUnknownAction expectedRealm evidence observed budget).resolution

inductive RecoveryNextStep where
  | prepareAndRunStrategy
  | replayCachedAction
  | stop
  deriving DecidableEq, Repr

def recoveryNextStep : UnknownActionResolution → RecoveryNextStep
  | .recoverToPrepare => .prepareAndRunStrategy
  | .recoveryExhausted | .interrupt => .stop

/-!
HVBattle exposes recovery exhaustion as a distinct typed interruption with its
own stable diagnostic code. This model deliberately stops at that package
boundary: opening another browser, restarting a worker, and bounding those
restarts are policies supplied by the calling application.
-/
inductive UnknownActionInterruptionKind where
  | outcomeUnknown
  | recoveryExhausted
  deriving DecidableEq, Repr

def interruptionKindForResolution :
    UnknownActionResolution → Option UnknownActionInterruptionKind
  | .recoverToPrepare => none
  | .interrupt => some .outcomeUnknown
  | .recoveryExhausted => some .recoveryExhausted

def diagnosticCodeForResolution : UnknownActionResolution → Option String
  | .recoverToPrepare => none
  | .interrupt => some "battle.action-outcome-unknown"
  | .recoveryExhausted => some "battle.action-recovery-exhausted"

theorem recoveryExhaustionRetainsTypedDiagnostic :
    interruptionKindForResolution .recoveryExhausted =
        some .recoveryExhausted ∧
      diagnosticCodeForResolution .recoveryExhausted =
        some "battle.action-recovery-exhausted" := by
  native_decide

theorem ordinaryUnknownRetainsTypedDiagnostic :
    interruptionKindForResolution .interrupt = some .outcomeUnknown ∧
      diagnosticCodeForResolution .interrupt =
        some "battle.action-outcome-unknown" := by
  native_decide

theorem acceptedRecoveryHasNoInterruptionDiagnostic :
    interruptionKindForResolution .recoverToPrepare = none ∧
      diagnosticCodeForResolution .recoverToPrepare = none := by
  native_decide

theorem recoveryRequiresEveryGuard
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (recovered :
      resolveUnknownAction expectedRealm evidence observed budget =
        .recoverToPrepare) :
    validExpectedRealm expectedRealm = true ∧
      matchesReloadRecoveryIncident evidence = true ∧
      stableSameRealmNewDocument expectedRealm evidence observed = true ∧
      recoveryBudgetAvailable budget = true := by
  cases validCase : validExpectedRealm expectedRealm <;>
    cases availableCase : recoveryBudgetAvailable budget <;>
    cases incidentCase : matchesReloadRecoveryIncident evidence <;>
    cases stableCase :
      stableSameRealmNewDocument expectedRealm evidence observed <;>
    simp_all [resolveUnknownAction, decideUnknownAction, classifyUnknownAction]

theorem recoveryEligibleExactlyWhenResolutionReturnsToPrepare
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget) :
    recoveryEligible expectedRealm evidence observed budget = true ↔
      resolveUnknownAction expectedRealm evidence observed budget =
        .recoverToPrepare := by
  cases validCase : validExpectedRealm expectedRealm <;>
    cases availableCase : recoveryBudgetAvailable budget <;>
    cases incidentCase : matchesReloadRecoveryIncident evidence <;>
    cases stableCase :
      stableSameRealmNewDocument expectedRealm evidence observed <;>
    simp_all [recoveryEligible, resolveUnknownAction, decideUnknownAction,
      classifyUnknownAction]

theorem allRecoveryGuardsProduceFreshPrepare
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (validRealm : validExpectedRealm expectedRealm = true)
    (incident : matchesReloadRecoveryIncident evidence = true)
    (stable :
      stableSameRealmNewDocument expectedRealm evidence observed = true)
    (available : recoveryBudgetAvailable budget = true) :
    resolveUnknownAction expectedRealm evidence observed budget =
      .recoverToPrepare := by
  simp [resolveUnknownAction, decideUnknownAction, classifyUnknownAction,
    validRealm, incident, stable, available]

theorem unmatchedUnknownInterrupts
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (validRealm : validExpectedRealm expectedRealm = true)
    (available : recoveryBudgetAvailable budget = true)
    (unmatched : matchesReloadRecoveryIncident evidence = false) :
    resolveUnknownAction expectedRealm evidence observed budget = .interrupt := by
  simp [resolveUnknownAction, decideUnknownAction, classifyUnknownAction,
    validRealm, available, unmatched]

theorem postClickLiveTimeoutWithAvailableBudgetIsOrdinaryInterrupt
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (validRealm : validExpectedRealm expectedRealm = true)
    (available : recoveryBudgetAvailable budget = true)
    (unobservable : evidence.postDocument = none) :
    resolveUnknownAction expectedRealm evidence observed budget = .interrupt := by
  apply unmatchedUnknownInterrupts expectedRealm evidence observed budget
    validRealm available
  exact unobservablePostDocumentCannotMatchRecoveryIncident evidence unobservable

theorem failedMatchedRecoveryIsTypedExhaustion
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (validRealm : validExpectedRealm expectedRealm = true)
    (available : recoveryBudgetAvailable budget = true)
    (incident : matchesReloadRecoveryIncident evidence = true)
    (unstable :
      stableSameRealmNewDocument expectedRealm evidence observed = false) :
    resolveUnknownAction expectedRealm evidence observed budget =
      .recoveryExhausted := by
  simp [resolveUnknownAction, decideUnknownAction, classifyUnknownAction,
    validRealm, available, incident, unstable]

theorem outsideExpectedRealmInterrupts
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget) :
    resolveUnknownAction .outside evidence observed budget = .interrupt := by
  simp [resolveUnknownAction, decideUnknownAction, classifyUnknownAction,
    validExpectedRealm]

theorem recoveredUnknownNeverReplaysCachedAction
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (recovered :
      resolveUnknownAction expectedRealm evidence observed budget =
        .recoverToPrepare) :
    recoveryNextStep
        (resolveUnknownAction expectedRealm evidence observed budget) =
        .prepareAndRunStrategy ∧
      recoveryNextStep
        (resolveUnknownAction expectedRealm evidence observed budget) ≠
        .replayCachedAction := by
  simp [recovered, recoveryNextStep]

theorem recoveredUnknownConsumesRecoveryBudget
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (recovered :
      resolveUnknownAction expectedRealm evidence observed budget =
        .recoverToPrepare) :
    (decideUnknownAction expectedRealm evidence observed budget).budgetAfter =
      ⟨true⟩ := by
  have classified :
      classifyUnknownAction expectedRealm evidence observed budget =
        .recoverToPrepare := by
    simpa [resolveUnknownAction, decideUnknownAction] using recovered
  simp [decideUnknownAction, classified, budgetAfterResolution,
    updateRecoveryBudget]

theorem secondConsecutiveUnknownIsTypedExhaustion
    (expectedRealm : BattleRealm)
    (firstEvidence nextEvidence : ActionRecoveryEvidence)
    (firstObservation nextObservation : RecoveryObservation)
    (budget : RecoveryBudget)
    (firstRecovered :
      resolveUnknownAction expectedRealm firstEvidence firstObservation budget =
        .recoverToPrepare) :
    resolveUnknownAction expectedRealm nextEvidence nextObservation
        (decideUnknownAction expectedRealm firstEvidence firstObservation
          budget).budgetAfter = .recoveryExhausted := by
  have guards := recoveryRequiresEveryGuard expectedRealm firstEvidence
    firstObservation budget firstRecovered
  have consumed := recoveredUnknownConsumesRecoveryBudget expectedRealm
    firstEvidence firstObservation budget firstRecovered
  rcases guards with ⟨validRealm, _, _, _⟩
  rw [consumed]
  simp [resolveUnknownAction, decideUnknownAction,
    classifyUnknownAction, validRealm, recoveryBudgetAvailable]

theorem confirmedActedRestoresRecoveryBudget (budget : RecoveryBudget) :
    recoveryBudgetAvailable
      (updateRecoveryBudget budget .confirmedActed) = true := by
  simp [updateRecoveryBudget, recoveryBudgetAvailable]

theorem confirmedNextFloorRestoresRecoveryBudget (budget : RecoveryBudget) :
    recoveryBudgetAvailable
      (updateRecoveryBudget budget .confirmedNextFloor) = true := by
  simp [updateRecoveryBudget, recoveryBudgetAvailable]

theorem acceptedRecoveryUsesAtMostOneManualReload
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (recovered :
      resolveUnknownAction expectedRealm evidence observed budget =
        .recoverToPrepare) :
    observed.manualReloadCount ≤ 1 := by
  have guards := recoveryRequiresEveryGuard expectedRealm evidence observed budget
    recovered
  rcases guards with ⟨_, _, stable, _⟩
  simp [stableSameRealmNewDocument] at stable
  split at stable <;> simp_all

structure AcceptedRecoveryCleanup where
  pageActionStateCleared : Bool
  dialogTrackerCleared : Bool
  sessionRoundCacheCleared : Bool
  sessionCompletionCacheCleared : Bool
  parserStoreReset : Bool
  cachedSubmittedAction : Option Nat
  deriving DecidableEq, Repr

def acceptedRecoveryCleanup (phase : RecoveryPhase) : AcceptedRecoveryCleanup :=
  { pageActionStateCleared := true
    dialogTrackerCleared := true
    sessionRoundCacheCleared := true
    sessionCompletionCacheCleared := true
    parserStoreReset := decide (phase ≠ .complete)
    cachedSubmittedAction := none }

def cleanupAfterResolution
    (observed : RecoveryObservation)
    (resolution : UnknownActionResolution) : Option AcceptedRecoveryCleanup :=
  match resolution with
  | .recoverToPrepare => some (acceptedRecoveryCleanup observed.first.phase)
  | .recoveryExhausted | .interrupt => none

theorem acceptedRecoveryClearsSubmittedActionAndSessionCaches
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (recovered :
      resolveUnknownAction expectedRealm evidence observed budget =
        .recoverToPrepare) :
    cleanupAfterResolution observed
        (resolveUnknownAction expectedRealm evidence observed budget) =
      some (acceptedRecoveryCleanup observed.first.phase) ∧
      (acceptedRecoveryCleanup observed.first.phase).pageActionStateCleared = true ∧
      (acceptedRecoveryCleanup observed.first.phase).dialogTrackerCleared = true ∧
      (acceptedRecoveryCleanup observed.first.phase).sessionRoundCacheCleared = true ∧
      (acceptedRecoveryCleanup observed.first.phase).sessionCompletionCacheCleared = true ∧
      (acceptedRecoveryCleanup observed.first.phase).cachedSubmittedAction = none := by
  simp [recovered, cleanupAfterResolution, acceptedRecoveryCleanup]

theorem acceptedNonCompleteRecoveryResetsParserStore
    (phase : RecoveryPhase)
    (notComplete : phase ≠ .complete) :
    (acceptedRecoveryCleanup phase).parserStoreReset = true := by
  simp [acceptedRecoveryCleanup, notComplete]

theorem acceptedCompleteRecoveryPreservesParserStore :
    (acceptedRecoveryCleanup .complete).parserStoreReset = false := by
  native_decide

def observedStatusZeroEvidence : ActionRecoveryEvidence where
  actionToken := 7
  actionKind := .turn
  selectorPresent := true
  clickStarted := true
  xhrPendingAtLeastFiveSeconds := false
  preDocument := some 133
  postDocument := some 134
  dialogActionToken := some 7
  dialogCategory := some .serverCommunicationFailed
  xhrSent := true
  xhrSentCount := 1
  xhrCompleted := true
  xhrStatus := some 0
  xhrOutcome := some .networkError

def observedReceiptUnavailableEvidence : ActionRecoveryEvidence :=
  { observedStatusZeroEvidence with
    xhrSent := false
    xhrSentCount := 0
    xhrCompleted := false
    xhrStatus := none
    xhrOutcome := none }

def observedUnboundUnknownEvidence : ActionRecoveryEvidence :=
  { observedReceiptUnavailableEvidence with
    dialogActionToken := none
    dialogCategory := none }

def observedStalledSingleXhrEvidence : ActionRecoveryEvidence :=
  { observedUnboundUnknownEvidence with
    xhrPendingAtLeastFiveSeconds := true
    postDocument := some 133
    xhrSent := true
    xhrSentCount := 1
    xhrCompleted := false
    xhrStatus := none
    xhrOutcome := none }

def observedRecoveredProbe : RecoveryProbe where
  document := 134
  realm := .persistent
  readiness := .complete
  phase := .active
  activeMarkersPresent := true
  signature := 134001

def observedStableRecovery : RecoveryObservation where
  first := observedRecoveredProbe
  second := observedRecoveredProbe
  final := observedRecoveredProbe
  stableReadCount := 2
  activeParsedAlive := true
  cleanupDocument := 134
  manualReloadCount := 0

def availableRecoveryBudget : RecoveryBudget := ⟨false⟩

def consumedRecoveryBudget : RecoveryBudget := ⟨true⟩

theorem observedStatusZeroIncidentMatchesRecoveryBoundary :
    matchesReloadRecoveryIncident observedStatusZeroEvidence = true := by
  native_decide

theorem observedUnavailableReceiptMatchesRecoveryBoundary :
    matchesReloadRecoveryIncident observedReceiptUnavailableEvidence = true := by
  native_decide

theorem duplicateIncompleteReceiptDoesNotMatchRecoveryBoundary :
    matchesReloadRecoveryIncident
      { observedReceiptUnavailableEvidence with
        xhrSent := true
        xhrSentCount := 2 } = false := by
  native_decide

theorem staleIncompleteReceiptMetadataDoesNotMatchRecoveryBoundary :
    matchesReloadRecoveryIncident
      { observedReceiptUnavailableEvidence with
        xhrStatus := some 0
        xhrOutcome := some .networkError } = false := by
  native_decide

theorem observedUnboundUnknownDoesNotMatchRecoveryBoundary :
    matchesReloadRecoveryIncident observedUnboundUnknownEvidence = false := by
  native_decide

theorem observedStalledSingleXhrMatchesRecoveryBoundary :
    matchesStalledSingleXhr observedStalledSingleXhrEvidence = true ∧
      matchesReloadRecoveryIncident observedStalledSingleXhrEvidence = true := by
  native_decide

theorem stalledSingleXhrRejectsEveryWeakenedGuard :
    matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with actionToken := 0 } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with selectorPresent := false } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with clickStarted := false } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with
          xhrPendingAtLeastFiveSeconds := false } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with actionKind := .nextFloor } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with preDocument := none } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with postDocument := none } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with postDocument := some 134 } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with
          dialogActionToken := some 7 } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with
          xhrSent := false
          xhrSentCount := 0 } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with
          xhrSent := false
          xhrSentCount := 1 } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with xhrSentCount := 0 } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with xhrSentCount := 2 } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with xhrCompleted := true } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with xhrStatus := some 0 } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with
          xhrOutcome := some .networkError } = false ∧
      matchesStalledSingleXhr
        { observedStalledSingleXhrEvidence with
          dialogCategory := some .other } = false := by
  native_decide

theorem observedDuplicateRetainedMonitorCannotUseInjectedCountOneEnvelope :
    resolveUnknownAction .persistent
      (freezeTransitionRecoveryEvidence observedStatusZeroEvidence
        (some observedDuplicateTransitionMonitor))
      observedStableRecovery availableRecoveryBudget = .interrupt := by
  native_decide

theorem observedDuplicateRetainedMonitorWithConsumedBudgetIsTypedExhaustion :
    resolveUnknownAction .persistent
      (freezeTransitionRecoveryEvidence observedStatusZeroEvidence
        (some observedDuplicateTransitionMonitor))
      observedStableRecovery consumedRecoveryBudget = .recoveryExhausted := by
  native_decide

theorem observedStableIncidentRecoversToFreshPrepare :
    resolveUnknownAction .persistent observedStatusZeroEvidence
      observedStableRecovery availableRecoveryBudget = .recoverToPrepare := by
  native_decide

theorem observedStableStalledXhrRecoversToFreshPrepareWithoutReplay :
    resolveUnknownAction .persistent observedStalledSingleXhrEvidence
        observedStableRecovery availableRecoveryBudget = .recoverToPrepare ∧
      recoveryNextStep
          (resolveUnknownAction .persistent observedStalledSingleXhrEvidence
            observedStableRecovery availableRecoveryBudget) =
        .prepareAndRunStrategy ∧
      recoveryNextStep
          (resolveUnknownAction .persistent observedStalledSingleXhrEvidence
            observedStableRecovery availableRecoveryBudget) ≠
        .replayCachedAction := by
  native_decide

theorem observedSecondUnboundUnknownAfterRecoveryIsTypedExhaustion :
    resolveUnknownAction .persistent observedUnboundUnknownEvidence
      observedStableRecovery
      (decideUnknownAction .persistent observedStatusZeroEvidence
        observedStableRecovery availableRecoveryBudget).budgetAfter =
      .recoveryExhausted := by
  native_decide

theorem observedWrongRealmIncidentIsTypedExhaustion :
    resolveUnknownAction .isekai observedStatusZeroEvidence
      observedStableRecovery availableRecoveryBudget = .recoveryExhausted := by
  native_decide

theorem observedUnparsedActiveIncidentIsTypedExhaustion :
    resolveUnknownAction .persistent observedStatusZeroEvidence
      { observedStableRecovery with activeParsedAlive := false }
      availableRecoveryBudget = .recoveryExhausted := by
  native_decide

theorem matchedIncidentRequiresBoundEvidenceAndTerminalShape
    (evidence : ActionRecoveryEvidence)
    (matched : matchesServerCommunicationFailure evidence = true) :
    actionDialogEvidenceBound evidence = true ∧
      (exactStatusZeroErrorReceipt evidence = true ∨
        terminalReceiptUnavailable evidence = true) := by
  simpa [matchesServerCommunicationFailure] using matched

theorem matchedIncidentBindsExactActionAndDialogTokens
    (evidence : ActionRecoveryEvidence)
    (matched : matchesServerCommunicationFailure evidence = true) :
    0 < evidence.actionToken ∧
      evidence.dialogActionToken = some evidence.actionToken ∧
      evidence.dialogCategory = some .serverCommunicationFailed := by
  have bound := (matchedIncidentRequiresBoundEvidenceAndTerminalShape
    evidence matched).1
  simp [actionDialogEvidenceBound, actionEvidenceEnvelopePresent] at bound
  rcases bound with ⟨throughToken, exactCategory⟩
  rcases throughToken with ⟨throughPostDocument, exactToken⟩
  rcases throughPostDocument with ⟨throughPreDocument, _⟩
  rcases throughPreDocument with ⟨throughClick, _⟩
  rcases throughClick with ⟨positiveAndSelector, _⟩
  exact ⟨positiveAndSelector.1, exactToken, exactCategory⟩

theorem observedUnstableSignatureIsTypedExhaustion :
    resolveUnknownAction .persistent observedStatusZeroEvidence
      { observedStableRecovery with
        second := { observedRecoveredProbe with signature := 999 } }
      availableRecoveryBudget = .recoveryExhausted := by
  native_decide

theorem observedSameDocumentIsTypedExhaustion :
    resolveUnknownAction .persistent observedStatusZeroEvidence
      { observedStableRecovery with
        first := { observedRecoveredProbe with document := 133 }
        second := { observedRecoveredProbe with document := 133 }
        final := { observedRecoveredProbe with document := 133 }
        cleanupDocument := 133 }
      availableRecoveryBudget = .recoveryExhausted := by
  native_decide

theorem observedUnreadyDocumentIsTypedExhaustion :
    resolveUnknownAction .persistent observedStatusZeroEvidence
      { observedStableRecovery with
        first := { observedRecoveredProbe with readiness := .loading }
        second := { observedRecoveredProbe with readiness := .loading }
        final := { observedRecoveredProbe with readiness := .loading } }
      availableRecoveryBudget = .recoveryExhausted := by
  native_decide

theorem observedOutsideExpectedRealmInterrupts :
    resolveUnknownAction .outside observedStatusZeroEvidence
      { observedStableRecovery with
        first := { observedRecoveredProbe with realm := .outside }
        second := { observedRecoveredProbe with realm := .outside }
        final := { observedRecoveredProbe with realm := .outside } }
      availableRecoveryBudget = .interrupt := by
  native_decide

structure CompletionExitSnapshot where
  document : Nat
  realm : BattleRealm
  readiness : DocumentReadiness
  battlePresent : Bool
  finishControlPresent : Bool
  nextFloorPresent : Bool
  ponychartPresent : Bool
  deriving DecidableEq, Repr

def completionControlReady
    (expectedRealm : BattleRealm) (current : CompletionExitSnapshot) : Bool :=
  decide (current.realm = expectedRealm) && readyEnough current.readiness &&
    current.battlePresent && current.finishControlPresent

def positiveCompletionExitEvidence
    (expectedRealm : BattleRealm)
    (before current : CompletionExitSnapshot) : Bool :=
  decide (current.document ≠ before.document) &&
    decide (current.realm = expectedRealm) && readyEnough current.readiness &&
    !current.battlePresent && !current.finishControlPresent &&
    !current.nextFloorPresent && !current.ponychartPresent

structure CompletionAckObservation where
  before : CompletionExitSnapshot
  after : CompletionExitSnapshot
  selectorResolved : Bool
  selectedDocumentStable : Bool
  deriving DecidableEq, Repr

def completionAckClickCount
    (expectedRealm : BattleRealm) (observed : CompletionAckObservation) : Nat :=
  if completionControlReady expectedRealm observed.before &&
      observed.selectorResolved && observed.selectedDocumentStable then 1 else 0

inductive CompletionAckOutcome where
  | confirmed
  | outcomeUnknown
  deriving DecidableEq, Repr

def completionAckOutcome
    (expectedRealm : BattleRealm)
    (observed : CompletionAckObservation) : CompletionAckOutcome :=
  if completionControlReady expectedRealm observed.before &&
      positiveCompletionExitEvidence expectedRealm
        observed.before observed.after then
    .confirmed
  else
    .outcomeUnknown

theorem completionAckClicksAtMostOnce
    (expectedRealm : BattleRealm) (observed : CompletionAckObservation) :
    completionAckClickCount expectedRealm observed ≤ 1 := by
  unfold completionAckClickCount
  split <;> simp

theorem confirmedCompletionAckRequiresPositiveNewDocumentExit
    (expectedRealm : BattleRealm) (observed : CompletionAckObservation)
    (confirmed : completionAckOutcome expectedRealm observed = .confirmed) :
    positiveCompletionExitEvidence expectedRealm
      observed.before observed.after = true := by
  cases controlCase : completionControlReady expectedRealm observed.before <;>
    cases exitCase : positiveCompletionExitEvidence expectedRealm
      observed.before observed.after <;>
    simp_all [completionAckOutcome]

theorem sameDocumentCannotConfirmCompletionExit
    (expectedRealm : BattleRealm)
    (before current : CompletionExitSnapshot)
    (sameDocument : current.document = before.document) :
    positiveCompletionExitEvidence expectedRealm before current = false := by
  simp [positiveCompletionExitEvidence, sameDocument]

inductive LogSeverity where
  | info
  | error
  deriving DecidableEq, Repr

inductive LogCode where
  | transitionConfirmed
  | transitionOutcomeUnknown
  | actionRecoveryAccepted
  | runnerCompletionUnconfirmed
  | completionAckConfirmed
  | completionAckOutcomeUnknown
  | runnerCompletionAckUnconfirmed
  | driverException
  | applicationBattleInterrupted
  | applicationPostBattleFailure
  deriving DecidableEq, Repr

structure LogRecord where
  severity : LogSeverity
  code : LogCode
  deriving DecidableEq, Repr

def infoRecord (code : LogCode) : LogRecord := ⟨.info, code⟩
def errorRecord (code : LogCode) : LogRecord := ⟨.error, code⟩

inductive TransitionManagerOutcome where
  | confirmed (evidence : TransitionEvidenceKind)
  | battleActionOutcomeUnknown
  deriving DecidableEq, Repr

inductive RunnerOutcome where
  | continueBattle
  | battleInterrupted
  | battleRecoveryExhausted
  deriving DecidableEq, Repr

inductive ApplicationOutcome where
  | running
  | exited (code : Nat)
  deriving DecidableEq, Repr

structure Audited (Outcome : Type) where
  outcome : Outcome
  records : List LogRecord
  deriving DecidableEq, Repr

def auditTransitionSelection
    (selected : Option TransitionEvidenceKind) : Audited TransitionManagerOutcome :=
  match selected with
  | some evidence =>
      ⟨.confirmed evidence, [.transitionConfirmed |> infoRecord]⟩
  | none =>
      ⟨.battleActionOutcomeUnknown, [.transitionOutcomeUnknown |> errorRecord]⟩

def mapUnrecoveredManagerOutcomeToRunner
    (manager : Audited TransitionManagerOutcome) : Audited RunnerOutcome :=
  match manager.outcome with
  | .confirmed _ => ⟨.continueBattle, manager.records⟩
  | .battleActionOutcomeUnknown =>
      ⟨.battleInterrupted,
        manager.records ++ [.runnerCompletionUnconfirmed |> errorRecord]⟩

/-!
This is the coordinator-aware runner boundary. A successful decision continues
only by returning to turn preparation; every rejected or exhausted decision is
fed into the existing fail-closed audited interruption path.
-/
def runUnknownActionRecovery
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget) : Audited RunnerOutcome :=
  match resolveUnknownAction expectedRealm evidence observed budget with
  | .recoverToPrepare =>
      ⟨.continueBattle, [.actionRecoveryAccepted |> infoRecord]⟩
  | .recoveryExhausted =>
      ⟨.battleRecoveryExhausted,
        [.transitionOutcomeUnknown |> errorRecord,
          .runnerCompletionUnconfirmed |> errorRecord]⟩
  | .interrupt =>
      mapUnrecoveredManagerOutcomeToRunner
        ⟨.battleActionOutcomeUnknown,
          [.transitionOutcomeUnknown |> errorRecord]⟩

def runTransition
    (before current : BattleSnapshot)
    (retainedTransitionMonitor : Option ActionMonitor)
    (expectedRealm : BattleRealm)
    (recoveryEvidence : ActionRecoveryEvidence)
    (recoveryObservation : RecoveryObservation)
    (budget : RecoveryBudget) : Audited RunnerOutcome :=
  match acceptedTransitionEvidence before current retainedTransitionMonitor with
  | some evidence =>
      mapUnrecoveredManagerOutcomeToRunner
        (auditTransitionSelection (some evidence))
  | none =>
      runUnknownActionRecovery expectedRealm
        (freezeTransitionRecoveryEvidence recoveryEvidence
          retainedTransitionMonitor)
        recoveryObservation budget

theorem runTransitionKnownDuplicateCannotTakeConfirmedBranch
    (before current : BattleSnapshot)
    (retainedTransitionMonitor : Option ActionMonitor)
    (expectedRealm : BattleRealm)
    (recoveryEvidence : ActionRecoveryEvidence)
    (recoveryObservation : RecoveryObservation)
    (budget : RecoveryBudget)
    (duplicate :
      transitionReceiptHasAtMostOneDispatch retainedTransitionMonitor = false) :
    runTransition before current retainedTransitionMonitor expectedRealm
        recoveryEvidence recoveryObservation budget =
      runUnknownActionRecovery expectedRealm
        (freezeTransitionRecoveryEvidence recoveryEvidence
          retainedTransitionMonitor)
        recoveryObservation budget := by
  simp [runTransition, acceptedTransitionEvidence, duplicate]

theorem observedDuplicateAdvancedTransitionCannotContinue :
    (runTransition observedRound21 observedRound22Interactive
      (some observedDuplicateTransitionMonitor) .persistent
      observedStatusZeroEvidence observedStableRecovery
      availableRecoveryBudget).outcome = .battleInterrupted := by
  native_decide

def runCompletionAck
    (expectedRealm : BattleRealm)
    (observed : CompletionAckObservation) : Audited RunnerOutcome :=
  match completionAckOutcome expectedRealm observed with
  | .confirmed =>
      ⟨.continueBattle, [.completionAckConfirmed |> infoRecord]⟩
  | .outcomeUnknown =>
      ⟨.battleInterrupted,
        [.completionAckOutcomeUnknown |> errorRecord,
          .runnerCompletionAckUnconfirmed |> errorRecord]⟩

def configurationFailureExitCode : Nat := 2
def postBattleFailureExitCode : Nat := 3
def battleInterruptedExitCode : Nat := 4
def loggingFailureExitCode : Nat := 5

/-!
Python owns the application log in both direct and supervised launches. A
terminal record must be persisted before its intended status is published; a
sink failure therefore replaces every intended status with terminal logging
failure exit 5.
-/
def applicationLoggedExitCode
    (intendedExitCode : Nat) (applicationLogSucceeded : Bool) : Nat :=
  if applicationLogSucceeded then
    intendedExitCode
  else
    loggingFailureExitCode

/-!
The outer launcher observes the checked sink through an independent marker.
The marker is tested before idle time, so a broken durable sink can never be
classified as a retryable watchdog timeout, even when both observations are
present at the same poll.
-/
inductive WatchdogDecision where
  | continue
  | stopForLoggingFailure
  | stopForIdleTimeout
  deriving DecidableEq, Repr

structure DurableSinkHealth where
  openSucceeded : Bool
  writeSucceeded : Bool
  pathIdentityMatches : Bool
  finalizeSucceeded : Bool
  deriving DecidableEq, Repr

def sinkFailureMarked (health : DurableSinkHealth) : Bool :=
  !(health.openSucceeded && health.writeSucceeded &&
    health.pathIdentityMatches && health.finalizeSucceeded)

def watchdogDecision (sinkFailureMarked idleTimeoutReached : Bool) :
    WatchdogDecision :=
  if sinkFailureMarked then
    .stopForLoggingFailure
  else if idleTimeoutReached then
    .stopForIdleTimeout
  else
    .continue

def checkedWatchdogDecision
    (health : DurableSinkHealth) (idleTimeoutReached : Bool) :
    WatchdogDecision :=
  watchdogDecision (sinkFailureMarked health) idleTimeoutReached

/-!
`main.sh` owns only `supervisor.log`. If appending its post-child decision
fails, an already-terminal 2/3/4/5 remains terminal; success or an unclassified
child failure becomes logging-failure exit 5.
-/
def supervisorDecisionExitCode
    (childExitCode : Nat) (decisionLogSucceeded : Bool) : Nat :=
  if decisionLogSucceeded then
    childExitCode
  else if childExitCode = configurationFailureExitCode ∨
      childExitCode = postBattleFailureExitCode ∨
      childExitCode = battleInterruptedExitCode ∨
      childExitCode = loggingFailureExitCode then
    childExitCode
  else
    loggingFailureExitCode

def exitAfterDecisionLog
    (childExitCode : Nat) (decisionLogSucceeded : Bool) : Nat :=
  supervisorDecisionExitCode childExitCode decisionLogSucceeded

/-!
This is a terminal projection, not hvbattle restart policy. A caller may first
consume an in-process worker/browser restart budget. Only after that caller has
chosen to stop does the interruption unwind through `BattleSession` and the
browser context before `main` records exit 4. Both typed interruption variants
retain the same terminal record suffix at this outer boundary.
-/
def projectTerminalRunnerOutcomeToApplication
    (runner : Audited RunnerOutcome) : Audited ApplicationOutcome :=
  match runner.outcome with
  | .continueBattle => ⟨.running, runner.records⟩
  | .battleInterrupted | .battleRecoveryExhausted =>
      ⟨.exited battleInterruptedExitCode,
        runner.records ++
          [.driverException |> errorRecord,
            .applicationBattleInterrupted |> errorRecord]⟩

def projectTerminalApplicationTransition
    (before current : BattleSnapshot)
    (retainedTransitionMonitor : Option ActionMonitor)
    (expectedRealm : BattleRealm)
    (recoveryEvidence : ActionRecoveryEvidence)
    (recoveryObservation : RecoveryObservation)
    (budget : RecoveryBudget) : Audited ApplicationOutcome :=
  projectTerminalRunnerOutcomeToApplication
    (runTransition before current retainedTransitionMonitor expectedRealm
      recoveryEvidence recoveryObservation budget)

def projectTerminalApplicationUnknownAction
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget) : Audited ApplicationOutcome :=
  projectTerminalRunnerOutcomeToApplication
    (runUnknownActionRecovery expectedRealm evidence observed budget)

def unknownTransitionErrorRecordSuffix : List LogRecord :=
  [errorRecord .transitionOutcomeUnknown,
    errorRecord .runnerCompletionUnconfirmed,
    errorRecord .driverException,
    errorRecord .applicationBattleInterrupted]

def hasExactSuffix (records suffix : List LogRecord) : Prop :=
  ∃ precedingRecords, records = precedingRecords ++ suffix

def postBattleFailureReport : Audited ApplicationOutcome :=
  ⟨.exited postBattleFailureExitCode,
    [.applicationPostBattleFailure |> errorRecord]⟩

/-!
`main.sh` is a zero-retry lifecycle wrapper. `launcherShouldRetry` models the
separate outer `battle.zsh` policy, which treats exits 2, 3, 4, and 5 as
terminal stops.
-/
inductive LauncherFailureKind where
  | timeout
  | crash
  deriving DecidableEq, Repr

def mainShellShouldRetry
    (_childExitCode _attempt _maxAttempts : Nat) : Bool :=
  false

def launcherShouldRetry
    (failureKind : LauncherFailureKind)
    (childExitCode retriesUsed maxRetries : Nat) : Bool :=
  if childExitCode = 0 ∨ childExitCode = configurationFailureExitCode ∨
      childExitCode = postBattleFailureExitCode ∨
      childExitCode = battleInterruptedExitCode ∨
      childExitCode = loggingFailureExitCode then
    false
  else if failureKind = .crash ∧
      (childExitCode = 130 ∨ childExitCode = 143) then
    false
  else
    decide (retriesUsed < maxRetries)

theorem postBattleFailureMapsToExitThreeAndLogs :
    postBattleFailureReport.outcome = .exited 3 ∧
      errorRecord .applicationPostBattleFailure ∈
        postBattleFailureReport.records := by
  native_decide

theorem terminalProjectionOfBattleInterruptionExitsFourAndLogs
    (records : List LogRecord) :
    let result := projectTerminalRunnerOutcomeToApplication
      (Audited.mk .battleInterrupted records)
    result.outcome = .exited 4 ∧
      errorRecord .driverException ∈ result.records ∧
      errorRecord .applicationBattleInterrupted ∈ result.records := by
  simp [projectTerminalRunnerOutcomeToApplication, battleInterruptedExitCode]

theorem terminalProjectionOfRecoveryExhaustionExitsFourAndLogs
    (records : List LogRecord) :
    let result := projectTerminalRunnerOutcomeToApplication
      (Audited.mk .battleRecoveryExhausted records)
    result.outcome = .exited 4 ∧
      errorRecord .driverException ∈ result.records ∧
      errorRecord .applicationBattleInterrupted ∈ result.records := by
  simp [projectTerminalRunnerOutcomeToApplication, battleInterruptedExitCode]

theorem healthyLoggingPreservesEveryChildStatus
    (childExitCode : Nat) :
    applicationLoggedExitCode childExitCode true = childExitCode := by
  simp [applicationLoggedExitCode]

theorem failedApplicationLoggingAlwaysExitsFive
    (childExitCode : Nat) :
    applicationLoggedExitCode childExitCode false = 5 := by
  simp [applicationLoggedExitCode, loggingFailureExitCode]

theorem successfulChildAndApplicationLogExitZero :
    applicationLoggedExitCode 0 true = 0 := by
  native_decide

theorem successfulChildWithFailedApplicationLogExitsFive :
    applicationLoggedExitCode 0 false = 5 := by
  native_decide

theorem unclassifiedChildWithFailedApplicationLogExitsFive :
    applicationLoggedExitCode 1 false = 5 := by
  native_decide

theorem sinkFailureMarkerPrecedesIdleTimeout
    (idleTimeoutReached : Bool) :
    watchdogDecision true idleTimeoutReached = .stopForLoggingFailure := by
  simp [watchdogDecision]

theorem replacedLogPathIsMarkedBeforeIdleClassification
    (health : DurableSinkHealth)
    (replaced : health.pathIdentityMatches = false)
    (idleTimeoutReached : Bool) :
    checkedWatchdogDecision health idleTimeoutReached =
      .stopForLoggingFailure := by
  simp [checkedWatchdogDecision, sinkFailureMarked, replaced,
    watchdogDecision]

theorem idleTimeoutRequiresHealthySinkMarker :
    watchdogDecision false true = .stopForIdleTimeout := by
  native_decide

theorem failedDecisionLogCannotTurnSuccessIntoSuccess :
    exitAfterDecisionLog 0 false = 5 := by
  native_decide

theorem failedDecisionLogCannotPermitGenericRetry :
    exitAfterDecisionLog 1 false = 5 := by
  native_decide

theorem failedDecisionLogPreservesExistingSafetyStop
    (childExitCode : Nat)
    (terminal :
      childExitCode = 2 ∨ childExitCode = 3 ∨ childExitCode = 4 ∨
        childExitCode = 5) :
    exitAfterDecisionLog childExitCode false = childExitCode := by
  rcases terminal with terminal | terminal | terminal | terminal <;>
    subst childExitCode <;> native_decide

theorem failedDecisionLogPreservesExistingLoggingFailure :
    exitAfterDecisionLog 5 false = 5 := by
  native_decide

theorem mainShellNeverRetriesConfigurationFailure
    (attempt maxAttempts : Nat) :
    mainShellShouldRetry 2 attempt maxAttempts = false := by
  simp [mainShellShouldRetry]

theorem mainShellNeverRetriesPostBattleFailure
    (attempt maxAttempts : Nat) :
    mainShellShouldRetry 3 attempt maxAttempts = false := by
  simp [mainShellShouldRetry]

theorem mainShellNeverRetriesBattleInterruption
    (attempt maxAttempts : Nat) :
    mainShellShouldRetry 4 attempt maxAttempts = false := by
  simp [mainShellShouldRetry]

theorem mainShellNeverRetriesLoggingFailure
    (attempt maxAttempts : Nat) :
    mainShellShouldRetry 5 attempt maxAttempts = false := by
  simp [mainShellShouldRetry]

theorem launcherNeverRetriesPostBattleFailure
    (failureKind : LauncherFailureKind)
    (retriesUsed maxRetries : Nat) :
    launcherShouldRetry failureKind 3 retriesUsed maxRetries = false := by
  simp [launcherShouldRetry, postBattleFailureExitCode]

theorem launcherNeverRetriesConfigurationFailure
    (failureKind : LauncherFailureKind)
    (retriesUsed maxRetries : Nat) :
    launcherShouldRetry failureKind 2 retriesUsed maxRetries = false := by
  simp [launcherShouldRetry, configurationFailureExitCode]

theorem launcherNeverRetriesBattleInterruption
    (failureKind : LauncherFailureKind)
    (retriesUsed maxRetries : Nat) :
    launcherShouldRetry failureKind 4 retriesUsed maxRetries = false := by
  simp [launcherShouldRetry, battleInterruptedExitCode]

theorem launcherNeverRetriesLoggingFailure
    (failureKind : LauncherFailureKind)
    (retriesUsed maxRetries : Nat) :
    launcherShouldRetry failureKind 5 retriesUsed maxRetries = false := by
  simp [launcherShouldRetry, loggingFailureExitCode]

theorem failedApplicationLogAfterSuccessfulChildIsTerminal
    (failureKind : LauncherFailureKind)
    (attempt maxAttempts retriesUsed maxRetries : Nat) :
    applicationLoggedExitCode 0 false = 5 ∧
      mainShellShouldRetry 5 attempt maxAttempts = false ∧
      launcherShouldRetry failureKind 5 retriesUsed maxRetries = false := by
  simp [applicationLoggedExitCode, loggingFailureExitCode,
    mainShellShouldRetry, launcherShouldRetry,
    configurationFailureExitCode, postBattleFailureExitCode,
    battleInterruptedExitCode]

theorem failedApplicationLogAfterUnclassifiedChildIsTerminal
    (failureKind : LauncherFailureKind)
    (attempt maxAttempts retriesUsed maxRetries : Nat) :
    applicationLoggedExitCode 1 false = 5 ∧
      mainShellShouldRetry 5 attempt maxAttempts = false ∧
      launcherShouldRetry failureKind 5 retriesUsed maxRetries = false := by
  simp [applicationLoggedExitCode, loggingFailureExitCode,
    mainShellShouldRetry, launcherShouldRetry,
    configurationFailureExitCode, postBattleFailureExitCode,
    battleInterruptedExitCode]

/-!
Once coordinator recovery was rejected or exhausted, an unknown manager
outcome is fail-closed and creates the runner-level error record regardless of
the records already attached by the manager.
-/
theorem unrecoveredManagerUnknownAlwaysInterruptsAndLogs
    (records : List LogRecord) :
    let result := mapUnrecoveredManagerOutcomeToRunner
      (Audited.mk .battleActionOutcomeUnknown records)
    result.outcome = .battleInterrupted ∧
      errorRecord .runnerCompletionUnconfirmed ∈ result.records := by
  simp [mapUnrecoveredManagerOutcomeToRunner]

theorem nonRecoveredUnknownMapsToRunnerInterruptionWithErrorRecords
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (notRecovered :
      resolveUnknownAction expectedRealm evidence observed budget ≠
        .recoverToPrepare) :
    let result := runUnknownActionRecovery expectedRealm evidence observed budget
    (result.outcome = .battleInterrupted ∨
        result.outcome = .battleRecoveryExhausted) ∧
      errorRecord .transitionOutcomeUnknown ∈ result.records ∧
      errorRecord .runnerCompletionUnconfirmed ∈ result.records := by
  cases resolutionCase :
      resolveUnknownAction expectedRealm evidence observed budget <;>
    simp_all [runUnknownActionRecovery, mapUnrecoveredManagerOutcomeToRunner]

theorem recoveryExhaustionMapsToTypedRunnerInterruption
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (exhausted :
      resolveUnknownAction expectedRealm evidence observed budget =
        .recoveryExhausted) :
    let result := runUnknownActionRecovery expectedRealm evidence observed budget
    result.outcome = .battleRecoveryExhausted ∧
      errorRecord .transitionOutcomeUnknown ∈ result.records ∧
      errorRecord .runnerCompletionUnconfirmed ∈ result.records ∧
      diagnosticCodeForResolution
        (resolveUnknownAction expectedRealm evidence observed budget) =
          some "battle.action-recovery-exhausted" := by
  simp [runUnknownActionRecovery, exhausted, diagnosticCodeForResolution]

theorem recoverableUnknownContinuesOnlyThroughPrepare
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (recovered :
      resolveUnknownAction expectedRealm evidence observed budget =
        .recoverToPrepare) :
    let result := runUnknownActionRecovery expectedRealm evidence observed budget
    result.outcome = .continueBattle ∧
      infoRecord .actionRecoveryAccepted ∈ result.records ∧
      recoveryNextStep
        (resolveUnknownAction expectedRealm evidence observed budget) =
        .prepareAndRunStrategy := by
  simp [runUnknownActionRecovery, recovered, recoveryNextStep]

/-!
A transition with no positive evidence and no accepted same-browser recovery
emits the manager and runner error records. A separate terminal projection used
below applies only after a caller has chosen to stop; it does not decide whether
the caller should create a new browser or restart a worker first.
-/
theorem unknownTransitionMapsToInterruptionWithErrorRecords
    (before current : BattleSnapshot)
    (retainedTransitionMonitor : Option ActionMonitor)
    (expectedRealm : BattleRealm)
    (recoveryEvidence : ActionRecoveryEvidence)
    (recoveryObservation : RecoveryObservation)
    (budget : RecoveryBudget)
    (unknown :
      acceptedTransitionEvidence before current retainedTransitionMonitor = none)
    (notRecovered :
      resolveUnknownAction expectedRealm
        (freezeTransitionRecoveryEvidence recoveryEvidence
          retainedTransitionMonitor)
        recoveryObservation budget ≠ .recoverToPrepare) :
    let result := runTransition before current retainedTransitionMonitor
      expectedRealm recoveryEvidence recoveryObservation budget
    (result.outcome = .battleInterrupted ∨
        result.outcome = .battleRecoveryExhausted) ∧
      errorRecord .transitionOutcomeUnknown ∈ result.records ∧
      errorRecord .runnerCompletionUnconfirmed ∈ result.records := by
  simpa [runTransition, unknown] using
    nonRecoveredUnknownMapsToRunnerInterruptionWithErrorRecords expectedRealm
      (freezeTransitionRecoveryEvidence recoveryEvidence
        retainedTransitionMonitor)
      recoveryObservation budget notRecovered

theorem recoverableUnknownTransitionReturnsToFreshPrepare
    (before current : BattleSnapshot)
    (retainedTransitionMonitor : Option ActionMonitor)
    (expectedRealm : BattleRealm)
    (recoveryEvidence : ActionRecoveryEvidence)
    (recoveryObservation : RecoveryObservation)
    (budget : RecoveryBudget)
    (unknown :
      acceptedTransitionEvidence before current retainedTransitionMonitor = none)
    (recovered :
      resolveUnknownAction expectedRealm
        (freezeTransitionRecoveryEvidence recoveryEvidence
          retainedTransitionMonitor)
        recoveryObservation budget = .recoverToPrepare) :
    let result := runTransition before current retainedTransitionMonitor
      expectedRealm recoveryEvidence recoveryObservation budget
    result.outcome = .continueBattle ∧
      infoRecord .actionRecoveryAccepted ∈ result.records ∧
      recoveryNextStep
        (resolveUnknownAction expectedRealm
          (freezeTransitionRecoveryEvidence recoveryEvidence
            retainedTransitionMonitor)
          recoveryObservation budget) = .prepareAndRunStrategy := by
  simp [runTransition, unknown, runUnknownActionRecovery,
    recovered, recoveryNextStep]

theorem terminalUnknownTransitionHasExactOrderedErrorRecordSuffix
    (before current : BattleSnapshot)
    (retainedTransitionMonitor : Option ActionMonitor)
    (expectedRealm : BattleRealm)
    (recoveryEvidence : ActionRecoveryEvidence)
    (recoveryObservation : RecoveryObservation)
    (budget : RecoveryBudget)
    (unknown :
      acceptedTransitionEvidence before current retainedTransitionMonitor = none)
    (notRecovered :
      resolveUnknownAction expectedRealm
        (freezeTransitionRecoveryEvidence recoveryEvidence
          retainedTransitionMonitor)
        recoveryObservation budget ≠ .recoverToPrepare) :
    hasExactSuffix
      (projectTerminalApplicationTransition before current
        retainedTransitionMonitor expectedRealm recoveryEvidence
        recoveryObservation budget).records
      unknownTransitionErrorRecordSuffix := by
  refine ⟨[], ?_⟩
  cases resolutionCase : resolveUnknownAction expectedRealm
      (freezeTransitionRecoveryEvidence recoveryEvidence
        retainedTransitionMonitor)
      recoveryObservation budget <;>
    simp_all [projectTerminalApplicationTransition, runTransition,
      runUnknownActionRecovery, mapUnrecoveredManagerOutcomeToRunner,
      projectTerminalRunnerOutcomeToApplication,
      unknownTransitionErrorRecordSuffix]

/-!
This terminal theorem is intentionally limited to an ordinary `.interrupt`
resolution. A `.recoveryExhausted` result remains a distinct reusable hvbattle
outcome; any browser-restart policy belongs to the calling application.
-/
theorem ordinaryUnknownTransitionTerminalProjectionStopsWithoutShellRetry
    (before current : BattleSnapshot)
    (retainedTransitionMonitor : Option ActionMonitor)
    (expectedRealm : BattleRealm)
    (recoveryEvidence : ActionRecoveryEvidence)
    (recoveryObservation : RecoveryObservation)
    (budget : RecoveryBudget)
    (unknown :
      acceptedTransitionEvidence before current retainedTransitionMonitor = none)
    (ordinary :
      resolveUnknownAction expectedRealm
        (freezeTransitionRecoveryEvidence recoveryEvidence
          retainedTransitionMonitor)
        recoveryObservation budget = .interrupt)
    (failureKind : LauncherFailureKind)
    (applicationLogSucceeded : Bool)
    (attempt maxAttempts retriesUsed maxRetries : Nat) :
    let result := projectTerminalApplicationTransition before current
      retainedTransitionMonitor expectedRealm recoveryEvidence
      recoveryObservation budget
    result.outcome = .exited 4 ∧
      errorRecord .transitionOutcomeUnknown ∈ result.records ∧
      errorRecord .runnerCompletionUnconfirmed ∈ result.records ∧
      errorRecord .driverException ∈ result.records ∧
      errorRecord .applicationBattleInterrupted ∈ result.records ∧
      (applicationLoggedExitCode 4 applicationLogSucceeded =
        if applicationLogSucceeded then 4 else 5) ∧
      mainShellShouldRetry
        (applicationLoggedExitCode 4 applicationLogSucceeded)
        attempt maxAttempts = false ∧
      launcherShouldRetry failureKind
        (applicationLoggedExitCode 4 applicationLogSucceeded)
        retriesUsed maxRetries = false := by
  cases applicationLogSucceeded <;>
    simp [projectTerminalApplicationTransition, runTransition, unknown,
    runUnknownActionRecovery, ordinary,
    mapUnrecoveredManagerOutcomeToRunner,
    projectTerminalRunnerOutcomeToApplication, battleInterruptedExitCode,
    applicationLoggedExitCode, loggingFailureExitCode,
    mainShellShouldRetry, launcherShouldRetry,
    configurationFailureExitCode, postBattleFailureExitCode]

/-!
Monitor-arm, monitor-cleanup, and session-parse live-operation timeouts are
raised as ordinary `BattleInterruptedError` values before same-browser recovery
is entered. Browser-context closure and any later restart decision are caller
responsibilities. If the caller then chooses the terminal projection, it
observes exit 4 and no shell retry.
-/
theorem ordinaryLiveOperationTimeoutTerminalProjectionStopsWithoutShellRetry
    (failureKind : LauncherFailureKind)
    (applicationLogSucceeded : Bool)
    (attempt maxAttempts retriesUsed maxRetries : Nat) :
    let result := projectTerminalRunnerOutcomeToApplication
      (Audited.mk .battleInterrupted [])
    result.outcome = .exited 4 ∧
      errorRecord .driverException ∈ result.records ∧
      errorRecord .applicationBattleInterrupted ∈ result.records ∧
      (applicationLoggedExitCode 4 applicationLogSucceeded =
        if applicationLogSucceeded then 4 else 5) ∧
      mainShellShouldRetry
        (applicationLoggedExitCode 4 applicationLogSucceeded)
        attempt maxAttempts = false ∧
      launcherShouldRetry failureKind
        (applicationLoggedExitCode 4 applicationLogSucceeded)
        retriesUsed maxRetries = false := by
  cases applicationLogSucceeded <;>
    simp [projectTerminalRunnerOutcomeToApplication, battleInterruptedExitCode,
    applicationLoggedExitCode, loggingFailureExitCode,
    mainShellShouldRetry, launcherShouldRetry,
    configurationFailureExitCode, postBattleFailureExitCode]

theorem unmatchedUnknownActionTerminalProjectionStopsWithoutShellRetry
    (expectedRealm : BattleRealm)
    (evidence : ActionRecoveryEvidence)
    (observed : RecoveryObservation)
    (budget : RecoveryBudget)
    (validRealm : validExpectedRealm expectedRealm = true)
    (available : recoveryBudgetAvailable budget = true)
    (unmatched : matchesReloadRecoveryIncident evidence = false)
    (failureKind : LauncherFailureKind)
    (applicationLogSucceeded : Bool)
    (attempt maxAttempts retriesUsed maxRetries : Nat) :
    let result := projectTerminalApplicationUnknownAction
      expectedRealm evidence observed budget
    result.outcome = .exited 4 ∧
      errorRecord .transitionOutcomeUnknown ∈ result.records ∧
      errorRecord .runnerCompletionUnconfirmed ∈ result.records ∧
      errorRecord .driverException ∈ result.records ∧
      errorRecord .applicationBattleInterrupted ∈ result.records ∧
      (applicationLoggedExitCode 4 applicationLogSucceeded =
        if applicationLogSucceeded then 4 else 5) ∧
      mainShellShouldRetry
        (applicationLoggedExitCode 4 applicationLogSucceeded)
        attempt maxAttempts = false ∧
      launcherShouldRetry failureKind
        (applicationLoggedExitCode 4 applicationLogSucceeded)
        retriesUsed maxRetries = false := by
  have ordinary := unmatchedUnknownInterrupts expectedRealm evidence observed
    budget validRealm available unmatched
  cases applicationLogSucceeded <;>
    simp [projectTerminalApplicationUnknownAction,
    runUnknownActionRecovery, ordinary, mapUnrecoveredManagerOutcomeToRunner,
    projectTerminalRunnerOutcomeToApplication, battleInterruptedExitCode,
    applicationLoggedExitCode, loggingFailureExitCode,
    mainShellShouldRetry, launcherShouldRetry,
    configurationFailureExitCode, postBattleFailureExitCode]

theorem unknownCompletionAckTerminalProjectionStopsWithoutShellRetry
    (expectedRealm : BattleRealm)
    (observed : CompletionAckObservation)
    (unknown : completionAckOutcome expectedRealm observed = .outcomeUnknown)
    (failureKind : LauncherFailureKind)
    (applicationLogSucceeded : Bool)
    (attempt maxAttempts retriesUsed maxRetries : Nat) :
    let result := projectTerminalRunnerOutcomeToApplication
      (runCompletionAck expectedRealm observed)
    result.outcome = .exited 4 ∧
      errorRecord .completionAckOutcomeUnknown ∈ result.records ∧
      errorRecord .runnerCompletionAckUnconfirmed ∈ result.records ∧
      errorRecord .driverException ∈ result.records ∧
      errorRecord .applicationBattleInterrupted ∈ result.records ∧
      (applicationLoggedExitCode 4 applicationLogSucceeded =
        if applicationLogSucceeded then 4 else 5) ∧
      mainShellShouldRetry
        (applicationLoggedExitCode 4 applicationLogSucceeded)
        attempt maxAttempts = false ∧
      launcherShouldRetry failureKind
        (applicationLoggedExitCode 4 applicationLogSucceeded)
        retriesUsed maxRetries = false := by
  cases applicationLogSucceeded <;>
    simp [runCompletionAck, unknown,
    projectTerminalRunnerOutcomeToApplication,
    battleInterruptedExitCode, applicationLoggedExitCode, loggingFailureExitCode,
    mainShellShouldRetry, launcherShouldRetry,
    configurationFailureExitCode, postBattleFailureExitCode]

end HVBattle

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

theorem observedRound21To22InteractiveIsAccepted :
    confirmedTransitionEvidence observedRound21 observedRound22Interactive =
      some .battleGenerationRoundAdvanced := by
  native_decide

theorem observedRound21To22LoadingIsRejected :
    confirmedTransitionEvidence observedRound21 observedRound22Loading = none := by
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

def mapManagerOutcomeToRunner
    (manager : Audited TransitionManagerOutcome) : Audited RunnerOutcome :=
  match manager.outcome with
  | .confirmed _ => ⟨.continueBattle, manager.records⟩
  | .battleActionOutcomeUnknown =>
      ⟨.battleInterrupted,
        manager.records ++ [.runnerCompletionUnconfirmed |> errorRecord]⟩

def runTransition
    (before current : BattleSnapshot) : Audited RunnerOutcome :=
  mapManagerOutcomeToRunner
    (auditTransitionSelection (confirmedTransitionEvidence before current))

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
The foreground logging wrapper drains both pipeline processes. With a healthy
`tee`, it preserves every child status. If `tee` fails, it preserves only the
already-terminal configuration/safety exits 2, 3, and 4; every other child
status maps to terminal logging-failure exit 5. This prevents missing logs from
turning a possibly completed run into an automatic retry.
-/
def loggedCommandExitCode (childExitCode : Nat) (teeSucceeded : Bool) : Nat :=
  if teeSucceeded then
    childExitCode
  else if childExitCode = configurationFailureExitCode ∨
      childExitCode = postBattleFailureExitCode ∨
      childExitCode = battleInterruptedExitCode then
    childExitCode
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
Appending the supervisor's own terminal decision uses the same fail-closed
matrix as the checked command sink.
-/
def exitAfterDecisionLog
    (childExitCode : Nat) (decisionLogSucceeded : Bool) : Nat :=
  loggedCommandExitCode childExitCode decisionLogSucceeded

/-!
An interruption unwinds through `BattleSession` and HBrowser's `Driver`
context before the private application's `main` handler logs the terminal
exception. The record order below mirrors that runtime path.
-/

def mapRunnerOutcomeToApplication
    (runner : Audited RunnerOutcome) : Audited ApplicationOutcome :=
  match runner.outcome with
  | .continueBattle => ⟨.running, runner.records⟩
  | .battleInterrupted =>
      ⟨.exited battleInterruptedExitCode,
        runner.records ++
          [.driverException |> errorRecord,
            .applicationBattleInterrupted |> errorRecord]⟩

def runApplicationTransition
    (before current : BattleSnapshot) : Audited ApplicationOutcome :=
  mapRunnerOutcomeToApplication (runTransition before current)

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
The private application has two supervisor layers. `containerSupervisorShouldRetry`
models `main.sh`; `launcherShouldRetry` models `battle.zsh`. Both classify exit
2, 3, 4, and 5 as terminal stops.
-/
inductive LauncherFailureKind where
  | timeout
  | crash
  deriving DecidableEq, Repr

def containerSupervisorShouldRetry
    (childExitCode attempt maxAttempts : Nat) : Bool :=
  if childExitCode = 0 ∨ childExitCode = configurationFailureExitCode ∨
      childExitCode = postBattleFailureExitCode ∨
      childExitCode = battleInterruptedExitCode ∨
      childExitCode = loggingFailureExitCode then
    false
  else
    decide (attempt < maxAttempts)

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

theorem battleInterruptedMapsToExitFourAndLogs
    (records : List LogRecord) :
    let result := mapRunnerOutcomeToApplication
      (Audited.mk .battleInterrupted records)
    result.outcome = .exited 4 ∧
      errorRecord .driverException ∈ result.records ∧
      errorRecord .applicationBattleInterrupted ∈ result.records := by
  simp [mapRunnerOutcomeToApplication, battleInterruptedExitCode]

theorem healthyLoggingPreservesEveryChildStatus
    (childExitCode : Nat) :
    loggedCommandExitCode childExitCode true = childExitCode := by
  simp [loggedCommandExitCode]

theorem failedLoggingPreservesOnlyTerminalChildStatuses
    (childExitCode : Nat)
    (terminal :
      childExitCode = configurationFailureExitCode ∨
        childExitCode = postBattleFailureExitCode ∨
        childExitCode = battleInterruptedExitCode) :
    loggedCommandExitCode childExitCode false = childExitCode := by
  simp [loggedCommandExitCode, terminal]

theorem failedLoggingMapsEveryNonterminalChildToFive
    (childExitCode : Nat)
    (notConfiguration : childExitCode ≠ configurationFailureExitCode)
    (notPostBattle : childExitCode ≠ postBattleFailureExitCode)
    (notInterrupted : childExitCode ≠ battleInterruptedExitCode) :
    loggedCommandExitCode childExitCode false = 5 := by
  simp [loggedCommandExitCode, notConfiguration, notPostBattle,
    notInterrupted, loggingFailureExitCode]

theorem successfulChildAndTeeExitZero :
    loggedCommandExitCode 0 true = 0 := by
  native_decide

theorem successfulChildWithFailedTeeExitsFive :
    loggedCommandExitCode 0 false = 5 := by
  native_decide

theorem unclassifiedChildWithFailedTeeExitsFive :
    loggedCommandExitCode 1 false = 5 := by
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
    (terminal : childExitCode = 2 ∨ childExitCode = 3 ∨ childExitCode = 4) :
    exitAfterDecisionLog childExitCode false = childExitCode := by
  simp [exitAfterDecisionLog, loggedCommandExitCode,
    configurationFailureExitCode, postBattleFailureExitCode,
    battleInterruptedExitCode, terminal]

theorem containerNeverRetriesConfigurationFailure
    (attempt maxAttempts : Nat) :
    containerSupervisorShouldRetry 2 attempt maxAttempts = false := by
  simp [containerSupervisorShouldRetry, configurationFailureExitCode]

theorem containerNeverRetriesPostBattleFailure
    (attempt maxAttempts : Nat) :
    containerSupervisorShouldRetry 3 attempt maxAttempts = false := by
  simp [containerSupervisorShouldRetry, postBattleFailureExitCode]

theorem containerNeverRetriesBattleInterruption
    (attempt maxAttempts : Nat) :
    containerSupervisorShouldRetry 4 attempt maxAttempts = false := by
  simp [containerSupervisorShouldRetry, battleInterruptedExitCode]

theorem containerNeverRetriesLoggingFailure
    (attempt maxAttempts : Nat) :
    containerSupervisorShouldRetry 5 attempt maxAttempts = false := by
  simp [containerSupervisorShouldRetry, loggingFailureExitCode]

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

theorem failedTeeAfterSuccessfulChildIsTerminal
    (failureKind : LauncherFailureKind)
    (attempt maxAttempts retriesUsed maxRetries : Nat) :
    loggedCommandExitCode 0 false = 5 ∧
      containerSupervisorShouldRetry 5 attempt maxAttempts = false ∧
      launcherShouldRetry failureKind 5 retriesUsed maxRetries = false := by
  simp [loggedCommandExitCode, loggingFailureExitCode,
    containerSupervisorShouldRetry, launcherShouldRetry,
    configurationFailureExitCode, postBattleFailureExitCode,
    battleInterruptedExitCode]

theorem failedTeeAfterUnclassifiedChildIsTerminal
    (failureKind : LauncherFailureKind)
    (attempt maxAttempts retriesUsed maxRetries : Nat) :
    loggedCommandExitCode 1 false = 5 ∧
      containerSupervisorShouldRetry 5 attempt maxAttempts = false ∧
      launcherShouldRetry failureKind 5 retriesUsed maxRetries = false := by
  simp [loggedCommandExitCode, loggingFailureExitCode,
    containerSupervisorShouldRetry, launcherShouldRetry,
    configurationFailureExitCode, postBattleFailureExitCode,
    battleInterruptedExitCode]

/-!
An unknown manager outcome is fail-closed and creates the runner-level error
record regardless of the records already attached by the manager.
-/
theorem managerUnknownAlwaysInterruptsAndLogs
    (records : List LogRecord) :
    let result := mapManagerOutcomeToRunner
      (Audited.mk .battleActionOutcomeUnknown records)
    result.outcome = .battleInterrupted ∧
      errorRecord .runnerCompletionUnconfirmed ∈ result.records := by
  simp [mapManagerOutcomeToRunner]

/-!
The reusable-library logging obligation: if transition selection has no
positive evidence, the manager emits its error record, the runner maps the
result to `BattleInterrupted`, and the runner emits its own error record.
-/
theorem unknownTransitionMapsToInterruptionWithErrorRecords
    (before current : BattleSnapshot)
    (unknown : confirmedTransitionEvidence before current = none) :
    let result := runTransition before current
    result.outcome = .battleInterrupted ∧
      errorRecord .transitionOutcomeUnknown ∈ result.records ∧
      errorRecord .runnerCompletionUnconfirmed ∈ result.records := by
  simp [runTransition, unknown, auditTransitionSelection,
    mapManagerOutcomeToRunner]

theorem unknownTransitionHasExactOrderedErrorRecordSuffix
    (before current : BattleSnapshot)
    (unknown : confirmedTransitionEvidence before current = none) :
    hasExactSuffix
      (runApplicationTransition before current).records
      unknownTransitionErrorRecordSuffix := by
  refine ⟨[], ?_⟩
  simp [runApplicationTransition, runTransition, unknown,
    auditTransitionSelection, mapManagerOutcomeToRunner,
    mapRunnerOutcomeToApplication, unknownTransitionErrorRecordSuffix]

/-!
End-to-end safety theorem for the reported failure class. Missing transition
evidence produces manager, runner, driver-context, and application error
records; exits with the dedicated interruption code; and is rejected by both
retry supervisors.
-/
theorem unknownTransitionStopsApplicationWithoutRetry
    (before current : BattleSnapshot)
    (unknown : confirmedTransitionEvidence before current = none)
    (failureKind : LauncherFailureKind)
    (teeSucceeded : Bool)
    (attempt maxAttempts retriesUsed maxRetries : Nat) :
    let result := runApplicationTransition before current
    result.outcome = .exited 4 ∧
      errorRecord .transitionOutcomeUnknown ∈ result.records ∧
      errorRecord .runnerCompletionUnconfirmed ∈ result.records ∧
      errorRecord .driverException ∈ result.records ∧
      errorRecord .applicationBattleInterrupted ∈ result.records ∧
      loggedCommandExitCode 4 teeSucceeded = 4 ∧
      containerSupervisorShouldRetry 4 attempt maxAttempts = false ∧
      launcherShouldRetry failureKind 4 retriesUsed maxRetries = false := by
  simp [runApplicationTransition, runTransition, unknown,
    auditTransitionSelection, mapManagerOutcomeToRunner,
    mapRunnerOutcomeToApplication, battleInterruptedExitCode,
    loggedCommandExitCode, loggingFailureExitCode,
    containerSupervisorShouldRetry, launcherShouldRetry,
    configurationFailureExitCode, postBattleFailureExitCode]

theorem unknownCompletionAckStopsApplicationWithoutRetry
    (expectedRealm : BattleRealm)
    (observed : CompletionAckObservation)
    (unknown : completionAckOutcome expectedRealm observed = .outcomeUnknown)
    (failureKind : LauncherFailureKind)
    (teeSucceeded : Bool)
    (attempt maxAttempts retriesUsed maxRetries : Nat) :
    let result := mapRunnerOutcomeToApplication
      (runCompletionAck expectedRealm observed)
    result.outcome = .exited 4 ∧
      errorRecord .completionAckOutcomeUnknown ∈ result.records ∧
      errorRecord .runnerCompletionAckUnconfirmed ∈ result.records ∧
      errorRecord .driverException ∈ result.records ∧
      errorRecord .applicationBattleInterrupted ∈ result.records ∧
      loggedCommandExitCode 4 teeSucceeded = 4 ∧
      containerSupervisorShouldRetry 4 attempt maxAttempts = false ∧
      launcherShouldRetry failureKind 4 retriesUsed maxRetries = false := by
  simp [runCompletionAck, unknown, mapRunnerOutcomeToApplication,
    battleInterruptedExitCode, loggedCommandExitCode, loggingFailureExitCode,
    containerSupervisorShouldRetry, launcherShouldRetry,
    configurationFailureExitCode, postBattleFailureExitCode]

end HVBattle

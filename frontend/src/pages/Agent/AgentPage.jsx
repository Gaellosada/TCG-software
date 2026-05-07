import { useState, useCallback, useEffect, useLayoutEffect, useRef } from 'react';
import useAgentSession, { formatTokens, formatElapsed, formatElapsedSeconds } from '../../hooks/useAgentSession';
import { useUnfocusedTitleAlert } from '../../hooks/useUnfocusedTitleAlert';
import SessionPanel from './SessionPanel';
import ChatPanel from './ChatPanel';
import AssumptionsPanel from './AssumptionsPanel';
import NotebookPanel from './NotebookPanel';
import { DEFAULT_MODEL } from '../../constants/agent';
import styles from './AgentPage.module.css';

// Duration (ms) the "Turn complete" momentary badge stays fully visible before
// fading out. The CSS transition handles the visual fade over ~300ms after this.
const TURN_COMPLETE_VISIBLE_MS = 3000;

// Threshold (ms) above which the persistent footer appends a relative-time
// string: "Last turn: 09:14:32 (3h ago)". Finance context → 1h threshold.
const RELATIVE_TIME_THRESHOLD_MS = 60 * 60 * 1000; // 1 hour

// S4 helper: returns " (Xm ago)" / " (Xh ago)" when diffMs >= 1h, else "".
// Intentionally shows nothing for < 1h (time stamp alone is sufficient).
// Exported for test access; not part of the hook API.
export function formatAgo(date) {
  const diffMs = Date.now() - date.getTime();
  if (diffMs < RELATIVE_TIME_THRESHOLD_MS) return '';
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 60) return ` (${diffMin}m ago)`;
  return ` (${Math.floor(diffMin / 60)}h ago)`;
}

const TABS = [
  { id: 'chat', label: 'Chat' },
  { id: 'notebook', label: 'Notebook' },
];

// Issue 23: human-readable tooltip text for auto_continue reason tokens.
const AUTO_CONTINUE_REASON_LABELS = {
  missing_done_marker: "Continuing because the agent didn't signal completion.",
  unmet_intent: "Continuing because the agent announced work that wasn't done.",
};

function AgentPage() {
  const [selectedSessionId, setSelectedSessionId] = useState(null);
  const [activeTab, setActiveTab] = useState('chat');
  const [selectedModel, setSelectedModel] = useState(DEFAULT_MODEL);

  const {
    messages,
    assumptions,
    status,
    warningMessage,
    compactBanner,
    processExitInfo,
    clearProcessExit,
    turnAbortedInfo,
    clearTurnAborted,
    subagentCount,
    tokenUsage,
    elapsedMs,
    lastTurnComplete,
    isConnected,
    isProcessing,
    sendMessage,
    stopAgent,
    interruptAgent,
    notebookReady,
    notebookFailedInfo,
    autoContinueInfo,
    autoContinueCapped,
  } = useAgentSession(selectedSessionId);

  // Issue 28b: tab title + OS Notifications when page is unfocused.
  useUnfocusedTitleAlert({
    lastTurnComplete,
    autoContinueCapped,
    notebookReady,
    notebookFailedInfo,
  });

  // Issue 16(b): momentary indicator state — true while the badge is fully
  // visible (first 3s after turn_complete). After the timeout, flips to false
  // which triggers the CSS fade-out. lastTurnComplete remains set (persistent
  // footer in the token area) until the next session reset.
  const [turnCompleteBadgeVisible, setTurnCompleteBadgeVisible] = useState(false);
  const turnCompleteTimerRef = useRef(null);

  // M1 fix: useLayoutEffect fires synchronously before paint so the badge
  // enters the DOM at opacity 1 (Visible class) from the very first frame,
  // then decays to Faded after TURN_COMPLETE_VISIBLE_MS. useEffect was
  // post-paint, causing an unintended 0.35→1.0 fade-in instead of 1.0→0.35.
  useLayoutEffect(() => {
    if (!lastTurnComplete) {
      setTurnCompleteBadgeVisible(false);
      return undefined;
    }
    // New turn_complete arrived — show badge immediately.
    setTurnCompleteBadgeVisible(true);
    // Clear any prior timer before starting a new one.
    if (turnCompleteTimerRef.current !== null) {
      clearTimeout(turnCompleteTimerRef.current);
    }
    turnCompleteTimerRef.current = setTimeout(() => {
      setTurnCompleteBadgeVisible(false);
      turnCompleteTimerRef.current = null;
    }, TURN_COMPLETE_VISIBLE_MS);
    return () => {
      if (turnCompleteTimerRef.current !== null) {
        clearTimeout(turnCompleteTimerRef.current);
        turnCompleteTimerRef.current = null;
      }
    };
  }, [lastTurnComplete]);

  // Issue 23: auto-continue badge visibility — analogous to turn-complete badge.
  // true while autoContinueInfo is non-null (shown immediately on first receive).
  const [autoContinueBadgeVisible, setAutoContinueBadgeVisible] = useState(false);

  // G-INVAR row 12: useLayoutEffect so badge enters DOM at opacity 1 from first paint.
  useLayoutEffect(() => {
    if (!autoContinueInfo) {
      setAutoContinueBadgeVisible(false);
      return undefined;
    }
    setAutoContinueBadgeVisible(true);
    return undefined;
  }, [autoContinueInfo]);

  // Wrap sendMessage to include the selected model
  const handleSendMessage = useCallback(
    (content) => sendMessage(content, { model: selectedModel }),
    [sendMessage, selectedModel],
  );

  // Wrap interruptAgent to include the selected model
  const handleInterruptAgent = useCallback(
    (content) => interruptAgent(content, { model: selectedModel }),
    [interruptAgent, selectedModel],
  );

  return (
    <div className={styles.page}>
      <div className={styles.banner}>
        <span className={styles.bannerIcon}>&#9888;</span>
        This agent only has access to the database within this page, it cannot see other parts of the app. It retains everything you share (including strategies), so avoid disclosing sensitive or proprietary information.
      </div>
      <div className={styles.mainArea}>
        <div className={styles.leftColumn}>
          <div className={styles.sessionPanel}>
            <SessionPanel
              selectedId={selectedSessionId}
              onSelect={setSelectedSessionId}
            />
          </div>
          <div className={styles.assumptionsPanel}>
            <AssumptionsPanel assumptions={assumptions} />
          </div>
        </div>
        <div className={styles.rightColumn}>
          <div className={styles.tabBar}>
            {TABS.map(({ id, label }) => {
              // Issue 22 + Issue 27 F3: notebook tab state machine:
              //   ready    → teal dot + clickable (G-INVAR #22)
              //   failed   → amber dot + clickable + data-state="failed"
              //   disabled → greyed + cursor:not-allowed
              const isNotebookTab = id === 'notebook';
              const isNotebookFailed = isNotebookTab && !!notebookFailedInfo && !notebookReady;
              // Disabled only when no notebook info at all (neither ready nor failed).
              const isDisabled = isNotebookTab && !notebookReady && !isNotebookFailed;
              const isActive = activeTab === id;
              const tabClassName = [
                styles.tab,
                isActive ? styles.tabActive : '',
                isDisabled ? styles.tabDisabled : '',
                isNotebookTab && notebookReady ? styles.tabNotebookReady : '',
                isNotebookFailed ? styles.tabNotebookFailed : '',
              ].filter(Boolean).join(' ');

              // Failed tooltip (overrides the generic disabled tooltip).
              let titleAttr;
              let ariaLabel;
              if (isNotebookFailed) {
                titleAttr = 'Notebook compilation failed — no outputs detected';
                ariaLabel = `${label} — compilation failed, no outputs detected`;
              } else if (isDisabled) {
                titleAttr = 'No notebook available for this session';
              }

              return (
                <button
                  key={id}
                  className={tabClassName}
                  onClick={() => !isDisabled && setActiveTab(id)}
                  disabled={isDisabled}
                  aria-disabled={isDisabled}
                  aria-label={ariaLabel}
                  data-state={isNotebookFailed ? 'failed' : undefined}
                  title={titleAttr}
                >
                  {label}
                  {isNotebookTab && notebookReady && (
                    <span className={styles.tabNotebookDot} aria-hidden="true" />
                  )}
                  {isNotebookFailed && (
                    <span className={styles.tabNotebookFailedDot} aria-hidden="true" />
                  )}
                </button>
              );
            })}
            {status && status !== 'idle' && (
              <span className={styles.statusBadge}>{status}</span>
            )}
            {warningMessage && (
              <span className={`${styles.statusBadge} ${styles.statusBadgeWarning}`}>{warningMessage}</span>
            )}
            {compactBanner && (
              <span className={`${styles.statusBadge} ${styles.statusBadgeCompact}`}>{compactBanner}</span>
            )}
            {subagentCount > 0 && (
              <span
                className={`${styles.statusBadge} ${styles.statusBadgeSubagent}`}
                data-testid="subagent-badge"
              >
                {subagentCount === 1 ? '1 subagent running' : `${subagentCount} subagents running`}
              </span>
            )}
            {isProcessing && elapsedMs > 0 && (
              <span
                className={`${styles.statusBadge} ${styles.statusBadgeElapsed}`}
                data-testid="elapsed-badge"
              >
                {`Working for ${formatElapsed(elapsedMs)}`}
              </span>
            )}
            {/* M2 fix: role="status" + aria-live="polite" so screen readers
                announce the badge text when it appears dynamically. aria-atomic
                ensures the full label is read, not just the changed portion.
                Issue 23: mutually exclusive with auto-continue badge during loop. */}
            {lastTurnComplete && !isProcessing && !autoContinueInfo && (
              <span
                role="status"
                aria-live="polite"
                aria-atomic="true"
                className={`${styles.statusBadge} ${styles.statusBadgeTurnComplete} ${turnCompleteBadgeVisible ? styles.statusBadgeTurnCompleteVisible : styles.statusBadgeTurnCompleteFaded}`}
                data-testid="turn-complete-badge"
              >
                {`Turn complete (${formatElapsedSeconds(lastTurnComplete.elapsedSeconds)})`}
              </span>
            )}
            {/* Issue 23: auto-continue badge — shown during loop iteration.
                Mutually exclusive with turn-complete badge.
                G-INVAR row 13: aria-live="polite". G-INVAR row 14: --accent-teal. */}
            {autoContinueInfo && !autoContinueCapped && (
              <span
                role="status"
                aria-live="polite"
                aria-atomic="true"
                className={`${styles.statusBadge} ${styles.statusBadgeAutoContinue} ${autoContinueBadgeVisible ? styles.statusBadgeAutoContinueVisible : ''}`}
                data-testid="auto-continue-badge"
                title={AUTO_CONTINUE_REASON_LABELS[autoContinueInfo.reason] ?? autoContinueInfo.reason}
              >
                {`Continuing… (${autoContinueInfo.iter}/${autoContinueInfo.max})`}
              </span>
            )}
            {/* Issue 23: cap badge — shown when max iterations reached.
                S2 fix: autoContinueCapped is now {iter, max}|null (not boolean)
                so badge displays the actual dynamic max (e.g. 2× when env-overridden). */}
            {autoContinueCapped && (
              <span
                role="status"
                aria-live="polite"
                aria-atomic="true"
                className={`${styles.statusBadge} ${styles.statusBadgeAutoContinueCapped}`}
                data-testid="auto-continue-capped-badge"
              >
                {`Continued ${autoContinueCapped.max}×; task may be incomplete`}
              </span>
            )}
          </div>
          {processExitInfo && (
            <div className={styles.processExitBanner}>
              <span>
                Agent process exited unexpectedly. Returncode {processExitInfo.returncode ?? 'null'}.
              </span>
              {processExitInfo.stderrTail && (
                <details className={styles.processExitDetails}>
                  <summary>stderr</summary>
                  <pre className={styles.processExitPre}>{processExitInfo.stderrTail}</pre>
                </details>
              )}
              <button
                className={styles.processExitDismiss}
                onClick={clearProcessExit}
                aria-label="Dismiss"
              >
                &times;
              </button>
            </div>
          )}
          {turnAbortedInfo && (
            <div className={styles.turnAbortedBanner} data-testid="turn-aborted-banner">
              <span>
                Connection dropped during agent reply — partial response saved. Send a new message to continue.
              </span>
              <button
                className={styles.processExitDismiss}
                onClick={clearTurnAborted}
                aria-label="Dismiss"
              >
                &times;
              </button>
            </div>
          )}
          <div className={styles.contentArea}>
            {activeTab === 'chat' ? (
              <ChatPanel
                messages={messages}
                isConnected={isConnected}
                sendMessage={handleSendMessage}
                stopAgent={stopAgent}
                interruptAgent={handleInterruptAgent}
                isProcessing={isProcessing}
                selectedModel={selectedModel}
                onModelChange={setSelectedModel}
              />
            ) : (
              <NotebookPanel
                sessionId={selectedSessionId}
                notebookReady={notebookReady}
                notebookFailedInfo={notebookFailedInfo}
              />
            )}
          </div>
          {(tokenUsage.total > 0 || (lastTurnComplete && !isProcessing)) && (
            <div className={styles.tokenFooter} data-testid="token-footer">
              {lastTurnComplete && !isProcessing && (
                <span className={styles.tokenFooterTurnComplete} data-testid="turn-complete-footer">
                  {/* S2 fix: explicit hour12:false for 24h display in all locales.
                      S4 fix: append relative time when gap > 1h via formatAgo(). */}
                  {`Last turn: ${lastTurnComplete.at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}${formatAgo(lastTurnComplete.at)}`}
                </span>
              )}
              {tokenUsage.total > 0 && (
                <>
                  <span className={styles.tokenFooterLabel}>Session:</span>
                  <span className={styles.tokenFooterValue}>
                    {`${formatTokens(tokenUsage.input)} in / ${formatTokens(tokenUsage.output)} out`}
                  </span>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default AgentPage;

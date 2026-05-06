import { useState, useCallback, useEffect, useRef } from 'react';
import useAgentSession, { formatTokens, formatElapsed, formatElapsedSeconds } from '../../hooks/useAgentSession';
import SessionPanel from './SessionPanel';
import ChatPanel from './ChatPanel';
import AssumptionsPanel from './AssumptionsPanel';
import NotebookPanel from './NotebookPanel';
import { DEFAULT_MODEL } from '../../constants/agent';
import styles from './AgentPage.module.css';

// Duration (ms) the "Turn complete" momentary badge stays fully visible before
// fading out. The CSS transition handles the visual fade over ~300ms after this.
const TURN_COMPLETE_VISIBLE_MS = 3000;

const TABS = [
  { id: 'chat', label: 'Chat' },
  { id: 'notebook', label: 'Notebook' },
];

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
  } = useAgentSession(selectedSessionId);

  // Issue 16(b): momentary indicator state — true while the badge is fully
  // visible (first 3s after turn_complete). After the timeout, flips to false
  // which triggers the CSS fade-out. lastTurnComplete remains set (persistent
  // footer in the token area) until the next session reset.
  const [turnCompleteBadgeVisible, setTurnCompleteBadgeVisible] = useState(false);
  const turnCompleteTimerRef = useRef(null);

  useEffect(() => {
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
            {TABS.map(({ id, label }) => (
              <button
                key={id}
                className={`${styles.tab} ${activeTab === id ? styles.tabActive : ''}`}
                onClick={() => setActiveTab(id)}
              >
                {label}
              </button>
            ))}
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
            {lastTurnComplete && !isProcessing && (
              <span
                className={`${styles.statusBadge} ${styles.statusBadgeTurnComplete} ${turnCompleteBadgeVisible ? styles.statusBadgeTurnCompleteVisible : styles.statusBadgeTurnCompleteFaded}`}
                data-testid="turn-complete-badge"
              >
                {`Turn complete (${formatElapsedSeconds(lastTurnComplete.elapsedSeconds)})`}
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
              />
            )}
          </div>
          {(tokenUsage.total > 0 || (lastTurnComplete && !isProcessing)) && (
            <div className={styles.tokenFooter} data-testid="token-footer">
              {lastTurnComplete && !isProcessing && (
                <span className={styles.tokenFooterTurnComplete} data-testid="turn-complete-footer">
                  {`Last turn: ${lastTurnComplete.at.toLocaleTimeString()}`}
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

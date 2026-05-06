import { useState, useCallback } from 'react';
import useAgentSession, { formatTokens, formatElapsed } from '../../hooks/useAgentSession';
import SessionPanel from './SessionPanel';
import ChatPanel from './ChatPanel';
import AssumptionsPanel from './AssumptionsPanel';
import NotebookPanel from './NotebookPanel';
import { DEFAULT_MODEL } from '../../constants/agent';
import styles from './AgentPage.module.css';

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
    isConnected,
    isProcessing,
    sendMessage,
    stopAgent,
    interruptAgent,
    notebookReady,
  } = useAgentSession(selectedSessionId);

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
          {tokenUsage && tokenUsage.total > 0 && (
            <div className={styles.tokenFooter} data-testid="token-footer">
              <span className={styles.tokenFooterLabel}>Session:</span>
              <span className={styles.tokenFooterValue}>
                {`${formatTokens(tokenUsage.input)} in / ${formatTokens(tokenUsage.output)} out`}
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default AgentPage;

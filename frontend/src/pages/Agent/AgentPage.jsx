import { useState, useCallback } from 'react';
import useAgentSession from '../../hooks/useAgentSession';
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

  const { messages, assumptions, status, warningMessage, compactBanner, processExitInfo, clearProcessExit, isConnected, isProcessing, sendMessage, stopAgent, interruptAgent, notebookReady } =
    useAgentSession(selectedSessionId);

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
        </div>
      </div>
    </div>
  );
}

export default AgentPage;

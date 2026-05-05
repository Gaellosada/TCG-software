import { useState, useCallback } from 'react';
import useAgentSession from '../../hooks/useAgentSession';
import SessionPanel from './SessionPanel';
import ChatPanel from './ChatPanel';
import AssumptionsPanel from './AssumptionsPanel';
import NotebookPanel from './NotebookPanel';
import styles from './AgentPage.module.css';

const TABS = [
  { id: 'chat', label: 'Chat' },
  { id: 'notebook', label: 'Notebook' },
];

const MODELS = [
  { id: 'claude-sonnet-4-6', label: 'Sonnet 4.6' },
  { id: 'claude-opus-4-6', label: 'Opus 4.6' },
];

function AgentPage() {
  const [selectedSessionId, setSelectedSessionId] = useState(null);
  const [activeTab, setActiveTab] = useState('chat');
  const [selectedModel, setSelectedModel] = useState(MODELS[0].id);

  const { messages, assumptions, status, isConnected, isProcessing, sendMessage, notebookReady } =
    useAgentSession(selectedSessionId);

  const isStreaming = isProcessing;

  // Wrap sendMessage to include the selected model
  const handleSendMessage = useCallback(
    (content) => sendMessage(content, { model: selectedModel }),
    [sendMessage, selectedModel],
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
            <div className={styles.tabBarSpacer} />
            <select
              className={styles.modelSelect}
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              title="Model"
            >
              {MODELS.map(({ id, label }) => (
                <option key={id} value={id}>{label}</option>
              ))}
            </select>
            {status && status !== 'idle' && (
              <span className={styles.statusBadge}>{status}</span>
            )}
          </div>
          <div className={styles.contentArea}>
            {activeTab === 'chat' ? (
              <ChatPanel
                messages={messages}
                isConnected={isConnected}
                sendMessage={handleSendMessage}
                isStreaming={isStreaming}
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

import { useState } from 'react';
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

function formatUsage(usage) {
  if (!usage) return null;

  const total = (usage.session_input_tokens || 0) + (usage.session_output_tokens || 0);
  let sessionStr;
  if (total >= 1_000_000) sessionStr = `${(total / 1_000_000).toFixed(1)}M`;
  else if (total >= 1_000) sessionStr = `${(total / 1_000).toFixed(1)}k`;
  else sessionStr = String(total);

  return `Session: ${sessionStr} tokens`;
}

function AgentPage() {
  const [selectedSessionId, setSelectedSessionId] = useState(null);
  const [activeTab, setActiveTab] = useState('chat');

  const { messages, assumptions, status, isConnected, sendMessage, notebookReady, usage } =
    useAgentSession(selectedSessionId);

  const isStreaming =
    messages.length > 0 && messages[messages.length - 1]?.streaming === true;

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
            {activeTab === 'chat' && usage && (
              <span className={styles.usageInfo}>{formatUsage(usage)}</span>
            )}
          </div>
          <div className={styles.contentArea}>
            {activeTab === 'chat' ? (
              <ChatPanel
                messages={messages}
                isConnected={isConnected}
                sendMessage={sendMessage}
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

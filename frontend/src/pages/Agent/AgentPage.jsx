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

function formatTokens(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatReset(isoString) {
  if (!isoString) return null;
  try {
    const reset = new Date(isoString);
    const now = Date.now();
    const diffMs = reset - now;
    if (diffMs <= 0) return 'now';
    const mins = Math.ceil(diffMs / 60_000);
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    const rm = mins % 60;
    return `${hrs}hr ${rm}m`;
  } catch {
    return null;
  }
}

function UsageBar({ usage }) {
  if (!usage) return null;

  const sessionTokens =
    (usage.session_input_tokens || 0) + (usage.session_output_tokens || 0);

  const parts = [`Session: ${formatTokens(sessionTokens)} tokens`];

  if (usage.tokens_limit > 0) {
    const used = usage.tokens_limit - (usage.tokens_remaining || 0);
    const pct = ((used / usage.tokens_limit) * 100).toFixed(1);
    parts.push(`Rate: ${pct}%`);
  }

  const reset = formatReset(usage.tokens_reset);
  if (reset) {
    parts.push(`Reset: ${reset}`);
  }

  if (usage.requests_limit > 0) {
    const used = usage.requests_limit - (usage.requests_remaining || 0);
    const pct = ((used / usage.requests_limit) * 100).toFixed(1);
    parts.push(`Requests: ${pct}%`);
  }

  return <span className={styles.usageInfo}>{parts.join('  |  ')}</span>;
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
        This agent has read-only access to your MongoDB database. You can teach it strategies, assumptions, and domain knowledge through conversation.
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
            {activeTab === 'chat' && <UsageBar usage={usage} />}
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

import { useState, useRef, useEffect, useCallback } from 'react';
import renderMarkdown from './renderMarkdown';
import styles from './ChatPanel.module.css';

/**
 * Chat interface panel — message list + input area.
 *
 * Props:
 *   messages     {Array}     [{role, content, streaming?, name?, input?}]
 *   isConnected  {boolean}   WebSocket connected
 *   sendMessage  {Function}  (content: string) => void
 *   isStreaming  {boolean}   Last message is still streaming
 */
function ChatPanel({ messages, isConnected, sendMessage, isStreaming }) {
  const [draft, setDraft] = useState('');
  const listRef = useRef(null);
  const textareaRef = useRef(null);
  const shouldAutoScroll = useRef(true);

  // Track whether user has scrolled up to avoid hijacking their position
  const handleScroll = useCallback(() => {
    const el = listRef.current;
    if (!el) return;
    const threshold = 40;
    shouldAutoScroll.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
  }, []);

  // Auto-scroll to bottom on new messages (unless user scrolled up)
  useEffect(() => {
    if (shouldAutoScroll.current && listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages]);

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    const maxRows = 5;
    const lineHeight = 20;
    const maxH = lineHeight * maxRows;
    ta.style.height = `${Math.min(ta.scrollHeight, maxH)}px`;
  }, [draft]);

  function handleSend() {
    const text = draft.trim();
    if (!text || !isConnected || isStreaming) return;
    sendMessage(text);
    setDraft('');
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function renderMessage(msg, idx) {
    if (msg.role === 'tool') {
      return <ToolMessage key={idx} msg={msg} />;
    }

    if (msg.role === 'error') {
      return (
        <div key={idx} className={styles.errorMsg}>
          <span className={styles.errorLabel}>Error</span>
          <span>{msg.content}</span>
        </div>
      );
    }

    const isUser = msg.role === 'user';
    const bubbleClass = isUser ? styles.userBubble : styles.assistantBubble;

    return (
      <div
        key={idx}
        className={`${styles.msgRow} ${isUser ? styles.msgRowUser : styles.msgRowAssistant}`}
      >
        <div className={bubbleClass}>
          {isUser ? (
            <span>{msg.content}</span>
          ) : (
            <span
              className={styles.markdownBody}
              dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content) }}
            />
          )}
          {msg.streaming && <span className={styles.cursor} />}
        </div>
      </div>
    );
  }

  const canSend = isConnected && !isStreaming && draft.trim().length > 0;

  return (
    <div className={styles.panel}>
      <div className={styles.messageList} ref={listRef} onScroll={handleScroll}>
        {messages.length === 0 && (
          <div className={styles.empty}>Start a conversation...</div>
        )}
        {messages.length > 0 && (
          <div className={styles.messagesInner}>
            {messages.map(renderMessage)}
          </div>
        )}
      </div>

      <div className={styles.inputArea}>
        <span
          className={`${styles.connectionDot} ${isConnected ? styles.dotConnected : styles.dotDisconnected}`}
          title={isConnected ? 'Connected' : 'Disconnected'}
        />
        <textarea
          ref={textareaRef}
          className={styles.textarea}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={isConnected ? 'Send a message...' : 'Disconnected'}
          disabled={!isConnected}
          rows={1}
        />
        <button
          type="button"
          className={styles.sendBtn}
          onClick={handleSend}
          disabled={!canSend}
          title="Send"
          aria-label="Send message"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M3 13V3l10 5-10 5z" fill="currentColor" />
          </svg>
        </button>
      </div>
    </div>
  );
}

/**
 * Extracted as a proper component so useState (expanded) is legal
 * at the top level of this component instead of inside renderMessage.
 */
function ToolMessage({ msg }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className={styles.toolMsg}>
      <button
        type="button"
        className={styles.toolPill}
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className={styles.toolIcon}>&#9881;</span>
        <span className={styles.toolName}>{msg.name || 'tool'}</span>
        <span className={styles.toolChevron}>{expanded ? '\u25B4' : '\u25BE'}</span>
      </button>
      {expanded && msg.input && (
        <pre className={styles.toolInput}>
          {typeof msg.input === 'string'
            ? msg.input
            : JSON.stringify(msg.input, null, 2)}
        </pre>
      )}
    </div>
  );
}

export default ChatPanel;

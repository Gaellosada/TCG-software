import { useState, useRef, useEffect, useCallback } from 'react';
import renderMarkdown from './renderMarkdown';
import styles from './ChatPanel.module.css';

/**
 * Chat interface panel — message list + input area.
 *
 * Props:
 *   messages        {Array}     [{role, content, streaming?, name?, input?}]
 *   isConnected     {boolean}   WebSocket connected
 *   sendMessage     {Function}  (content: string) => void
 *   stopAgent       {Function}  () => void
 *   interruptAgent  {Function}  (content: string) => void
 *   isStreaming     {boolean}   Last message is still streaming
 *   selectedModel   {string}    Current model id
 *   onModelChange   {Function}  (modelId: string) => void
 */
function ChatPanel({ messages, isConnected, sendMessage, stopAgent, interruptAgent, isStreaming, selectedModel, onModelChange }) {
  const [draft, setDraft] = useState('');
  const [busyDialogText, setBusyDialogText] = useState(null);
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
    if (!text || !isConnected) return;

    if (isStreaming) {
      // Agent is busy — show dialog
      setBusyDialogText(text);
      return;
    }

    sendMessage(text);
    setDraft('');
  }

  function handleStop() {
    if (stopAgent) stopAgent();
  }

  function handleBusyInterrupt() {
    if (busyDialogText && interruptAgent) {
      interruptAgent(busyDialogText);
      setDraft('');
    }
    setBusyDialogText(null);
  }

  function handleBusyQueue() {
    if (busyDialogText) {
      sendMessage(busyDialogText);
      setDraft('');
    }
    setBusyDialogText(null);
  }

  function handleBusyCancel() {
    setBusyDialogText(null);
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

  const canSend = isConnected && draft.trim().length > 0;

  // Show thinking indicator when the agent is processing but not actively streaming text.
  // This covers: initial thinking, tool execution, between tool loops.
  const lastMsg = messages[messages.length - 1];
  const isActivelyStreaming = lastMsg?.role === 'assistant' && lastMsg.streaming && lastMsg.content;
  const showThinking = isStreaming && !isActivelyStreaming;

  return (
    <div className={styles.panel}>
      <div className={styles.messageList} ref={listRef} onScroll={handleScroll}>
        {messages.length === 0 && (
          <div className={styles.empty}>Start a conversation...</div>
        )}
        {messages.length > 0 && (
          <div className={styles.messagesInner}>
            {messages.map(renderMessage)}
            {showThinking && <ThinkingIndicator />}
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
        {isStreaming ? (
          <button
            type="button"
            className={`${styles.sendBtn} ${styles.stopBtn}`}
            onClick={handleStop}
            title="Stop agent"
            aria-label="Stop agent"
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <rect x="2" y="2" width="10" height="10" rx="1.5" fill="currentColor" />
            </svg>
          </button>
        ) : (
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
        )}
        <ModelPicker selected={selectedModel} onChange={onModelChange} />
      </div>

      {busyDialogText !== null && (
        <BusyDialog
          onInterrupt={handleBusyInterrupt}
          onQueue={handleBusyQueue}
          onCancel={handleBusyCancel}
        />
      )}
    </div>
  );
}

const MODELS = [
  { id: 'claude-sonnet-4-6', label: 'Sonnet' },
  { id: 'claude-opus-4-6', label: 'Opus' },
];

/**
 * Claude-style model picker — compact pill dropdown below the input.
 */
function ModelPicker({ selected, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    function handleClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  const current = MODELS.find((m) => m.id === selected) || MODELS[0];

  return (
    <div className={styles.modelPicker} ref={ref}>
      <button
        type="button"
        className={styles.modelBtn}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title="Select model"
      >
        <span className={styles.modelLabel}>{current.label}</span>
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" className={styles.modelChevron}>
          <path d="M2.5 4L5 6.5L7.5 4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <div className={styles.modelDropdown}>
          {MODELS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              className={`${styles.modelOption} ${id === selected ? styles.modelOptionActive : ''}`}
              onClick={() => { onChange(id); setOpen(false); }}
            >
              {label}
              {id === selected && (
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                  <path d="M2 6L5 9L10 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Animated dots indicator shown while the agent is thinking/processing.
 */
function ThinkingIndicator() {
  return (
    <div className={`${styles.msgRow} ${styles.msgRowAssistant}`}>
      <div className={styles.assistantBubble}>
        <span className={styles.thinking}>
          <span className={styles.dot} />
          <span className={styles.dot} />
          <span className={styles.dot} />
        </span>
      </div>
    </div>
  );
}

/**
 * Dialog shown when the user tries to send a message while the agent is working.
 * Offers: Interrupt & Send, Queue, or Cancel.
 */
function BusyDialog({ onInterrupt, onQueue, onCancel }) {
  return (
    <div
      className={styles.dialogBackdrop}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}
    >
      <div className={styles.dialogCard} role="dialog" aria-modal="true">
        <p className={styles.dialogMessage}>The agent is still working.</p>
        <div className={styles.dialogActions}>
          <button type="button" className={styles.dialogBtnCancel} onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className={styles.dialogBtnSecondary} onClick={onQueue}>
            Queue
          </button>
          <button type="button" className={styles.dialogBtnPrimary} onClick={onInterrupt}>
            Interrupt & Send
          </button>
        </div>
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

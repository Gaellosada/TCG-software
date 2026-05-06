// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import AgentPage from './AgentPage';

// ---------------------------------------------------------------------------
// Mock useAgentSession hook
// ---------------------------------------------------------------------------
const mockHookReturn = {
  messages: [],
  assumptions: [],
  status: 'idle',
  warningMessage: null,
  compactBanner: null,
  processExitInfo: null,
  clearProcessExit: vi.fn(),
  turnAbortedInfo: null,
  clearTurnAborted: vi.fn(),
  subagentCount: 0,
  tokenUsage: { input: 0, output: 0, total: 0 },
  elapsedMs: 0,
  turnStartTimestamp: null,
  isConnected: false,
  isProcessing: false,
  sendMessage: vi.fn(),
  stopAgent: vi.fn(),
  interruptAgent: vi.fn(),
  notebookReady: false,
};

vi.mock('../../hooks/useAgentSession', async (importOriginal) => {
  // Use importOriginal so the named exports (formatTokens, formatElapsed)
  // are real and only the default hook is mocked. AgentPage imports both
  // the hook and the helpers from this module.
  const actual = await importOriginal();
  return {
    ...actual,
    default: vi.fn(() => mockHookReturn),
  };
});

import useAgentSession from '../../hooks/useAgentSession';

// ---------------------------------------------------------------------------
// Mock child panels to isolate AgentPage logic
// ---------------------------------------------------------------------------
let capturedSessionPanelProps = null;
vi.mock('./SessionPanel', () => ({
  default: vi.fn((props) => {
    capturedSessionPanelProps = props;
    return <div data-testid="session-panel">SessionPanel</div>;
  }),
}));

vi.mock('./ChatPanel', () => ({
  default: vi.fn((props) => (
    <div data-testid="chat-panel" data-connected={String(props.isConnected)}>
      ChatPanel
    </div>
  )),
}));

vi.mock('./AssumptionsPanel', () => ({
  default: vi.fn((props) => (
    <div data-testid="assumptions-panel" data-count={props.assumptions.length}>
      AssumptionsPanel
    </div>
  )),
}));

vi.mock('./NotebookPanel', () => ({
  default: vi.fn((props) => (
    <div data-testid="notebook-panel" data-session={props.sessionId || ''}>
      NotebookPanel
    </div>
  )),
}));

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe('<AgentPage>', () => {
  beforeEach(() => {
    capturedSessionPanelProps = null;
    Object.assign(mockHookReturn, {
      messages: [],
      assumptions: [],
      status: 'idle',
      warningMessage: null,
      compactBanner: null,
      processExitInfo: null,
      clearProcessExit: vi.fn(),
      turnAbortedInfo: null,
      clearTurnAborted: vi.fn(),
      subagentCount: 0,
      tokenUsage: { input: 0, output: 0, total: 0 },
      elapsedMs: 0,
      turnStartTimestamp: null,
      isConnected: false,
      isProcessing: false,
      sendMessage: vi.fn(),
      stopAgent: vi.fn(),
      interruptAgent: vi.fn(),
      notebookReady: false,
    });
    useAgentSession.mockImplementation(() => mockHookReturn);
  });

  afterEach(cleanup);

  it('renders with no session selected — shows session, assumptions, and tab panels', () => {
    render(<AgentPage />);
    expect(screen.getByTestId('session-panel')).toBeTruthy();
    expect(screen.getByTestId('assumptions-panel')).toBeTruthy();
    // Chat tab is default — ChatPanel visible, NotebookPanel not
    expect(screen.getByTestId('chat-panel')).toBeTruthy();
    expect(screen.queryByTestId('notebook-panel')).toBeNull();
  });

  it('does not connect WebSocket when no session is selected', () => {
    render(<AgentPage />);
    expect(useAgentSession).toHaveBeenCalledWith(null);
  });

  it('passes selectedId prop to SessionPanel', () => {
    render(<AgentPage />);
    // Initially, selectedId is null
    expect(capturedSessionPanelProps).not.toBeNull();
    expect(capturedSessionPanelProps.selectedId).toBeNull();
    // onSelect is a function
    expect(typeof capturedSessionPanelProps.onSelect).toBe('function');
  });

  it('tab switching — Chat to Notebook and back', () => {
    render(<AgentPage />);

    // Chat visible by default
    expect(screen.getByTestId('chat-panel')).toBeTruthy();
    expect(screen.queryByTestId('notebook-panel')).toBeNull();

    // Click Notebook tab
    fireEvent.click(screen.getByRole('button', { name: /notebook/i }));
    expect(screen.queryByTestId('chat-panel')).toBeNull();
    expect(screen.getByTestId('notebook-panel')).toBeTruthy();

    // Click Chat tab
    fireEvent.click(screen.getByRole('button', { name: /chat/i }));
    expect(screen.getByTestId('chat-panel')).toBeTruthy();
    expect(screen.queryByTestId('notebook-panel')).toBeNull();
  });

  it('shows status badge when status is non-idle', () => {
    mockHookReturn.status = 'thinking';
    render(<AgentPage />);
    expect(screen.getByText('thinking')).toBeTruthy();
  });

  it('does not show status badge when idle', () => {
    mockHookReturn.status = 'idle';
    render(<AgentPage />);
    expect(screen.queryByText('idle')).toBeNull();
  });

  it('passes assumptions from hook to AssumptionsPanel', () => {
    mockHookReturn.assumptions = [
      { field: 'db', value: 'prod', source: 'inferred', confidence: 'high', group: 'General' },
    ];
    render(<AgentPage />);
    // Our mock renders the count as data attribute
    expect(screen.getByTestId('assumptions-panel').dataset.count).toBe('1');
  });

  it('passes isConnected to ChatPanel', () => {
    mockHookReturn.isConnected = true;
    render(<AgentPage />);
    expect(screen.getByTestId('chat-panel').dataset.connected).toBe('true');
  });

  /* ---------- Runtime visibility (Issues 9-12) ---------- */

  it('does not render subagent badge when count is 0', () => {
    mockHookReturn.subagentCount = 0;
    render(<AgentPage />);
    expect(screen.queryByTestId('subagent-badge')).toBeNull();
  });

  it('renders subagent badge with singular copy when count is 1', () => {
    mockHookReturn.subagentCount = 1;
    render(<AgentPage />);
    const badge = screen.getByTestId('subagent-badge');
    expect(badge.textContent).toBe('1 subagent running');
  });

  it('renders subagent badge with plural copy when count > 1', () => {
    mockHookReturn.subagentCount = 3;
    render(<AgentPage />);
    expect(screen.getByTestId('subagent-badge').textContent).toBe('3 subagents running');
  });

  it('does not render token footer when total is 0', () => {
    mockHookReturn.tokenUsage = { input: 0, output: 0, total: 0 };
    render(<AgentPage />);
    expect(screen.queryByTestId('token-footer')).toBeNull();
  });

  it('renders token footer when total > 0 with humanized values', () => {
    mockHookReturn.tokenUsage = { input: 12345, output: 4700, total: 17045 };
    render(<AgentPage />);
    const footer = screen.getByTestId('token-footer');
    // 12345 → 12.3k; 4700 → 4.7k
    expect(footer.textContent).toContain('12.3k');
    expect(footer.textContent).toContain('4.7k');
  });

  it('renders elapsed badge while processing with formatted seconds', () => {
    mockHookReturn.isProcessing = true;
    mockHookReturn.elapsedMs = 12_000;
    render(<AgentPage />);
    expect(screen.getByTestId('elapsed-badge').textContent).toBe('Working for 12s');
  });

  it('renders elapsed badge with minutes-and-seconds format', () => {
    mockHookReturn.isProcessing = true;
    mockHookReturn.elapsedMs = 83_000; // 1m 23s
    render(<AgentPage />);
    expect(screen.getByTestId('elapsed-badge').textContent).toBe('Working for 1m 23s');
  });

  it('hides elapsed badge when not processing', () => {
    mockHookReturn.isProcessing = false;
    mockHookReturn.elapsedMs = 12_000;
    render(<AgentPage />);
    expect(screen.queryByTestId('elapsed-badge')).toBeNull();
  });

  it('hides elapsed badge when elapsedMs is 0 (turn just started)', () => {
    // Avoid flicker on the very first frame before the ticker has run.
    mockHookReturn.isProcessing = true;
    mockHookReturn.elapsedMs = 0;
    render(<AgentPage />);
    expect(screen.queryByTestId('elapsed-badge')).toBeNull();
  });

  it('renders turn-aborted banner when turnAbortedInfo is set', () => {
    mockHookReturn.turnAbortedInfo = { reason: 'ws_disconnect', hadPartialContent: true };
    render(<AgentPage />);
    const banner = screen.getByTestId('turn-aborted-banner');
    expect(banner.textContent).toContain('Connection dropped during agent reply');
    expect(banner.textContent).toContain('partial response saved');
  });

  it('does not render turn-aborted banner when turnAbortedInfo is null', () => {
    mockHookReturn.turnAbortedInfo = null;
    render(<AgentPage />);
    expect(screen.queryByTestId('turn-aborted-banner')).toBeNull();
  });

  it('turn-aborted banner dismiss button calls clearTurnAborted', () => {
    const clearFn = vi.fn();
    mockHookReturn.turnAbortedInfo = { reason: 'ws_disconnect', hadPartialContent: false };
    mockHookReturn.clearTurnAborted = clearFn;
    render(<AgentPage />);
    const banner = screen.getByTestId('turn-aborted-banner');
    const dismissBtn = banner.querySelector('button');
    fireEvent.click(dismissBtn);
    expect(clearFn).toHaveBeenCalledTimes(1);
  });
});

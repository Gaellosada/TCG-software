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
  isConnected: false,
  sendMessage: vi.fn(),
  notebookReady: false,
};

vi.mock('../../hooks/useAgentSession', () => ({
  default: vi.fn(() => mockHookReturn),
}));

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
      isConnected: false,
      sendMessage: vi.fn(),
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
});

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react';
import AgentPage, { formatAgo } from './AgentPage';

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
  lastTurnComplete: null,
  isConnected: false,
  isProcessing: false,
  sendMessage: vi.fn(),
  stopAgent: vi.fn(),
  interruptAgent: vi.fn(),
  notebookReady: false,
  autoContinueInfo: null,
  autoContinueCapped: null,
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
      lastTurnComplete: null,
      isConnected: false,
      isProcessing: false,
      sendMessage: vi.fn(),
      stopAgent: vi.fn(),
      interruptAgent: vi.fn(),
      notebookReady: false,
      autoContinueInfo: null,
      autoContinueCapped: null,
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

  it('tab switching — Chat to Notebook and back (when notebookReady=true)', () => {
    // Notebook tab is only clickable when notebookReady is true (Issue 22).
    mockHookReturn.notebookReady = true;
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

  /* ---------- turn_complete indicator (Issue 16b) ---------- */

  it('does not render turn-complete badge when lastTurnComplete is null', () => {
    mockHookReturn.lastTurnComplete = null;
    mockHookReturn.isProcessing = false;
    render(<AgentPage />);
    expect(screen.queryByTestId('turn-complete-badge')).toBeNull();
  });

  it('renders turn-complete badge when lastTurnComplete is set and not processing', () => {
    mockHookReturn.lastTurnComplete = {
      at: new Date('2026-05-07T12:00:00Z'),
      elapsedSeconds: 83.4,
    };
    mockHookReturn.isProcessing = false;
    render(<AgentPage />);
    const badge = screen.getByTestId('turn-complete-badge');
    expect(badge).toBeTruthy();
    // Text includes "Turn complete" and formatted seconds (83.4s → "1m 23s")
    expect(badge.textContent).toContain('Turn complete');
    expect(badge.textContent).toContain('1m 23s');
  });

  it('renders turn-complete badge with short format for sub-60s turns', () => {
    mockHookReturn.lastTurnComplete = {
      at: new Date('2026-05-07T12:00:00Z'),
      elapsedSeconds: 12,
    };
    mockHookReturn.isProcessing = false;
    render(<AgentPage />);
    const badge = screen.getByTestId('turn-complete-badge');
    expect(badge.textContent).toContain('12s');
  });

  it('hides turn-complete badge while a new turn is processing', () => {
    mockHookReturn.lastTurnComplete = {
      at: new Date('2026-05-07T12:00:00Z'),
      elapsedSeconds: 5,
    };
    // Simulates new turn started after prior turn_complete
    mockHookReturn.isProcessing = true;
    render(<AgentPage />);
    // Badge should not show while processing
    expect(screen.queryByTestId('turn-complete-badge')).toBeNull();
  });

  it('renders turn-complete footer when lastTurnComplete is set and not processing', () => {
    mockHookReturn.lastTurnComplete = {
      at: new Date('2026-05-07T12:34:56Z'),
      elapsedSeconds: 10,
    };
    mockHookReturn.isProcessing = false;
    render(<AgentPage />);
    const footer = screen.getByTestId('turn-complete-footer');
    expect(footer).toBeTruthy();
    expect(footer.textContent).toContain('Last turn:');
  });

  it('does not render turn-complete footer when lastTurnComplete is null', () => {
    mockHookReturn.lastTurnComplete = null;
    mockHookReturn.tokenUsage = { input: 0, output: 0, total: 0 };
    mockHookReturn.isProcessing = false;
    render(<AgentPage />);
    expect(screen.queryByTestId('turn-complete-footer')).toBeNull();
  });

  it('token footer renders with both turn-complete and token usage', () => {
    mockHookReturn.lastTurnComplete = {
      at: new Date('2026-05-07T12:34:56Z'),
      elapsedSeconds: 10,
    };
    mockHookReturn.tokenUsage = { input: 5000, output: 2000, total: 7000 };
    mockHookReturn.isProcessing = false;
    render(<AgentPage />);
    const footer = screen.getByTestId('token-footer');
    expect(footer.textContent).toContain('Last turn:');
    expect(footer.textContent).toContain('5.0k');
    expect(footer.textContent).toContain('2.0k');
  });

  /* ---------- R5 follow-up: M1/M2/S1/S2/S3/S4 fixes (F-fe-fixes) ---------- */

  describe('M1 — badge enters visible (useLayoutEffect fires before paint)', () => {
    it('badge has Visible class immediately on first render with lastTurnComplete set', () => {
      vi.useFakeTimers();
      mockHookReturn.lastTurnComplete = {
        at: new Date('2026-05-07T14:00:00Z'),
        elapsedSeconds: 10,
      };
      mockHookReturn.isProcessing = false;
      // useLayoutEffect fires synchronously inside act() before paint in JSDOM.
      act(() => {
        render(<AgentPage />);
      });
      const badge = screen.getByTestId('turn-complete-badge');
      // Class name contains the CSS module-hashed suffix; check the base name portion.
      expect(badge.className).toMatch(/TurnCompleteVisible/);
      expect(badge.className).not.toMatch(/TurnCompleteFaded/);
      vi.useRealTimers();
    });

    it('badge transitions to Faded class after 3s (TURN_COMPLETE_VISIBLE_MS)', () => {
      vi.useFakeTimers();
      mockHookReturn.lastTurnComplete = {
        at: new Date('2026-05-07T14:00:00Z'),
        elapsedSeconds: 10,
      };
      mockHookReturn.isProcessing = false;
      act(() => {
        render(<AgentPage />);
      });
      // Advance past the 3000ms timeout.
      act(() => {
        vi.advanceTimersByTime(3100);
      });
      const badge = screen.getByTestId('turn-complete-badge');
      expect(badge.className).toMatch(/TurnCompleteFaded/);
      expect(badge.className).not.toMatch(/TurnCompleteVisible/);
      vi.useRealTimers();
    });
  });

  describe('M2 — aria-live region on badge', () => {
    it('turn-complete badge has role="status", aria-live="polite", aria-atomic="true"', () => {
      mockHookReturn.lastTurnComplete = {
        at: new Date('2026-05-07T14:00:00Z'),
        elapsedSeconds: 10,
      };
      mockHookReturn.isProcessing = false;
      render(<AgentPage />);
      const badge = screen.getByTestId('turn-complete-badge');
      expect(badge.getAttribute('role')).toBe('status');
      expect(badge.getAttribute('aria-live')).toBe('polite');
      expect(badge.getAttribute('aria-atomic')).toBe('true');
    });
  });

  describe('S1 — faded badge does not use the 0.35 low-contrast opacity', () => {
    it('Faded class is applied after timeout and is not the old 35% value (CSS verified via class presence)', () => {
      // The actual opacity value (0.7 vs 0.35) is in the CSS module which
      // JSDOM does not evaluate. We verify the correct class is applied and
      // trust the CSS change in AgentPage.module.css.
      vi.useFakeTimers();
      mockHookReturn.lastTurnComplete = {
        at: new Date('2026-05-07T14:00:00Z'),
        elapsedSeconds: 10,
      };
      mockHookReturn.isProcessing = false;
      act(() => { render(<AgentPage />); });
      act(() => { vi.advanceTimersByTime(3100); });
      const badge = screen.getByTestId('turn-complete-badge');
      // Correct faded class applied (opacity: 0.7 in CSS, not 0.35).
      expect(badge.className).toMatch(/TurnCompleteFaded/);
      vi.useRealTimers();
    });
  });

  describe('S2 — footer time format is 24h', () => {
    it('footer displays time with no AM/PM for a noon timestamp', () => {
      // Use a UTC timestamp that is noon UTC. toLocaleTimeString with hour12:false
      // must produce "12:00:00" not "12:00:00 PM".
      const noonUTC = new Date('2026-05-07T12:00:00Z');
      mockHookReturn.lastTurnComplete = { at: noonUTC, elapsedSeconds: 5 };
      mockHookReturn.isProcessing = false;
      render(<AgentPage />);
      const footer = screen.getByTestId('turn-complete-footer');
      // Must not contain AM or PM.
      expect(footer.textContent).not.toMatch(/AM|PM/i);
      expect(footer.textContent).toContain('Last turn:');
    });

    it('footer displays time with no AM/PM for a morning timestamp', () => {
      const morningUTC = new Date('2026-05-07T08:30:15Z');
      mockHookReturn.lastTurnComplete = { at: morningUTC, elapsedSeconds: 5 };
      mockHookReturn.isProcessing = false;
      render(<AgentPage />);
      const footer = screen.getByTestId('turn-complete-footer');
      expect(footer.textContent).not.toMatch(/AM|PM/i);
    });
  });

  describe('S4 — persistent footer relative-time staleness indicator', () => {
    it('no relative time appended when gap < 1h', () => {
      // Timestamp 30 minutes ago — under threshold.
      const thirtyMinAgo = new Date(Date.now() - 30 * 60 * 1000);
      mockHookReturn.lastTurnComplete = { at: thirtyMinAgo, elapsedSeconds: 5 };
      mockHookReturn.isProcessing = false;
      render(<AgentPage />);
      const footer = screen.getByTestId('turn-complete-footer');
      expect(footer.textContent).not.toMatch(/ago/);
    });

    it('appends "(Xh ago)" when gap >= 1h', () => {
      // Timestamp exactly 2h ago — over threshold.
      const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000);
      mockHookReturn.lastTurnComplete = { at: twoHoursAgo, elapsedSeconds: 5 };
      mockHookReturn.isProcessing = false;
      render(<AgentPage />);
      const footer = screen.getByTestId('turn-complete-footer');
      expect(footer.textContent).toMatch(/2h ago/);
    });

    it('appends "(1h ago)" when gap is 90 minutes (floor to hours)', () => {
      // formatAgo is exported from AgentPage for direct testing.
      // formatAgo uses Math.floor(diffMin/60): 90min → floor(1.5) = 1 → "1h ago".
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-05-07T14:00:00Z'));
      const ninetyMinAgo = new Date('2026-05-07T12:30:00Z');
      expect(formatAgo(ninetyMinAgo)).toBe(' (1h ago)');
      vi.useRealTimers();
    });

    it('formatAgo returns empty string for gap < 1h', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-05-07T14:00:00Z'));
      const fortyMinAgo = new Date('2026-05-07T13:20:00Z');
      expect(formatAgo(fortyMinAgo)).toBe('');
      vi.useRealTimers();
    });

    it('formatAgo returns " (2h ago)" for exactly 2h gap', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-05-07T14:00:00Z'));
      const twoHoursAgo = new Date('2026-05-07T12:00:00Z');
      expect(formatAgo(twoHoursAgo)).toBe(' (2h ago)');
      vi.useRealTimers();
    });
  });

  /* ---------- Issue 22 — notebook tab affordance ---------- */

  describe('Issue 22 — notebook tab disabled/enabled state', () => {
    it('notebook tab is disabled when notebookReady is false', () => {
      mockHookReturn.notebookReady = false;
      render(<AgentPage />);
      const notebookBtn = screen.getByRole('button', { name: /notebook/i });
      expect(notebookBtn.disabled).toBe(true);
      expect(notebookBtn.getAttribute('aria-disabled')).toBe('true');
    });

    it('notebook tab is not disabled when notebookReady is true', () => {
      mockHookReturn.notebookReady = true;
      render(<AgentPage />);
      const notebookBtn = screen.getByRole('button', { name: /notebook/i });
      expect(notebookBtn.disabled).toBe(false);
    });

    it('disabled notebook tab click does not switch panel', () => {
      mockHookReturn.notebookReady = false;
      render(<AgentPage />);
      fireEvent.click(screen.getByRole('button', { name: /notebook/i }));
      // Chat panel should still be visible
      expect(screen.getByTestId('chat-panel')).toBeTruthy();
      expect(screen.queryByTestId('notebook-panel')).toBeNull();
    });

    it('disabled notebook tab has title tooltip', () => {
      mockHookReturn.notebookReady = false;
      render(<AgentPage />);
      const notebookBtn = screen.getByRole('button', { name: /notebook/i });
      expect(notebookBtn.title).toBeTruthy();
      expect(notebookBtn.title.toLowerCase()).toContain('no notebook');
    });

    it('notebook tab has no title tooltip when notebookReady is true', () => {
      mockHookReturn.notebookReady = true;
      render(<AgentPage />);
      const notebookBtn = screen.getByRole('button', { name: /notebook/i });
      expect(notebookBtn.title).toBeFalsy();
    });

    it('shows notebook indicator dot when notebook is ready (S1 Issue 22 coverage)', () => {
      mockHookReturn.notebookReady = true;
      render(<AgentPage />);
      // The tabNotebookDot span is rendered inside the Notebook button when ready.
      // We locate it by the aria-hidden attribute used on the indicator dot.
      const notebookBtn = screen.getByRole('button', { name: /notebook/i });
      const dot = notebookBtn.querySelector('[aria-hidden="true"]');
      expect(dot).not.toBeNull();
    });

    it('transitions notebook tab from disabled to enabled when notebookReady changes (S1 Issue 22 coverage)', () => {
      mockHookReturn.notebookReady = false;
      const { rerender } = render(<AgentPage />);
      const notebookBtn = screen.getByRole('button', { name: /notebook/i });
      expect(notebookBtn.disabled).toBe(true);

      // Simulate notebookReady becoming true (e.g. WS event or probe response)
      mockHookReturn.notebookReady = true;
      rerender(<AgentPage />);
      expect(notebookBtn.disabled).toBe(false);
    });
  });

  /* ---------- Issue 23 — auto-continue badge ---------- */

  describe('Issue 23 — auto-continue badge', () => {
    it('auto-continue badge is not shown when autoContinueInfo is null', () => {
      mockHookReturn.autoContinueInfo = null;
      render(<AgentPage />);
      expect(screen.queryByTestId('auto-continue-badge')).toBeNull();
    });

    it('auto-continue badge shows "Continuing… (N/5)" when autoContinueInfo is set', () => {
      mockHookReturn.autoContinueInfo = { iter: 2, max: 5, reason: 'missing_done_marker' };
      mockHookReturn.autoContinueCapped = null;
      render(<AgentPage />);
      const badge = screen.getByTestId('auto-continue-badge');
      expect(badge.textContent).toContain('Continuing…');
      expect(badge.textContent).toContain('2/5');
    });

    it('auto-continue badge has aria-live="polite" (G-INVAR row 13)', () => {
      mockHookReturn.autoContinueInfo = { iter: 1, max: 5, reason: 'missing_done_marker' };
      render(<AgentPage />);
      const badge = screen.getByTestId('auto-continue-badge');
      expect(badge.getAttribute('aria-live')).toBe('polite');
      expect(badge.getAttribute('role')).toBe('status');
    });

    it('auto-continue badge has tooltip with human-readable reason for missing_done_marker', () => {
      mockHookReturn.autoContinueInfo = { iter: 1, max: 5, reason: 'missing_done_marker' };
      render(<AgentPage />);
      const badge = screen.getByTestId('auto-continue-badge');
      expect(badge.title).toContain("didn't signal completion");
    });

    it('auto-continue badge has tooltip with human-readable reason for unmet_intent', () => {
      mockHookReturn.autoContinueInfo = { iter: 1, max: 5, reason: 'unmet_intent' };
      render(<AgentPage />);
      const badge = screen.getByTestId('auto-continue-badge');
      expect(badge.title).toContain("announced work that wasn't done");
    });

    it('auto-continue badge is hidden when autoContinueCapped is set (capped badge replaces it)', () => {
      mockHookReturn.autoContinueInfo = null;
      // S2 fix: autoContinueCapped is now {iter, max} not boolean
      mockHookReturn.autoContinueCapped = { iter: 5, max: 5 };
      render(<AgentPage />);
      // auto-continue badge should not show (capped badge replaces it)
      expect(screen.queryByTestId('auto-continue-badge')).toBeNull();
    });

    it('capped badge shows warning text and dynamic max when autoContinueCapped is set', () => {
      // S2 fix: cap badge displays autoContinueCapped.max (dynamic, not hardcoded 5)
      mockHookReturn.autoContinueCapped = { iter: 2, max: 2 };
      mockHookReturn.autoContinueInfo = null;
      render(<AgentPage />);
      const badge = screen.getByTestId('auto-continue-capped-badge');
      expect(badge.textContent).toContain('may be incomplete');
      // Verify the dynamic max (2) is shown, not the hardcoded fallback (5)
      expect(badge.textContent).toContain('2×');
    });

    it('capped badge has aria-live="polite"', () => {
      mockHookReturn.autoContinueCapped = { iter: 5, max: 5 };
      render(<AgentPage />);
      const badge = screen.getByTestId('auto-continue-capped-badge');
      expect(badge.getAttribute('aria-live')).toBe('polite');
    });

    it('turn-complete badge is hidden while autoContinueInfo is set (mutually exclusive)', () => {
      mockHookReturn.lastTurnComplete = {
        at: new Date('2026-05-07T12:00:00Z'),
        elapsedSeconds: 10,
      };
      mockHookReturn.isProcessing = false;
      mockHookReturn.autoContinueInfo = { iter: 1, max: 5, reason: 'missing_done_marker' };
      render(<AgentPage />);
      // turn-complete badge must not show during auto-continue loop
      expect(screen.queryByTestId('turn-complete-badge')).toBeNull();
      // auto-continue badge must show
      expect(screen.getByTestId('auto-continue-badge')).toBeTruthy();
    });
  });
});

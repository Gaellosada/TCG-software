// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import SessionPanel from './SessionPanel';

// ---------------------------------------------------------------------------
// Mock the agent API module
// ---------------------------------------------------------------------------
vi.mock('../../api/agent', () => ({
  listSessions: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
}));

import { listSessions, createSession, deleteSession } from '../../api/agent';

const MOCK_SESSIONS = [
  { id: 'sess-1', name: 'SPX SMA Backtest', created_at: '2026-05-04T12:00:00Z' },
  { id: 'sess-2', name: 'VIX Put Strategy', created_at: '2026-05-03T10:00:00Z' },
];

describe('<SessionPanel>', () => {
  beforeEach(() => {
    listSessions.mockResolvedValue(MOCK_SESSIONS);
    createSession.mockResolvedValue({ id: 'new-id', name: 'New Session', created_at: '2026-05-04T14:00:00Z' });
    deleteSession.mockResolvedValue({ status: 'deleted' });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('renders session list from API', async () => {
    render(<SessionPanel selectedId={null} onSelect={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText('SPX SMA Backtest')).toBeTruthy();
    });
    expect(screen.getByText('VIX Put Strategy')).toBeTruthy();
  });

  it('shows loading state initially', () => {
    // Make listSessions hang
    listSessions.mockReturnValue(new Promise(() => {}));
    render(<SessionPanel selectedId={null} onSelect={vi.fn()} />);
    expect(screen.getByText('Loading...')).toBeTruthy();
  });

  it('shows empty state when no sessions', async () => {
    listSessions.mockResolvedValue([]);
    render(<SessionPanel selectedId={null} onSelect={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText('No sessions yet')).toBeTruthy();
    });
  });

  it('calls onSelect when a session row is clicked', async () => {
    const onSelect = vi.fn();
    render(<SessionPanel selectedId={null} onSelect={onSelect} />);
    await waitFor(() => {
      expect(screen.getByText('SPX SMA Backtest')).toBeTruthy();
    });
    fireEvent.click(screen.getByText('SPX SMA Backtest'));
    expect(onSelect).toHaveBeenCalledWith('sess-1');
  });

  it('creates a new session and selects it', async () => {
    const onSelect = vi.fn();
    render(<SessionPanel selectedId={null} onSelect={onSelect} />);
    await waitFor(() => {
      expect(screen.getByText('SPX SMA Backtest')).toBeTruthy();
    });

    fireEvent.click(screen.getByRole('button', { name: /new session/i }));

    await waitFor(() => {
      expect(createSession).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(onSelect).toHaveBeenCalledWith('new-id');
    });
  });

  it('deletes a session and deselects if it was selected', async () => {
    const onSelect = vi.fn();
    render(<SessionPanel selectedId="sess-1" onSelect={onSelect} />);
    await waitFor(() => {
      expect(screen.getByText('SPX SMA Backtest')).toBeTruthy();
    });

    // Click the delete button (×) for the first session
    const deleteBtn = screen.getByRole('button', { name: /delete session spx/i });
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(deleteSession).toHaveBeenCalledWith('sess-1');
    });
    // Should clear selection since deleted session was selected
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it('shows error on fetch failure', async () => {
    listSessions.mockRejectedValue(new Error('Network error'));
    render(<SessionPanel selectedId={null} onSelect={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText('Network error')).toBeTruthy();
    });
  });
});

// @vitest-environment jsdom
//
// TicketsPage tests. The global test setup (src/test/setup.js) auto-wraps
// every RTL render in a fresh QueryClientProvider with retry off, so a bare
// render() works and useTicketsList resolves against the mocked api below.
//
// We mock ../../api/tickets so no real HTTP is attempted: listTickets feeds
// the list query; create/update/delete are spies the tests assert on.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  render, screen, cleanup, act, fireEvent, waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const mockListTickets = vi.fn();
const mockCreateTicket = vi.fn();
const mockUpdateTicket = vi.fn();
const mockDeleteTicket = vi.fn();

vi.mock('../../api/tickets', () => ({
  listTickets: (...a) => mockListTickets(...a),
  createTicket: (...a) => mockCreateTicket(...a),
  updateTicket: (...a) => mockUpdateTicket(...a),
  deleteTicket: (...a) => mockDeleteTicket(...a),
  // The page imports describePersistenceError from api/tickets (which
  // re-exports it from api/persistence) — provide a simple stand-in.
  describePersistenceError: (err) => (err && err.message) || String(err),
}));

import TicketsPage from './TicketsPage';

const TICKET_A = {
  id: 'tkt-a',
  text: 'Chart fails to load on the Data page',
  created_at: '2026-06-24T10:30:00Z',
};
const TICKET_B = {
  id: 'tkt-b',
  text: 'Portfolio rebalance date is off by one',
  created_at: '2026-06-23T08:00:00Z',
};

beforeEach(() => {
  mockListTickets.mockReset();
  mockCreateTicket.mockReset();
  mockUpdateTicket.mockReset();
  mockDeleteTicket.mockReset();
  // Default: list resolves with two tickets (newest-first, as the backend
  // already orders them).
  mockListTickets.mockResolvedValue([TICKET_A, TICKET_B]);
  mockCreateTicket.mockResolvedValue({ id: 'tkt-new', text: 'x', created_at: '2026-06-24T11:00:00Z' });
  mockUpdateTicket.mockResolvedValue({ ...TICKET_A, text: 'edited' });
  mockDeleteTicket.mockResolvedValue(null);
});

afterEach(() => {
  cleanup();
});

describe('<TicketsPage>', () => {
  it('mounts without runtime error', async () => {
    await act(async () => { render(<TicketsPage />); });
    expect(screen.getByTestId('ticket-list')).toBeTruthy();
  });

  it('renders the title "Tickets"', async () => {
    await act(async () => { render(<TicketsPage />); });
    expect(screen.getByText('Tickets')).toBeTruthy();
  });

  it('renders the mocked list of tickets, newest-first', async () => {
    await act(async () => { render(<TicketsPage />); });
    await waitFor(() => {
      expect(screen.getByTestId('ticket-row-tkt-a')).toBeTruthy();
    });
    expect(screen.getByText(TICKET_A.text)).toBeTruthy();
    expect(screen.getByText(TICKET_B.text)).toBeTruthy();
  });

  it('shows the empty state when there are no tickets', async () => {
    mockListTickets.mockResolvedValue([]);
    await act(async () => { render(<TicketsPage />); });
    await waitFor(() => {
      expect(screen.getByTestId('ticket-empty')).toBeTruthy();
    });
  });

  it('disables Add until text is entered, then calls createTicket with the text', async () => {
    const user = userEvent.setup();
    await act(async () => { render(<TicketsPage />); });

    const addBtn = screen.getByTestId('ticket-add-btn');
    // Disabled on empty input (immediate client-side guard). jest-dom is not
    // configured in this project, so assert the native DOM property.
    expect(addBtn.disabled).toBe(true);

    const input = screen.getByTestId('ticket-add-input');
    await act(async () => { await user.type(input, '  New problem note  '); });
    expect(addBtn.disabled).toBe(false);

    await act(async () => { await user.click(addBtn); });
    // Text is trimmed before the call.
    expect(mockCreateTicket).toHaveBeenCalledTimes(1);
    expect(mockCreateTicket).toHaveBeenCalledWith('New problem note');
  });

  it('inline-edit calls updateTicket with the edited text', async () => {
    await act(async () => { render(<TicketsPage />); });
    await waitFor(() => expect(screen.getByTestId('ticket-row-tkt-a')).toBeTruthy());

    // Open the inline editor via the edit button.
    await act(async () => {
      fireEvent.click(screen.getByTestId('ticket-edit-btn-tkt-a'));
    });
    const editInput = screen.getByTestId('ticket-edit-input-tkt-a');
    expect(editInput).toBeTruthy();

    // Change the text and commit with Cmd/Ctrl+Enter.
    await act(async () => {
      fireEvent.change(editInput, { target: { value: 'Updated ticket text' } });
      fireEvent.keyDown(editInput, { key: 'Enter', ctrlKey: true });
    });

    expect(mockUpdateTicket).toHaveBeenCalledTimes(1);
    expect(mockUpdateTicket).toHaveBeenCalledWith('tkt-a', 'Updated ticket text');
  });

  it('does NOT call updateTicket when the inline edit is unchanged', async () => {
    await act(async () => { render(<TicketsPage />); });
    await waitFor(() => expect(screen.getByTestId('ticket-row-tkt-a')).toBeTruthy());

    await act(async () => {
      fireEvent.click(screen.getByTestId('ticket-edit-btn-tkt-a'));
    });
    const editInput = screen.getByTestId('ticket-edit-input-tkt-a');
    // Commit with the same text → no-op, editor closes, no PUT.
    await act(async () => {
      fireEvent.keyDown(editInput, { key: 'Enter', ctrlKey: true });
    });
    expect(mockUpdateTicket).not.toHaveBeenCalled();
    // Editor closed → the text paragraph is back.
    expect(screen.getByTestId('ticket-text-tkt-a')).toBeTruthy();
  });

  it('delete opens the ConfirmDialog and confirming calls deleteTicket', async () => {
    await act(async () => { render(<TicketsPage />); });
    await waitFor(() => expect(screen.getByTestId('ticket-row-tkt-a')).toBeTruthy());

    // No dialog initially.
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByTestId('ticket-delete-btn-tkt-a'));
    });
    // ConfirmDialog (shared component) is now open with a permanence message.
    const dialog = screen.getByTestId('confirm-dialog');
    expect(dialog).toBeTruthy();
    expect(screen.getByText(/permanently deleted/i)).toBeTruthy();

    // deleteTicket only fires on confirm, not on open.
    expect(mockDeleteTicket).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
    });
    expect(mockDeleteTicket).toHaveBeenCalledTimes(1);
    expect(mockDeleteTicket).toHaveBeenCalledWith('tkt-a');
  });

  it('cancelling the delete dialog does NOT call deleteTicket', async () => {
    await act(async () => { render(<TicketsPage />); });
    await waitFor(() => expect(screen.getByTestId('ticket-row-tkt-a')).toBeTruthy());

    await act(async () => {
      fireEvent.click(screen.getByTestId('ticket-delete-btn-tkt-a'));
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));
    });
    expect(mockDeleteTicket).not.toHaveBeenCalled();
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();
  });

  it('surfaces a load error when listTickets rejects', async () => {
    mockListTickets.mockRejectedValue(new Error('Backend unreachable'));
    await act(async () => { render(<TicketsPage />); });
    await waitFor(() => {
      expect(screen.getByTestId('ticket-load-error')).toBeTruthy();
    });
    expect(screen.getByText(/Failed to load tickets/)).toBeTruthy();
  });
});

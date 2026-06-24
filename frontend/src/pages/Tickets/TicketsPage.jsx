import { useState, useRef, useEffect, useCallback } from 'react';
import { useMutation } from '@tanstack/react-query';
import ConfirmDialog from '../../components/ConfirmDialog';
import { formatDateTime } from '../../utils/format';
import {
  createTicket, updateTicket, deleteTicket, describePersistenceError,
} from '../../api/tickets';
import { useTicketsList, useInvalidatePersistence } from '../../hooks/persistenceQueries';
import styles from './TicketsPage.module.css';

// Mirror the backend cap (TicketCreateIn/TicketUpdateIn max_length). Enforcing
// it client-side gives immediate feedback instead of a round-trip 400.
const MAX_TICKET_LEN = 10000;

/**
 * Tickets page — a simple notebook of free-text issue notes.
 *
 * A ticket is a single text string a user records when they hit a problem.
 * Full CRUD: list (newest-first) · add · inline-edit · delete-with-confirm.
 *
 * Unlike Signals/Indicators, tickets carry no category, no lock and no
 * editor; the list IS the feature, so this page uses the canonical TanStack
 * Query model directly — ``useTicketsList`` is the source of truth and each
 * mutation invalidates that list on success (see hooks/persistenceQueries.js).
 * The inline-edit affordance mirrors SignalsList's inline rename; delete reuses
 * the shared destructive ConfirmDialog exactly as IndicatorsPage does.
 */
function TicketsPage() {
  // --- List (the persisted source of truth) --------------------------------
  const ticketsQuery = useTicketsList();
  const invalidate = useInvalidatePersistence();
  const tickets = Array.isArray(ticketsQuery.data) ? ticketsQuery.data : [];

  // --- Add form ------------------------------------------------------------
  const [draft, setDraft] = useState('');

  // --- Inline edit (mirrors SignalsList rename) ----------------------------
  const [editingId, setEditingId] = useState(null);
  const [editDraft, setEditDraft] = useState('');
  const editRef = useRef(null);

  // --- Delete confirmation (mirrors IndicatorsPage pendingDeleteId) --------
  const [pendingDeleteId, setPendingDeleteId] = useState(null);

  // One shared error message for the most recent failed mutation, surfaced via
  // describePersistenceError (same formatter the other persistence pages use).
  const [actionError, setActionError] = useState(null);

  useEffect(() => {
    if (editingId && editRef.current) {
      editRef.current.focus();
      editRef.current.select();
    }
  }, [editingId]);

  // --- Mutations -----------------------------------------------------------
  const createMut = useMutation({
    mutationFn: (text) => createTicket(text),
    onSuccess: () => {
      setActionError(null);
      setDraft('');
      invalidate.tickets();
    },
    onError: (err) => setActionError(describePersistenceError(err)),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, text }) => updateTicket(id, text),
    onSuccess: () => {
      setActionError(null);
      invalidate.tickets();
    },
    onError: (err) => setActionError(describePersistenceError(err)),
  });

  const deleteMut = useMutation({
    mutationFn: (id) => deleteTicket(id),
    onSuccess: () => {
      setActionError(null);
      invalidate.tickets();
    },
    onError: (err) => setActionError(describePersistenceError(err)),
  });

  const trimmedDraft = draft.trim();
  const canAdd = trimmedDraft.length > 0 && trimmedDraft.length <= MAX_TICKET_LEN
    && !createMut.isPending;

  const handleAdd = useCallback(() => {
    const text = draft.trim();
    if (!text || text.length > MAX_TICKET_LEN) return;
    createMut.mutate(text);
  }, [draft, createMut]);

  const startEdit = useCallback((ticket) => {
    setEditingId(ticket.id);
    setEditDraft(ticket.text || '');
  }, []);

  const cancelEdit = useCallback(() => {
    setEditingId(null);
    setEditDraft('');
  }, []);

  const commitEdit = useCallback(() => {
    if (!editingId) return;
    const id = editingId;
    const next = editDraft.trim();
    const current = tickets.find((t) => t.id === id);
    // No-op edits (blank, too long, or unchanged) just close the editor — no
    // pointless PUT, and a blank/too-long value can never reach the backend.
    if (!next || next.length > MAX_TICKET_LEN || (current && next === current.text)) {
      cancelEdit();
      return;
    }
    updateMut.mutate({ id, text: next });
    cancelEdit();
  }, [editingId, editDraft, tickets, updateMut, cancelEdit]);

  const handleConfirmDelete = useCallback(() => {
    const id = pendingDeleteId;
    setPendingDeleteId(null);
    if (!id) return;
    deleteMut.mutate(id);
  }, [pendingDeleteId, deleteMut]);

  // --- Render --------------------------------------------------------------
  const loading = ticketsQuery.isPending && ticketsQuery.fetchStatus !== 'idle';
  const loadError = ticketsQuery.error ? describePersistenceError(ticketsQuery.error) : null;

  return (
    <div className={styles.page}>
      <span className={styles.label}>WORKSPACE</span>
      <h1 className={styles.title}>Tickets</h1>
      <p className={styles.subtitle}>
        Jot down any issue you hit. Tickets are free-text notes you can add,
        edit, and permanently delete.
      </p>

      {/* Add form */}
      <div className={styles.addRow}>
        <textarea
          className={styles.addInput}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            // Cmd/Ctrl+Enter submits (a bare Enter inserts a newline, since a
            // ticket may be multi-line).
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              if (canAdd) handleAdd();
            }
          }}
          placeholder="Describe an issue you ran into…"
          rows={2}
          maxLength={MAX_TICKET_LEN}
          aria-label="New ticket text"
          data-testid="ticket-add-input"
        />
        <button
          type="button"
          className={styles.addBtn}
          onClick={handleAdd}
          disabled={!canAdd}
          title={canAdd ? 'Add ticket' : 'Type something to add a ticket'}
          data-testid="ticket-add-btn"
        >
          Add
        </button>
      </div>

      {actionError && (
        <div className={styles.errorBanner} role="alert" data-testid="ticket-action-error">
          {actionError}
        </div>
      )}

      {/* List */}
      <div className={styles.list} data-testid="ticket-list">
        {loadError ? (
          <div className={styles.empty} data-testid="ticket-load-error">
            <strong>Failed to load tickets:</strong> {loadError}
          </div>
        ) : loading ? (
          <div className={styles.empty}>Loading…</div>
        ) : tickets.length === 0 ? (
          <div className={styles.empty} data-testid="ticket-empty">No tickets yet.</div>
        ) : (
          tickets.map((ticket) => {
            const isEditing = editingId === ticket.id;
            return (
              <div
                key={ticket.id}
                className={styles.row}
                data-testid={`ticket-row-${ticket.id}`}
              >
                {isEditing ? (
                  <textarea
                    ref={editRef}
                    className={styles.editInput}
                    value={editDraft}
                    onChange={(e) => setEditDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                        e.preventDefault();
                        commitEdit();
                      } else if (e.key === 'Escape') {
                        e.preventDefault();
                        cancelEdit();
                      }
                    }}
                    onBlur={commitEdit}
                    rows={2}
                    maxLength={MAX_TICKET_LEN}
                    aria-label={`Edit ticket ${ticket.id}`}
                    data-testid={`ticket-edit-input-${ticket.id}`}
                  />
                ) : (
                  <div className={styles.rowMain}>
                    <p
                      className={styles.rowText}
                      onDoubleClick={() => startEdit(ticket)}
                      data-testid={`ticket-text-${ticket.id}`}
                    >
                      {ticket.text}
                    </p>
                    <span className={styles.rowDate}>{formatDateTime(ticket.created_at)}</span>
                  </div>
                )}
                {!isEditing && (
                  <div className={styles.rowActions}>
                    <button
                      type="button"
                      className={styles.iconBtn}
                      onClick={() => startEdit(ticket)}
                      title="Edit"
                      aria-label="Edit ticket"
                      data-testid={`ticket-edit-btn-${ticket.id}`}
                    >
                      ✎
                    </button>
                    <button
                      type="button"
                      className={styles.deleteBtn}
                      onClick={() => setPendingDeleteId(ticket.id)}
                      title="Delete"
                      aria-label="Delete ticket"
                      data-testid={`ticket-delete-btn-${ticket.id}`}
                    >
                      ×
                    </button>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      <ConfirmDialog
        open={pendingDeleteId !== null}
        title="Delete ticket?"
        message="This ticket will be permanently deleted. This cannot be undone."
        confirmLabel="Delete"
        cancelLabel="Cancel"
        destructive
        onConfirm={handleConfirmDelete}
        onCancel={() => setPendingDeleteId(null)}
      />
    </div>
  );
}

export default TicketsPage;

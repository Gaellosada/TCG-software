import { useState, useEffect, useCallback, useRef } from 'react';
import { listSessions, createSession, deleteSession, renameSession } from '../../api/agent';
import ConfirmDialog from '../../components/ConfirmDialog';
import styles from './SessionPanel.module.css';

/**
 * Panel showing the list of agent sessions with create / delete / rename / select.
 *
 * Props:
 *   selectedId  {string|null}  Currently selected session id
 *   onSelect    {Function}     (id) => void
 */
function SessionPanel({ selectedId, onSelect }) {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editingId, setEditingId] = useState(null);
  const [editingName, setEditingName] = useState('');
  const [deleteTarget, setDeleteTarget] = useState(null);
  const editInputRef = useRef(null);

  const fetchSessions = useCallback(async () => {
    try {
      const data = await listSessions();
      // Backend may return { sessions: [...] } or an array directly
      setSessions(Array.isArray(data) ? data : data.sessions || []);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to load sessions');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  // Focus the rename input when editing starts
  useEffect(() => {
    if (editingId && editInputRef.current) {
      editInputRef.current.focus();
      editInputRef.current.select();
    }
  }, [editingId]);

  async function handleCreate() {
    try {
      const result = await createSession();
      // Refresh the list and select the new session
      await fetchSessions();
      if (result && (result.id || result._id)) {
        onSelect(result.id || result._id);
      }
    } catch (err) {
      setError(err.message || 'Failed to create session');
    }
  }

  function handleDeleteClick(id, name) {
    setDeleteTarget({ id, name });
  }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return;
    const { id } = deleteTarget;
    setDeleteTarget(null);
    try {
      // Deselect first to disconnect WebSocket (kills running agent subprocess)
      if (selectedId === id) {
        onSelect(null);
      }
      await deleteSession(id);
      await fetchSessions();
    } catch (err) {
      setError(err.message || 'Failed to delete session');
    }
  }

  function startRename(id, currentName) {
    setEditingId(id);
    setEditingName(currentName);
  }

  async function commitRename() {
    if (!editingId) return;
    const trimmed = editingName.trim();
    if (trimmed) {
      try {
        await renameSession(editingId, trimmed);
        await fetchSessions();
      } catch (err) {
        setError(err.message || 'Failed to rename session');
      }
    }
    setEditingId(null);
    setEditingName('');
  }

  function handleRenameKeyDown(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      commitRename();
    } else if (e.key === 'Escape') {
      setEditingId(null);
      setEditingName('');
    }
  }

  function formatDate(dateStr) {
    if (!dateStr) return '';
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    return d.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>Sessions</span>
        <button
          type="button"
          className={styles.newBtn}
          onClick={handleCreate}
          title="New session"
          aria-label="New session"
        >
          + New
        </button>
      </div>

      {error && <div className={styles.error}>{error}</div>}

      <div className={styles.list}>
        {loading && <div className={styles.empty}>Loading...</div>}

        {!loading && sessions.length === 0 && (
          <div className={styles.empty}>No sessions yet</div>
        )}

        {sessions.map((s) => {
          const id = s.id || s._id;
          const isSelected = id === selectedId;
          const displayName = s.name || `Session ${id.slice(0, 8)}`;
          const isEditing = editingId === id;

          return (
            <div
              key={id}
              className={`${styles.row} ${isSelected ? styles.rowSelected : ''}`}
              onClick={() => !isEditing && onSelect(id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => e.key === 'Enter' && !isEditing && onSelect(id)}
            >
              <div className={styles.rowInfo}>
                {isEditing ? (
                  <input
                    ref={editInputRef}
                    className={styles.renameInput}
                    value={editingName}
                    onChange={(e) => setEditingName(e.target.value)}
                    onBlur={commitRename}
                    onKeyDown={handleRenameKeyDown}
                    onClick={(e) => e.stopPropagation()}
                    aria-label="Rename session"
                  />
                ) : (
                  <span
                    className={styles.rowName}
                    onDoubleClick={(e) => {
                      e.stopPropagation();
                      startRename(id, displayName);
                    }}
                    title="Double-click to rename"
                  >
                    {displayName}
                  </span>
                )}
                <span className={styles.rowDate}>
                  {formatDate(s.created_at || s.createdAt)}
                </span>
              </div>
              <button
                type="button"
                className={styles.deleteBtn}
                onClick={(e) => {
                  e.stopPropagation();
                  handleDeleteClick(id, displayName);
                }}
                title="Delete session"
                aria-label={`Delete session ${displayName}`}
              >
                &times;
              </button>
            </div>
          );
        })}
      </div>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete session"
        message={`Are you sure you want to delete "${deleteTarget?.name ?? ''}"? This cannot be undone.`}
        confirmLabel="Delete"
        destructive
        onConfirm={handleDeleteConfirm}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}

export default SessionPanel;

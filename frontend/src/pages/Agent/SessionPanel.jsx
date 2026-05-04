import { useState, useEffect, useCallback } from 'react';
import { listSessions, createSession, deleteSession } from '../../api/agent';
import styles from './SessionPanel.module.css';

/**
 * Panel showing the list of agent sessions with create / delete / select.
 *
 * Props:
 *   selectedId  {string|null}  Currently selected session id
 *   onSelect    {Function}     (id) => void
 */
function SessionPanel({ selectedId, onSelect }) {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

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

  async function handleDelete(id) {
    try {
      await deleteSession(id);
      if (selectedId === id) {
        onSelect(null);
      }
      await fetchSessions();
    } catch (err) {
      setError(err.message || 'Failed to delete session');
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
          return (
            <div
              key={id}
              className={`${styles.row} ${isSelected ? styles.rowSelected : ''}`}
              onClick={() => onSelect(id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => e.key === 'Enter' && onSelect(id)}
            >
              <div className={styles.rowInfo}>
                <span className={styles.rowName}>
                  {s.name || `Session ${id.slice(0, 8)}`}
                </span>
                <span className={styles.rowDate}>
                  {formatDate(s.created_at || s.createdAt)}
                </span>
              </div>
              <button
                type="button"
                className={styles.deleteBtn}
                onClick={(e) => {
                  e.stopPropagation();
                  handleDelete(id);
                }}
                title="Delete session"
                aria-label={`Delete session ${s.name || id}`}
              >
                &times;
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default SessionPanel;

import { useState, useRef, useEffect } from 'react';
import styles from './Signals.module.css';

/**
 * Left panel — searchable list of signals. Simpler than the Indicators
 * list because signals have no read-only defaults; there is exactly one
 * section (custom) with a ``+ New`` button in the header.
 *
 * Props match the Indicators list for consistency:
 *   signals          {Array}    [{id, name}, ...]
 *   selectedId       {string}
 *   onSelect         {Function} (id) => void
 *   onAdd            {Function} () => void
 *   onDelete         {Function} (id) => void (caller confirms)
 *   onRename         {Function} (id, newName) => void
 *   search           {string}
 *   onSearchChange   {Function} (q) => void
 */
function SignalsList({
  signals,
  selectedId,
  onSelect,
  onAdd,
  onDelete,
  onRename,
  search,
  onSearchChange,
}) {
  const [renamingId, setRenamingId] = useState(null);
  const [renameDraft, setRenameDraft] = useState('');
  const inputRef = useRef(null);

  useEffect(() => {
    if (renamingId && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [renamingId]);

  function startRename(sig) {
    setRenamingId(sig.id);
    setRenameDraft(sig.name || '');
  }

  function commitRename() {
    if (!renamingId) return;
    const next = renameDraft.trim();
    if (next && onRename) onRename(renamingId, next);
    setRenamingId(null);
    setRenameDraft('');
  }

  function cancelRename() {
    setRenamingId(null);
    setRenameDraft('');
  }

  function renderRow(sig) {
    const isRenaming = renamingId === sig.id;
    return (
      <div
        key={sig.id}
        className={`${styles.row} ${sig.id === selectedId ? styles.rowActive : ''}`}
        onClick={() => onSelect(sig.id)}
        onDoubleClick={() => startRename(sig)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && !isRenaming && onSelect(sig.id)}
        data-testid={`signal-row-${sig.id}`}
      >
        {isRenaming ? (
          <input
            ref={inputRef}
            className={styles.renameInput}
            value={renameDraft}
            onChange={(e) => setRenameDraft(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); commitRename(); }
              else if (e.key === 'Escape') { e.preventDefault(); cancelRename(); }
            }}
            onBlur={commitRename}
            aria-label={`Rename ${sig.name}`}
          />
        ) : (
          <span className={styles.rowName}>{sig.name}</span>
        )}
        {!isRenaming && (
          <button
            type="button"
            className={styles.iconBtn}
            onClick={(e) => { e.stopPropagation(); startRename(sig); }}
            title="Rename"
            aria-label={`Rename ${sig.name}`}
          >
            ✎
          </button>
        )}
        {!isRenaming && (
          <button
            type="button"
            className={styles.deleteBtn}
            onClick={(e) => { e.stopPropagation(); onDelete(sig.id); }}
            title="Delete"
            aria-label={`Delete ${sig.name}`}
          >
            ×
          </button>
        )}
      </div>
    );
  }

  return (
    <div className={styles.listPanelBody}>
      <div className={styles.listHeader}>
        <span className={styles.listTitle}>Signals</span>
        <button
          type="button"
          className={styles.addBtn}
          onClick={onAdd}
          title="New signal"
          aria-label="New signal"
          data-testid="add-signal-btn"
        >
          + New
        </button>
      </div>
      <div className={styles.listSearchRow}>
        <input
          className={styles.search}
          type="text"
          placeholder="Search signals..."
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          aria-label="Search signals"
        />
      </div>
      <div className={styles.listBody}>
        {signals.length === 0 ? (
          <div className={styles.listEmpty}>
            No signals yet — click + New to create one.
          </div>
        ) : (
          signals.map(renderRow)
        )}
      </div>
    </div>
  );
}

export default SignalsList;

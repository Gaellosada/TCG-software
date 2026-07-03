import { useState, useRef, useEffect } from 'react';
import styles from './Signals.module.css';
import { CATEGORIES } from '../../api/persistence';
import LockToggle from '../../components/LockToggle';

/**
 * Left panel — searchable list of signals. Simpler than the Indicators
 * list because signals have no read-only defaults; there is exactly one
 * section (custom) with a ``+ New`` button in the header.
 *
 * Each row carries a shared ``LockToggle``. When a signal is locked its
 * rename pencil, category dropdown and delete (×) are disabled (greyed);
 * only the LockToggle stays active so the user can unlock it.
 *
 * Props match the Indicators list for consistency:
 *   signals            {Array}    [{id, name, category?, locked?}, ...]
 *   selectedId         {string}
 *   onSelect           {Function} (id) => void
 *   onAdd              {Function} () => void
 *   onDelete           {Function} (id) => void (caller confirms)
 *   onRename           {Function} (id, newName) => void
 *   search             {string}
 *   onSearchChange     {Function} (q) => void
 *   category           {string}   currently selected category (one of CATEGORIES)
 *   onCategoryChange   {Function} (cat) => void
 *   onChangeItemCat    {Function} (id, newCat) => void — move item to a different category
 *   onSetSignalLocked  {Function} (id, nextBool) => void — toggle lock state
 *   loading            {boolean}  show a loading hint in the list
 */
function SignalsList({
  signals,
  selectedId,
  onSelect,
  onAdd,
  onDelete,
  onDuplicate,
  onRename,
  search,
  onSearchChange,
  category,
  onCategoryChange,
  onChangeItemCat,
  onSetSignalLocked,
  loading,
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
    const locked = !!sig.locked;
    return (
      <div
        key={sig.id}
        className={`${styles.row} ${sig.id === selectedId ? styles.rowActive : ''}`}
        onClick={() => onSelect(sig.id)}
        onDoubleClick={() => { if (!locked) startRename(sig); }}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && !isRenaming && onSelect(sig.id)}
        data-testid={`signal-row-${sig.id}`}
        data-locked={locked ? 'true' : 'false'}
      >
        {/* Lock toggle is the FIRST child so the padlock sits at the row's
            left edge. It stays interactive even when locked — it's the only
            way back to an editable state. Shared component (UI consistency). */}
        {!isRenaming && onSetSignalLocked && (
          <LockToggle
            locked={locked}
            entityLabel="signal"
            onSetLocked={(next) => onSetSignalLocked(sig.id, next)}
          />
        )}
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
        {/* Hover/focus action cluster. Wrapped in .rowActions which collapses
            to zero width at rest (so the name spans the full row, no premature
            ellipsis) and expands on :hover / :focus-within. */}
        {!isRenaming && (
          <div className={styles.rowActions}>
            <button
              type="button"
              className={styles.iconBtn}
              onClick={(e) => { e.stopPropagation(); if (!locked) startRename(sig); }}
              title={locked ? 'Locked — unlock to rename' : 'Rename'}
              aria-label={`Rename ${sig.name}`}
              disabled={locked}
            >
              ✎
            </button>
            {/* Duplicate is allowed even on a LOCKED signal — cloning doesn't
                mutate the original, and the copy always lands unlocked. */}
            {onDuplicate && (
              <button
                type="button"
                className={styles.iconBtn}
                onClick={(e) => { e.stopPropagation(); onDuplicate(sig.id); }}
                title="Duplicate"
                aria-label={`Duplicate ${sig.name}`}
                data-testid={`signal-duplicate-${sig.id}`}
              >
                ⧉
              </button>
            )}
            {onChangeItemCat && (
              <select
                className={styles.categoryChipSelect}
                value={sig.category || category || 'RESEARCH'}
                onClick={(e) => e.stopPropagation()}
                onChange={(e) => {
                  e.stopPropagation();
                  onChangeItemCat(sig.id, e.target.value);
                }}
                aria-label={`Category for ${sig.name}`}
                data-testid={`signal-cat-select-${sig.id}`}
                title={locked ? 'Locked — unlock to move' : 'Move to category'}
                disabled={locked}
              >
                {CATEGORIES.map((cat) => (
                  <option key={cat} value={cat}>{cat}</option>
                ))}
              </select>
            )}
            <button
              type="button"
              className={styles.deleteBtn}
              onClick={(e) => { e.stopPropagation(); if (!locked) onDelete(sig.id); }}
              title={locked ? 'Locked — unlock to delete' : 'Delete'}
              aria-label={`Delete ${sig.name}`}
              disabled={locked}
            >
              ×
            </button>
          </div>
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
      {onCategoryChange && (
        <div className={styles.categoryRow}>
          <label className={styles.categoryLabel} htmlFor="signals-category-select">
            Category
          </label>
          <select
            id="signals-category-select"
            className={styles.categorySelect}
            value={category || 'RESEARCH'}
            onChange={(e) => onCategoryChange(e.target.value)}
            aria-label="Filter signals by category"
            data-testid="signals-category-filter"
          >
            {CATEGORIES.map((cat) => (
              <option key={cat} value={cat}>{cat}</option>
            ))}
          </select>
        </div>
      )}
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
        {loading ? (
          <div className={styles.listEmpty}>Loading...</div>
        ) : signals.length === 0 ? (
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

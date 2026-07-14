// PersistedPortfolioPanel — panel that shows backend-persisted portfolios
// filtered by category, with category selector, + Save as new, per-row
// category chip, row-level select, and row-level archive.
//
// Styled to match the shared Card component (same container, header title,
// font sizes, row layout) used throughout the Portfolio page (HoldingsList,
// etc.). Per-row category chip and hover-reveal actions follow the same
// patterns as SignalsList.
//
// Props:
//   category              {string}   currently selected category
//   onCategoryChange      {Function} (cat) => void
//   portfolios            {Array}    [{id, name, category, ...}]
//   loading               {boolean}
//   onSaveCurrent         {Function} () => void — save current portfolio state
//   saveDisabled          {boolean}  disable the save button
//   onChangeItemCat       {Function} (id, newCat) => void
//   onArchive             {Function} (id) => void
//   selectedId            {string|null} id of the currently loaded portfolio
//   onSelect              {Function} (id) => void — load this portfolio into editor

import Card from '../../components/Card';
import LockToggle from '../../components/LockToggle';
import styles from './PersistedPortfolioPanel.module.css';
import { CATEGORIES } from '../../api/persistence';

function PersistedPortfolioPanel({
  title = 'Saved Portfolios',
  category,
  onCategoryChange,
  portfolios,
  loading,
  onSaveCurrent,
  saveDisabled,
  onChangeItemCat,
  onArchive,
  selectedId,
  onSelect,
  onSetPortfolioLocked,
  cacheEnabled = false,
  cacheStatusById = {},
}) {
  const headerRight = (
    <div className={styles.headerActions}>
      <label className={styles.categoryLabel} htmlFor="portfolio-category-select">
        Category
      </label>
      <select
        id="portfolio-category-select"
        className={styles.categorySelect}
        value={category}
        onChange={(e) => onCategoryChange(e.target.value)}
        aria-label="Filter portfolios by category"
        data-testid="portfolio-category-filter"
      >
        {CATEGORIES.map((cat) => (
          <option key={cat} value={cat}>{cat}</option>
        ))}
      </select>
      <button
        type="button"
        className={styles.addBtn}
        onClick={onSaveCurrent}
        disabled={!!saveDisabled}
        title="Save as a new portfolio in this category"
        aria-label="Save as new portfolio"
        data-testid="persist-portfolio-btn"
      >
        + Save as new
      </button>
    </div>
  );

  return (
    <Card
      title={title}
      right={headerRight}
      data-testid="persisted-portfolio-panel"
    >
      <div className={styles.list}>
        {loading ? (
          <div className={styles.empty}>Loading...</div>
        ) : portfolios.length === 0 ? (
          <div className={styles.empty} data-testid="persisted-portfolio-empty">
            No saved portfolios in {category} — click &quot;+ Save as new&quot; to add one.
          </div>
        ) : (
          portfolios.map((p) => {
            const isSelected = p.id === selectedId;
            const isLocked = !!p.locked;
            // Backend-driven proactive status: 'cached' | 'not-cached' |
            // 'checking'. Only shown when the caching toggle is on.
            const cacheStatus = cacheEnabled ? (cacheStatusById[p.id] || 'checking') : null;
            return (
              <div
                key={p.id}
                className={`${styles.row}${isSelected ? ` ${styles.rowActive}` : ''}`}
                data-testid={`persisted-portfolio-row-${p.id}`}
                data-selected={isSelected ? 'true' : 'false'}
              >
                {/* Lock toggle is the FIRST child so the padlock sits at the
                    row's left edge — same idiom as the Signals and Indicators
                    lists. Rendered for every persisted portfolio when a lock
                    handler is provided (portfolios have no readonly rows). */}
                {onSetPortfolioLocked && (
                  <LockToggle
                    entityLabel="portfolio"
                    locked={isLocked}
                    onSetLocked={(next) => onSetPortfolioLocked(p.id, next)}
                  />
                )}
                <button
                  type="button"
                  className={styles.rowName}
                  title={`Load ${p.name}`}
                  onClick={() => onSelect && onSelect(p.id)}
                  data-testid={`load-portfolio-${p.id}`}
                >
                  {p.name}
                </button>
                {/* Backend-driven per-row cache status TAG (text pill, matches
                    the active Compute-row badge). Just left of the hover-reveal
                    actions; hidden entirely when the caching toggle is off. */}
                {cacheStatus && (
                  <span
                    className={`${styles.cacheTag} ${
                      cacheStatus === 'cached'
                        ? styles.cacheTagHit
                        : cacheStatus === 'not-cached'
                          ? styles.cacheTagMiss
                          : styles.cacheTagChecking
                    }`}
                    data-testid={`portfolio-row-cache-${p.id}`}
                    data-cache-status={cacheStatus}
                    title={
                      cacheStatus === 'cached'
                        ? 'Cached — opening this portfolio and computing serves from cache'
                        : cacheStatus === 'not-cached'
                          ? 'Not cached — opening will need a Compute'
                          : 'Checking cache…'
                    }
                    aria-label={`cache ${cacheStatus}`}
                  >
                    {cacheStatus === 'cached'
                      ? 'cached'
                      : cacheStatus === 'not-cached'
                        ? 'not cached'
                        : 'checking…'}
                  </span>
                )}
                {/* Hover/focus action cluster (category chip + archive ×).
                    Wrapped in .rowActions which collapses to zero width at rest
                    so the name spans the full row (no premature ellipsis) and
                    expands on hover/focus. */}
                <div className={styles.rowActions}>
                  <select
                    className={styles.rowCatSelect}
                    value={p.category}
                    onChange={(e) => onChangeItemCat(p.id, e.target.value)}
                    aria-label={`Category for ${p.name}`}
                    data-testid={`portfolio-cat-select-${p.id}`}
                    title={isLocked ? 'Locked — unlock to move' : 'Move to category'}
                    disabled={isLocked}
                  >
                    {CATEGORIES.map((cat) => (
                      <option key={cat} value={cat}>{cat}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className={styles.rowDeleteBtn}
                    onClick={() => onArchive(p.id)}
                    title={isLocked ? 'Locked — unlock to archive' : 'Archive portfolio'}
                    aria-label={`Archive ${p.name}`}
                    data-testid={`archive-portfolio-${p.id}`}
                    disabled={isLocked}
                  >
                    ×
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </Card>
  );
}

export default PersistedPortfolioPanel;

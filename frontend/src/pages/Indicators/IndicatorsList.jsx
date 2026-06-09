import { useState, useRef, useEffect, useCallback } from 'react';
import LockToggle from '../../components/LockToggle';
import styles from './IndicatorsList.module.css';
import { LIST_COLLAPSED_KEY } from './storageKeys';

/**
 * Left panel — searchable, scrollable list of indicators.
 *
 * iter-7: when the search box is empty, indicators are grouped under two
 * section headers — DEFAULT (read-only built-ins) and CUSTOM (user-authored).
 * When the search box has text, the list is rendered flat so matches across
 * both categories surface together.
 *
 * iter-8: section headers are now interactive. Clicking a header (or
 * pressing Enter/Space on it) toggles a collapse/expand state. Collapsed
 * state is persisted in localStorage under ``tcg.indicators.listCollapsed``
 * as ``{default: bool, custom: bool}`` so it survives reloads. Default:
 * both expanded. When a section is collapsed, its items do not render —
 * only the header with a count suffix like "DEFAULT (3)". Search takes
 * precedence: while the user is typing a query we show the flat list and
 * ignore collapsed state. The ``+ New`` button in the CUSTOM header
 * remains visible even when CUSTOM is collapsed.
 *
 * Props:
 *   indicators           {Array}    list of indicator objects { id, name, readonly?, locked?, compatibleAssetTypes? }
 *   selectedId           {string}   currently selected indicator id
 *   onSelect             {Function} (id) => void
 *   onAdd                {Function} () => void
 *   onDelete             {Function} (id) => void (caller handles confirmation)
 *   onRename             {Function} (id, newName) => void
 *   onSetIndicatorLocked {Function} (id, nextBool) => void — wired to the lock API
 *   search               {string}
 *   onSearchChange       {Function} (q) => void
 *   currentAssetType     {'index'|'equity'|'option'|null}
 *                     the inferred asset_type of the currently-selected
 *                     indicator's seriesMap. When non-null, indicator
 *                     rows whose ``compatibleAssetTypes`` array does
 *                     not include this value are greyed out and given
 *                     a tooltip listing the accepted types. When
 *                     ``null`` (no asset selected, slot conflict, or
 *                     unknown collection), nothing is greyed —
 *                     full availability so the user sees every option.
 *
 * Asset-type compat is decoration only — onSelect still fires on a
 * greyed row so the user can inspect the indicator's code/docs and
 * see why it would not run. The Run button is the actual gate (see
 * ``runGate.computeAssetCompatibility``).
 */

function loadCollapsed() {
  try {
    const raw = localStorage.getItem(LIST_COLLAPSED_KEY);
    if (!raw) return { default: false, custom: false };
    const parsed = JSON.parse(raw);
    return {
      default: !!parsed.default,
      custom: !!parsed.custom,
    };
  } catch {
    return { default: false, custom: false };
  }
}

function saveCollapsed(next) {
  try { localStorage.setItem(LIST_COLLAPSED_KEY, JSON.stringify(next)); } catch { /* quota */ }
}

function IndicatorsList({
  indicators,
  selectedId,
  onSelect,
  onAdd,
  onDelete,
  onRename,
  onSetIndicatorLocked,
  search,
  onSearchChange,
  currentAssetType = null,
}) {
  const [renamingId, setRenamingId] = useState(null);
  const [renameDraft, setRenameDraft] = useState('');
  const inputRef = useRef(null);
  const [collapsed, setCollapsedState] = useState(loadCollapsed);

  useEffect(() => {
    if (renamingId && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [renamingId]);

  const toggleCollapsed = useCallback((section) => {
    setCollapsedState((prev) => {
      const next = { ...prev, [section]: !prev[section] };
      saveCollapsed(next);
      return next;
    });
  }, []);

  function startRename(ind) {
    if (ind.readonly) return;
    setRenamingId(ind.id);
    setRenameDraft(ind.name || '');
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

  const hasSearch = !!(search && search.trim());
  const defaults = indicators.filter((ind) => ind.readonly === true);
  const customs = indicators.filter((ind) => ind.readonly !== true);

  function renderRow(ind) {
    const isRenaming = renamingId === ind.id;
    // Normalised lock flag — single source for the locked-disable idiom,
    // matching SignalsList/PersistedPortfolioPanel (``const locked = !!…``).
    const locked = !!ind.locked;
    // Asset-type compat decoration. Only applies when:
    //   * the parent passed a non-null currentAssetType, AND
    //   * this indicator declares a non-empty compatibleAssetTypes
    //     array, AND
    //   * the current asset's type is not in that array, AND
    //   * the indicator does NOT have its own defaultSeries (indicators
    //     with defaultSeries are self-sufficient — they bring their own
    //     data source and don't depend on the currently selected asset).
    // A missing or empty compatibleAssetTypes means "universally
    // compatible" (back-compat for user-authored indicators) — never
    // greyed. Sign 10: the tooltip surfaces a human-readable reason.
    const compat = Array.isArray(ind.compatibleAssetTypes) ? ind.compatibleAssetTypes : null;
    const hasSelfContainedDefaults = !!(ind.defaultSeries && typeof ind.defaultSeries === 'object'
      && Object.keys(ind.defaultSeries).length > 0);
    const isIncompat = !!(
      currentAssetType
      && compat
      && compat.length > 0
      && !compat.includes(currentAssetType)
      && !hasSelfContainedDefaults
    );
    const incompatTitle = isIncompat
      ? `Not compatible with ${currentAssetType} data — accepts ${compat.join(' or ')}.`
      : undefined;
    const rowClassName = `${styles.row} ${ind.id === selectedId ? styles.rowActive : ''} ${isIncompat ? styles.rowIncompat : ''}`.trim();
    return (
      <div
        key={ind.id}
        className={rowClassName}
        data-incompat={isIncompat ? 'true' : 'false'}
        title={incompatTitle}
        onClick={() => onSelect(ind.id)}
        onDoubleClick={() => startRename(ind)}
        role="button"
        tabIndex={0}
        aria-disabled={isIncompat ? 'true' : undefined}
        onKeyDown={(e) => e.key === 'Enter' && !isRenaming && onSelect(ind.id)}
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
            aria-label={`Rename ${ind.name}`}
          />
        ) : (
          <span className={styles.rowName}>{ind.name}</span>
        )}
        {/* Show a "default" badge on readonly indicators only in search-flat
          * mode — section headers ("DEFAULT / CUSTOM") disappear during search,
          * so the badge is the only visual cue distinguishing built-ins from
          * user customs in search results. */}
        {ind.readonly && hasSearch && !isRenaming && (
          <span className={styles.defaultBadge} aria-label="default indicator">
            default
          </span>
        )}
        {!ind.readonly && !isRenaming && (
          <button
            className={styles.iconBtn}
            onClick={(e) => { e.stopPropagation(); if (!locked) startRename(ind); }}
            title={locked ? 'Locked — unlock to rename' : 'Rename'}
            aria-label={`Rename ${ind.name}`}
            disabled={locked}
          >
            ✎
          </button>
        )}
        {!ind.readonly && !isRenaming && (
          <button
            className={styles.deleteBtn}
            onClick={(e) => { e.stopPropagation(); if (!locked) onDelete(ind.id); }}
            title={locked ? 'Locked — unlock to delete' : 'Delete'}
            aria-label={`Delete ${ind.name}`}
            disabled={locked}
          >
            ×
          </button>
        )}
        {!ind.readonly && !isRenaming && onSetIndicatorLocked && (
          <LockToggle
            locked={locked}
            onSetLocked={(next) => onSetIndicatorLocked(ind.id, next)}
            entityLabel="indicator"
          />
        )}
      </div>
    );
  }

  /**
   * Render a collapsible section header.
   *
   * The header is a button (keyboard Enter/Space → toggle). A small
   * ``▸/▾`` chevron sits to the left of the label. When collapsed, a
   * count suffix ``"(N)"`` is appended to the label. Optional trailing
   * content (e.g. the ``+ New`` button) sits at the right and its click
   * handler must ``stopPropagation`` to avoid toggling the section.
   */
  function renderCategoryHeader({ section, label, count, testId, trailing }) {
    const isCollapsed = collapsed[section];
    const chevron = isCollapsed ? '▸' : '▾';
    return (
      <div
        className={styles.categoryHeader}
        data-testid={testId}
        data-collapsed={isCollapsed ? 'true' : 'false'}
        role="button"
        tabIndex={0}
        aria-expanded={!isCollapsed}
        aria-label={`${isCollapsed ? 'Expand' : 'Collapse'} ${label} section`}
        onClick={() => toggleCollapsed(section)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            toggleCollapsed(section);
          }
        }}
      >
        <span className={styles.chevron} aria-hidden="true">{chevron}</span>
        <span className={styles.categoryLabel}>
          {label}
          {isCollapsed && count !== undefined && (
            <span className={styles.categoryCountInline}> ({count})</span>
          )}
        </span>
        {!isCollapsed && count !== undefined && (
          <span className={styles.categoryCount}>{count}</span>
        )}
        {trailing}
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>Indicators</span>
      </div>
      <div className={styles.searchRow}>
        <input
          className={styles.search}
          type="text"
          placeholder="Search indicators..."
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          aria-label="Search indicators"
        />
      </div>
      <div className={styles.list}>
        {hasSearch ? (
          /* Flat list while searching — no section headers. */
          indicators.length === 0 ? (
            <div className={styles.empty}>No matches.</div>
          ) : (
            indicators.map(renderRow)
          )
        ) : (
          <>
            {/* DEFAULT section — read-only built-ins. */}
            {defaults.length > 0 && (
              <div className={styles.category} data-category="default">
                {renderCategoryHeader({
                  section: 'default',
                  label: 'Default',
                  count: defaults.length,
                  testId: 'category-default',
                })}
                {!collapsed.default && (
                  <div className={styles.categoryBody}>
                    {defaults.map(renderRow)}
                  </div>
                )}
              </div>
            )}
            {/* CUSTOM section — user-authored; + New lives here and stays
             * visible even when CUSTOM is collapsed so the user can add
             * without expanding first. */}
            <div className={styles.category} data-category="custom">
              {renderCategoryHeader({
                section: 'custom',
                label: 'Custom',
                count: customs.length,
                testId: 'category-custom',
                trailing: (
                  <button
                    className={styles.addBtn}
                    onClick={(e) => { e.stopPropagation(); onAdd(); }}
                    title="New indicator"
                    aria-label="New indicator"
                  >
                    + New
                  </button>
                ),
              })}
              {!collapsed.custom && (
                <div className={styles.categoryBody}>
                  {customs.length === 0 ? (
                    <div className={styles.empty}>
                      No custom indicators yet — click + New to create one.
                    </div>
                  ) : (
                    customs.map(renderRow)
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default IndicatorsList;

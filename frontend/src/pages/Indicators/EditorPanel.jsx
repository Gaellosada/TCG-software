import { useRef, useCallback } from 'react';
import CodeEditor from './CodeEditor';
import DocView from './DocView';
import styles from './EditorPanel.module.css';

/**
 * Middle-column wrapper that hosts a two-tab header (Code /
 * Documentation) and renders the matching body. The caller owns the
 * view-mode state — it is page-level, not persisted.
 *
 * Props:
 *   indicatorId        {string|null}   — used as React key on DocView so
 *                                        its internal draft is dropped
 *                                        when the selected indicator
 *                                        changes (prevents cross-indicator
 *                                        draft leak on mid-edit switch)
 *   code               {string}
 *   onCodeChange       {(string) => void}
 *   doc                {string}
 *   onDocChange        {(string) => void}
 *   readOnly           {boolean}       — locks BOTH code and doc editing
 *   viewMode           {'code'|'doc'}
 *   onViewModeChange   {(mode) => void}
 *
 * A11y: role=tablist + role=tab + aria-selected + aria-controls.
 * Keyboard: left/right arrows move focus between tabs (roving tabindex
 * pattern — only the selected tab is in the tab sequence); Enter/Space
 * activate the focused tab (native button behaviour).
 */

const TABS = [
  { mode: 'code', label: 'Code', id: 'editorpanel-tab-code', panelId: 'editorpanel-panel-code' },
  { mode: 'doc', label: 'Documentation', id: 'editorpanel-tab-doc', panelId: 'editorpanel-panel-doc' },
];

function EditorPanel({
  indicatorId,
  code,
  onCodeChange,
  doc,
  onDocChange,
  readOnly,
  viewMode,
  onViewModeChange,
}) {
  const tabRefs = useRef({});
  const effectiveMode = viewMode === 'doc' ? 'doc' : 'code';

  const handleKeyDown = useCallback((event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const idx = TABS.findIndex((t) => t.mode === effectiveMode);
    if (idx < 0) return;
    const delta = event.key === 'ArrowRight' ? 1 : -1;
    const next = TABS[(idx + delta + TABS.length) % TABS.length];
    if (onViewModeChange) onViewModeChange(next.mode);
    // Move focus to the newly selected tab on the next tick — the
    // component will re-render with updated tabIndex values first.
    queueMicrotask(() => {
      const el = tabRefs.current[next.mode];
      if (el) el.focus();
    });
  }, [effectiveMode, onViewModeChange]);

  const activeTab = TABS.find((t) => t.mode === effectiveMode) || TABS[0];

  return (
    <div className={styles.panel}>
      <div
        className={styles.header}
        role="tablist"
        aria-label="Indicator editor views"
        onKeyDown={handleKeyDown}
      >
        {TABS.map((tab) => {
          const selected = tab.mode === effectiveMode;
          return (
            <button
              key={tab.mode}
              ref={(el) => { tabRefs.current[tab.mode] = el; }}
              id={tab.id}
              type="button"
              role="tab"
              aria-selected={selected ? 'true' : 'false'}
              aria-controls={tab.panelId}
              tabIndex={selected ? 0 : -1}
              className={styles.tab}
              onClick={() => { if (onViewModeChange) onViewModeChange(tab.mode); }}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      <div
        className={styles.body}
        role="tabpanel"
        id={activeTab.panelId}
        aria-labelledby={activeTab.id}
      >
        {effectiveMode === 'code' ? (
          <CodeEditor
            value={code}
            onChange={onCodeChange}
            readOnly={readOnly}
          />
        ) : (
          <DocView
            key={indicatorId ?? 'none'}
            value={doc}
            onChange={onDocChange}
            readOnly={readOnly}
          />
        )}
      </div>
    </div>
  );
}

export default EditorPanel;

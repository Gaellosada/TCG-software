import { useMemo } from 'react';
import CodeMirror from '@uiw/react-codemirror';
import { python } from '@codemirror/lang-python';
import { oneDark } from '@codemirror/theme-one-dark';
import styles from './CodeEditor.module.css';

/**
 * Center-top panel — CodeMirror 6 editor with Python syntax
 * highlighting and the canonical ``one-dark`` theme.
 *
 *   value      {string}   code content
 *   onChange   {Function} (string) => void
 *   readOnly   {boolean}  disables editing when true
 *   placeholder {string}  optional placeholder text
 *
 * Wave 1a (indicator-doc-tab): the header/title that used to live here
 * now lives in ``EditorPanel`` which wraps this component alongside the
 * new ``DocView``. This component is purely the editor body now.
 *
 * Layout: the editor fills 100% of its flex parent (which has
 * ``min-height: 0``) — ``style={{ height: '100%' }}`` on the inner
 * CodeMirror root plus ``flex: 1`` on the outer wrapper.
 */
function CodeEditor({ value, onChange, readOnly, placeholder }) {
  // The Python language extension brings syntax highlighting; the
  // one-dark theme provides colours and a dark background that is
  // clearly distinct from the surrounding panel chrome.
  const extensions = useMemo(() => [python()], []);

  // When readOnly is true AND there is no source yet (no indicator
  // selected), fall back to a helpful placeholder and an empty value.
  // When readOnly is true but a value IS provided (e.g. a default
  // indicator whose code is locked), we display the code verbatim.
  const hasValue = typeof value === 'string' && value.length > 0;
  const effectiveValue = hasValue ? value : '';
  const effectivePlaceholder = (!hasValue && readOnly)
    ? (placeholder || 'Select or create an indicator to edit its code')
    : (placeholder || '');

  const wrapperClass = readOnly
    ? `${styles.editorWrapper} ${styles.readonly}`
    : styles.editorWrapper;

  return (
    <div className={styles.panel}>
      <div className={wrapperClass} data-readonly={readOnly ? 'true' : 'false'}>
        <CodeMirror
          value={effectiveValue}
          onChange={(v) => {
            if (readOnly) return;
            onChange(v);
          }}
          readOnly={!!readOnly}
          editable={!readOnly}
          theme={oneDark}
          extensions={extensions}
          placeholder={effectivePlaceholder}
          height="100%"
          style={{ height: '100%', minHeight: 0, flex: 1 }}
          basicSetup={{
            lineNumbers: true,
            highlightActiveLine: !readOnly,
            highlightActiveLineGutter: !readOnly,
            bracketMatching: true,
            closeBrackets: true,
            autocompletion: false,
            foldGutter: false,
            indentOnInput: true,
            tabSize: 4,
          }}
          aria-label="Indicator code"
        />
        {readOnly && (
          <>
            {/* Dim/lock layer over the ENTIRE .cm-editor incl. gutters. */}
            <div className={styles.readonlyOverlay} aria-hidden="true" />
            {/* Small low-prominence badge top-right. */}
            <span className={styles.readonlyBadge}>Read-only</span>
          </>
        )}
      </div>
    </div>
  );
}

export default CodeEditor;

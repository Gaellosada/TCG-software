import { useState } from 'react';

/**
 * Shared error card for structured error envelopes:
 *   { error_type, message, traceback? }
 *
 * Extracted from the two near-identical copies previously inlined in
 * pages/Indicators/IndicatorChart.jsx and pages/Signals/ResultsView.jsx.
 *
 * The call-site passes in:
 *   - ``headings``: map from error_type → human heading.
 *   - ``fallbackHeading``: heading used when error_type is not in
 *     ``headings`` (e.g. "Error running indicator" vs
 *     "Error running signal").
 *   - ``icons`` (optional): map from error_type → SVG path ``d`` string.
 *     When provided, an icon is rendered in the header. Omit to skip
 *     the icon entirely (Signals' pre-refactor behaviour).
 *   - ``styles``: the page's CSS-module object. Kept as a prop so each
 *     caller preserves its own visual design (Indicators and Signals
 *     had visually-different cards; we don't unify them here).
 *   - ``coerceErrorType`` (optional): normalises ``error.error_type``
 *     so ``data-error-type`` reflects the resolved key. Indicators
 *     previously mapped unknown types to ``'generic'``; Signals
 *     rendered the raw value. Defaults to pass-through.
 */
function ErrorCard({
  error,
  headings,
  fallbackHeading,
  icons,
  styles,
  coerceErrorType,
}) {
  const kind = coerceErrorType
    ? coerceErrorType(error.error_type, headings)
    : error.error_type;
  const heading = headings[kind] || fallbackHeading;
  const iconPath = icons ? icons[kind] : null;

  const [copied, setCopied] = useState(false);

  function handleCopy() {
    const blob = error.traceback
      ? `${error.error_type}: ${error.message}\n\n${error.traceback}`
      : error.message;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(blob).then(
          () => { setCopied(true); setTimeout(() => setCopied(false), 1600); },
          () => { /* clipboard blocked — swallow silently */ },
        );
      }
    } catch { /* ignore */ }
  }

  return (
    <div className={styles.errorCard} data-error-type={kind} role="alert">
      <div className={styles.errorHeader}>
        {iconPath && (
          <svg
            viewBox="0 0 24 24"
            className={styles.errorIcon}
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            width="22"
            height="22"
            aria-hidden="true"
          >
            <path d={iconPath} />
          </svg>
        )}
        <h3 className={styles.errorHeading}>{heading}</h3>
        <button
          type="button"
          className={styles.copyBtn}
          onClick={handleCopy}
          aria-label="Copy error details"
        >
          {copied ? 'Copied!' : 'Copy'}
        </button>
      </div>
      <p className={styles.errorMessage}>{error.message}</p>
      {error.traceback && (
        <details className={styles.tracebackDetails}>
          <summary>Show traceback</summary>
          <pre className={styles.tracebackPre}>{error.traceback}</pre>
        </details>
      )}
    </div>
  );
}

export default ErrorCard;

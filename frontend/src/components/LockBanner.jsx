import styles from './LockBanner.module.css';

/**
 * Shared read-only banner shown above an editor when the loaded entity is
 * locked. One styled banner used identically across the Indicators, Signals
 * and Portfolio editors (UI-consistency rule) — previously each page styled
 * its own variant.
 *
 * The unlock control lives in the LIST (the shared LockToggle), so the copy
 * directs the user there.
 *
 * Props:
 *   entityLabel  {string}  human-readable noun, e.g. "indicator" / "signal" /
 *                          "portfolio". Used to build the message.
 *   className    {string}  optional extra class for page-specific spacing
 *                          (margins differ per layout); the visual style of
 *                          the banner itself is shared.
 *   testId       {string}  data-testid for the banner element. Each page
 *                          passes its existing id so tests/queries are stable.
 */
function LockBanner({ entityLabel = 'item', className = '', testId }) {
  return (
    <div
      className={`${styles.lockBanner}${className ? ` ${className}` : ''}`}
      role="status"
      data-testid={testId}
    >
      This {entityLabel} is locked — unlock it in the list to edit.
    </div>
  );
}

export default LockBanner;

// Centralised localStorage key constants for the Indicators page.
//
// All three keys share the ``tcg.indicators`` namespace so collisions are
// impossible and a future key-version migration can be done in one place.

/** Versioned schema key for the main indicators state (indicators[], defaultState). */
export const INDICATORS_STORAGE_KEY = 'tcg.indicators.v1';

/** Autosave-enabled toggle persisted per browser session. */
export const AUTOSAVE_KEY = 'tcg.indicators.autosave';

/** Per-section collapse state for the DEFAULT / CUSTOM list sections. */
export const LIST_COLLAPSED_KEY = 'tcg.indicators.listCollapsed';

const STORAGE_KEY = 'tcg-default-providers';
const EVENT_NAME = 'tcg-provider-change';

function readDefaults() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

/**
 * Hook for reading/writing default provider preferences per collection type.
 *
 * Stored in localStorage as JSON: { "INDEX": "YAHOO", "FUT_": "IVOLATILITY", ... }
 *
 * getDefault(collection):
 *   1. Exact match on collection name
 *   2. Prefix match (keys ending with "_" like "FUT_")
 *   3. null if no match
 *
 * setDefault(collectionKey, provider):
 *   Updates localStorage and dispatches a custom event for cross-component sync.
 */
export default function useProviderPreference() {
  function getDefault(collection) {
    const defaults = readDefaults();

    // Exact match
    if (defaults[collection] !== undefined) {
      return defaults[collection];
    }

    // Prefix match: keys ending with "_" act as prefixes
    for (const key of Object.keys(defaults)) {
      if (key.endsWith('_') && collection.startsWith(key)) {
        return defaults[key];
      }
    }

    return null;
  }

  function setDefault(collectionKey, provider) {
    const defaults = readDefaults();
    defaults[collectionKey] = provider;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(defaults));
    } catch {
      // localStorage unavailable — ignore
    }
    window.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: { key: collectionKey, provider } }));
  }

  return { getDefault, setDefault };
}

export { STORAGE_KEY, EVENT_NAME };

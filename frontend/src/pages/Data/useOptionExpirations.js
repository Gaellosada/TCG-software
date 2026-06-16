import { useOptionExpirationsQuery } from '../../hooks/marketQueries';

/**
 * Distinct expirations for an option root.
 *
 * Backs the chain / smile date pickers so the user can only choose
 * dates that actually have contracts. Backed by TanStack Query
 * (stale-while-revalidate): the expirations for a root are cached, so the
 * pickers populate instantly on re-selection and revalidate in the
 * background. Resolves to ``[]`` when ``root`` is null (query disabled).
 *
 * @param {string|null} root - OPT_* collection name (e.g. 'OPT_SP_500').
 * @returns {{ expirations: string[], loading: boolean, error: Error|null }}
 *   ``expirations`` is the array of ISO YYYY-MM-DD strings, sorted ascending.
 */
export function useOptionExpirations(root) {
  const { data, loading, error } = useOptionExpirationsQuery(root);
  const expirations =
    data && Array.isArray(data.expirations) ? data.expirations : [];
  return { expirations, loading, error };
}

import useAsync from '../../hooks/useAsync';
import { getOptionExpirations } from '../../api/options';

/**
 * Distinct expirations for an option root.
 *
 * Backs the chain / smile date pickers so the user can only choose
 * dates that actually have contracts. Re-fetches whenever ``root``
 * changes; resolves to ``[]`` when ``root`` is null.
 *
 * @param {string|null} root - OPT_* collection name (e.g. 'OPT_SP_500').
 * @returns {{ expirations: string[], loading: boolean, error: Error|null }}
 *   ``expirations`` is the array of ISO YYYY-MM-DD strings, sorted ascending.
 */
export function useOptionExpirations(root) {
  const { data, loading, error } = useAsync(
    () => (root ? getOptionExpirations(root) : Promise.resolve(null)),
    [root],
  );
  const expirations =
    data && Array.isArray(data.expirations) ? data.expirations : [];
  return { expirations, loading, error };
}

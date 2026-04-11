import { useState, useEffect } from 'react';

/**
 * Generic async data fetching hook.
 *
 * @param {() => Promise<T>} asyncFn - Function returning a promise
 * @param {Array} deps - Dependency array for re-fetching
 * @returns {{ data: T | null, loading: boolean, error: Error | null }}
 */
function useAsync(asyncFn, deps = []) {
  const [state, setState] = useState({ data: null, loading: true, error: null });

  useEffect(() => {
    let cancelled = false;
    setState({ data: null, loading: true, error: null });

    asyncFn()
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, error: null });
      })
      .catch((err) => {
        const error = err instanceof Error ? err : new Error(String(err || 'Unknown error'));
        if (!cancelled) setState({ data: null, loading: false, error });
      });

    return () => {
      cancelled = true;
    };
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps

  return state;
}

export default useAsync;

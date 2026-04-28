import { useEffect, useMemo } from 'react';
import { useOptionsChain } from './useOptionsChain';
import styles from './OptionChainTable.module.css';

// Roots whose strike-factor convention is still pending verification
// per Phase 1A investigation. The chain may be displayed at the wrong scale
// until a sample-verified factor is committed in `_strike_factor.py`.
const VERIFICATION_PENDING_ROOTS = new Set([
  'OPT_T_NOTE_10_Y',
  'OPT_T_BOND',
  'OPT_EURUSD',
  'OPT_JPYUSD',
]);

// ---------------------------------------------------------------------------
// Number formatters
// ---------------------------------------------------------------------------

function fmt(value, decimals) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toFixed(decimals);
}

function fmtInt(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Math.round(Number(value)).toLocaleString();
}

// ---------------------------------------------------------------------------
// ComputeResultCell — visual rules from spec §6 + §8
//
//   stored   → normal weight, formatted value
//   computed → italic + ⓒ badge + tooltip from inputs_used
//   missing  → em-dash + tooltip "{error_code}: {error_detail}"
//
// Decimal convention:
//   - IV / Δ / Γ / Θ / ν: 4 decimal places
//   - bid / ask / mid / strike: 2 decimal places
//   - open_interest: integer (handled separately)
// ---------------------------------------------------------------------------

export function ComputeResultCell({ result, decimals = 4 }) {
  if (!result) {
    // Defensive — shouldn't happen given the contract, but render an em-dash
    // rather than a crash-stack.
    return <span className={styles.missing}>—</span>;
  }

  const { value, source } = result;

  if (source === 'missing') {
    const tip = result.error_code
      ? `${result.error_code}: ${result.error_detail || ''}`
      : 'Missing';
    return (
      <span className={styles.missing} title={tip}>
        —
      </span>
    );
  }

  if (source === 'computed') {
    const inputs = result.inputs_used || {};
    const parts = [];
    if (result.model) parts.push(`Computed via ${result.model}.`);
    const inputBits = [];
    if (inputs.underlying_price !== undefined && inputs.underlying_price !== null) {
      inputBits.push(`F = ${inputs.underlying_price}`);
    }
    if (inputs.iv !== undefined && inputs.iv !== null) {
      inputBits.push(`IV = ${inputs.iv}`);
    }
    if (inputs.ttm !== undefined && inputs.ttm !== null) {
      inputBits.push(`T = ${inputs.ttm} yr`);
    }
    if (inputs.r !== undefined && inputs.r !== null) {
      inputBits.push(`r = ${inputs.r}`);
    }
    if (inputBits.length > 0) {
      parts.push(`Inputs: ${inputBits.join(', ')}.`);
    }
    const tip = parts.join(' ');
    return (
      <span className={styles.computed} title={tip}>
        {fmt(value, decimals)}
        <span className={styles.computedBadge} aria-label="computed">ⓒ</span>
      </span>
    );
  }

  // source === 'stored' (or any other value — render defensively)
  if (value === null || value === undefined) {
    // Shouldn't happen for source="stored" per the contract.
    return <span className={styles.missing}>—</span>;
  }
  return <span>{fmt(value, decimals)}</span>;
}

// ---------------------------------------------------------------------------
// Main table component
// ---------------------------------------------------------------------------

export default function OptionChainTable({ root, onRowClick, initialFilters }) {
  const { filters, chainData, loading, fetchChain, updateFilters } = useOptionsChain(
    root,
    initialFilters,
  );

  // Trigger initial fetch when root changes (mount or root prop update).
  useEffect(() => {
    if (root && root !== filters.root) {
      updateFilters({ root });
    }
  }, [root, filters.root, updateFilters]);

  // Auto-fetch on mount or filter changes (debounced 200ms).
  useEffect(() => {
    if (!filters.root || !filters.date) return undefined;
    const handle = setTimeout(() => {
      fetchChain();
    }, 200);
    return () => clearTimeout(handle);
  }, [filters, fetchChain]);

  const error = chainData && chainData.error ? chainData.error : null;
  const rows = chainData && !chainData.error && chainData.rows ? chainData.rows : null;

  const showVerificationBanner = useMemo(() => {
    if (filters.root && VERIFICATION_PENDING_ROOTS.has(filters.root)) return true;
    if (rows && rows.some((r) => r && r.strike_factor_verified === false)) return true;
    return false;
  }, [filters.root, rows]);

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h2 className={styles.title}>{filters.root || 'Option Chain'}</h2>
        {chainData && !chainData.error && (
          <span className={styles.meta}>
            {(rows || []).length.toLocaleString()} contracts
            {chainData.date ? ` · as of ${chainData.date}` : ''}
          </span>
        )}
      </div>

      {showVerificationBanner && (
        <div className={styles.banner} role="alert">
          Strike factor verification pending for {filters.root}. Contract data may be
          displayed at the wrong scale until verified.
        </div>
      )}

      <div className={styles.filters}>
        <label className={styles.filterLabel}>
          Date
          <input
            type="date"
            className={styles.filterInput}
            value={filters.date || ''}
            onChange={(e) => updateFilters({ date: e.target.value || null })}
          />
        </label>
        <label className={styles.filterLabel}>
          Type
          <select
            className={styles.filterSelect}
            value={filters.type}
            onChange={(e) => updateFilters({ type: e.target.value })}
          >
            <option value="both">Both</option>
            <option value="C">Calls</option>
            <option value="P">Puts</option>
          </select>
        </label>
        <label className={styles.filterLabel}>
          Expiration min
          <input
            type="date"
            className={styles.filterInput}
            value={filters.expirationMin || ''}
            onChange={(e) => updateFilters({ expirationMin: e.target.value || null })}
          />
        </label>
        <label className={styles.filterLabel}>
          Expiration max
          <input
            type="date"
            className={styles.filterInput}
            value={filters.expirationMax || ''}
            onChange={(e) => updateFilters({ expirationMax: e.target.value || null })}
          />
        </label>
        <label className={styles.filterLabel}>
          Strike min
          <input
            type="number"
            className={styles.filterInput}
            value={filters.strikeMin == null ? '' : filters.strikeMin}
            onChange={(e) =>
              updateFilters({
                strikeMin: e.target.value === '' ? null : Number(e.target.value),
              })
            }
          />
        </label>
        <label className={styles.filterLabel}>
          Strike max
          <input
            type="number"
            className={styles.filterInput}
            value={filters.strikeMax == null ? '' : filters.strikeMax}
            onChange={(e) =>
              updateFilters({
                strikeMax: e.target.value === '' ? null : Number(e.target.value),
              })
            }
          />
        </label>
        <label
          className={styles.toggle}
          title="Compute missing Greeks via Black-76 (Decision C: transient)"
        >
          <input
            type="checkbox"
            checked={!!filters.computeMissing}
            onChange={(e) => updateFilters({ computeMissing: e.target.checked })}
          />
          Compute missing Greeks
        </label>
        <button
          type="button"
          className={styles.fetchButton}
          onClick={() => fetchChain()}
          disabled={loading || !filters.root || !filters.date}
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {loading && !rows && <div className={styles.loading}>Loading chain…</div>}
      {error && (
        <div className={styles.error}>
          Failed to load chain: {error.message || String(error)}
        </div>
      )}

      {rows && rows.length === 0 && !loading && (
        <div className={styles.empty}>No contracts match current filters.</div>
      )}

      {rows && rows.length > 0 && (
        <div className={styles.tableWrapper}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Expiration</th>
                <th>Type</th>
                <th>Strike</th>
                <th>Bid</th>
                <th>Ask</th>
                <th>Mid</th>
                <th>IV</th>
                <th>Δ</th>
                <th>Γ</th>
                <th>Θ</th>
                <th>ν</th>
                <th>OI</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.contract_id}
                  className={styles.row}
                  onClick={() =>
                    onRowClick &&
                    onRowClick({
                      collection: filters.root,
                      instrument_id: row.contract_id,
                      expiry: row.expiration,
                      strike: row.strike,
                      optionType: row.type,
                    })
                  }
                >
                  <td>{row.expiration}</td>
                  <td className={row.type === 'C' ? styles.typeCall : styles.typePut}>
                    {row.type}
                  </td>
                  <td>{fmt(row.strike, 2)}</td>
                  <td>{fmt(row.bid, 2)}</td>
                  <td>{fmt(row.ask, 2)}</td>
                  <td>{fmt(row.mid, 2)}</td>
                  <td><ComputeResultCell result={row.iv} decimals={4} /></td>
                  <td><ComputeResultCell result={row.delta} decimals={4} /></td>
                  <td><ComputeResultCell result={row.gamma} decimals={4} /></td>
                  <td><ComputeResultCell result={row.theta} decimals={4} /></td>
                  <td><ComputeResultCell result={row.vega} decimals={4} /></td>
                  <td>{fmtInt(row.open_interest)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

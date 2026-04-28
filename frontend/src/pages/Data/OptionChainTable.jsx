import { useEffect, useMemo } from 'react';
import { useOptionsChain } from './useOptionsChain';
import { useOptionExpirations } from './useOptionExpirations';
import styles from './OptionChainTable.module.css';

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
// MergedChainTable — canonical chain layout. Rows are unique (expiration,
// strike) pairs; each row carries the call on the left, the strike in the
// middle, the put on the right. Click on a call cell opens the call's
// detail; click on a put cell opens the put's. Click on a shared cell (Exp /
// Strike) defaults to whichever side exists, preferring the call.
// ---------------------------------------------------------------------------

function mergeRows(rows) {
  const map = new Map();
  for (const r of rows) {
    const key = `${r.expiration}|${r.strike}`;
    if (!map.has(key)) {
      map.set(key, {
        expiration: r.expiration,
        expiration_cycle: r.expiration_cycle ?? '',
        strike: r.strike,
        call: null,
        put: null,
      });
    }
    const entry = map.get(key);
    if (r.type === 'C') entry.call = r;
    else if (r.type === 'P') entry.put = r;
  }
  // Sort by expiration descending, then strike descending — most distant
  // expiration on top, highest strike first within an expiration.
  return [...map.values()].sort((a, b) => {
    if (a.expiration !== b.expiration) return a.expiration < b.expiration ? 1 : -1;
    return b.strike - a.strike;
  });
}

function MergedChainTable({ rows, collection, onRowClick }) {
  const merged = useMemo(() => mergeRows(rows), [rows]);

  const handleRowClick = (e, entry) => {
    if (!onRowClick) return;
    const td = e.target.closest('td');
    const side = td?.dataset?.side;
    const target =
      side === 'call'
        ? entry.call
        : side === 'put'
          ? entry.put
          : entry.call || entry.put;
    if (!target) return;
    onRowClick({
      collection,
      instrument_id: target.contract_id,
      expiry: target.expiration,
      strike: target.strike,
      optionType: target.type,
    });
  };

  return (
    <div className={styles.tableWrapper}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th rowSpan={2} className={styles.colExp}>Expiration</th>
            <th colSpan={10} className={styles.groupCalls}>Calls</th>
            <th rowSpan={2} className={styles.colStrike}>Strike</th>
            <th colSpan={10} className={styles.groupPuts}>Puts</th>
          </tr>
          <tr>
            <th className={styles.bidQuote}>Bid</th>
            <th>Mid</th>
            <th className={styles.askQuote}>Ask</th>
            <th>IV</th>
            <th>Δ</th><th>Γ</th><th>Θ</th><th>ν</th><th>OI</th>
            <th className={styles.typeCol} aria-label="Call marker"></th>
            <th className={styles.typeCol} aria-label="Put marker"></th>
            <th className={styles.bidQuote}>Bid</th>
            <th>Mid</th>
            <th className={styles.askQuote}>Ask</th>
            <th>IV</th>
            <th>Δ</th><th>Γ</th><th>Θ</th><th>ν</th><th>OI</th>
          </tr>
        </thead>
        <tbody>
          {merged.map((entry, idx) => {
            const c = entry.call;
            const p = entry.put;
            const expChanged =
              idx > 0 && merged[idx - 1].expiration !== entry.expiration;
            return (
              <tr
                key={`${entry.expiration}|${entry.strike}`}
                className={`${styles.row} ${expChanged ? styles.expChange : ''}`}
                onClick={(e) => handleRowClick(e, entry)}
              >
                <td className={styles.colExp}>
                  {entry.expiration}
                  {entry.expiration_cycle && (
                    <span
                      className={styles.cycleChip}
                      title={entry.expiration_cycle}
                      data-testid="cycle-chip"
                    >
                      {entry.expiration_cycle.trim()[0]}
                    </span>
                  )}
                </td>
                <td data-side="call" className={styles.bidQuote}>{c ? fmt(c.bid, 2) : ''}</td>
                <td data-side="call">{c ? fmt(c.mid, 2) : ''}</td>
                <td data-side="call" className={`${styles.askQuote} ${styles.thinSepRight}`}>{c ? fmt(c.ask, 2) : ''}</td>
                <td data-side="call">{c ? <ComputeResultCell result={c.iv} decimals={4} /> : ''}</td>
                <td data-side="call">{c ? <ComputeResultCell result={c.delta} decimals={4} /> : ''}</td>
                <td data-side="call">{c ? <ComputeResultCell result={c.gamma} decimals={4} /> : ''}</td>
                <td data-side="call">{c ? <ComputeResultCell result={c.theta} decimals={4} /> : ''}</td>
                <td data-side="call">{c ? <ComputeResultCell result={c.vega} decimals={4} /> : ''}</td>
                <td data-side="call">{c ? fmtInt(c.open_interest) : ''}</td>
                <td
                  data-side="call"
                  className={`${styles.typeCol} ${styles.thinSepLeft} ${c ? styles.typeCall : ''}`}
                >
                  {c ? 'C' : ''}
                </td>
                <td className={styles.colStrike}>{fmt(entry.strike, 2)}</td>
                <td
                  data-side="put"
                  className={`${styles.typeCol} ${styles.thinSepRight} ${p ? styles.typePut : ''}`}
                >
                  {p ? 'P' : ''}
                </td>
                <td data-side="put" className={styles.bidQuote}>{p ? fmt(p.bid, 2) : ''}</td>
                <td data-side="put">{p ? fmt(p.mid, 2) : ''}</td>
                <td data-side="put" className={`${styles.askQuote} ${styles.thinSepRight}`}>{p ? fmt(p.ask, 2) : ''}</td>
                <td data-side="put">{p ? <ComputeResultCell result={p.iv} decimals={4} /> : ''}</td>
                <td data-side="put">{p ? <ComputeResultCell result={p.delta} decimals={4} /> : ''}</td>
                <td data-side="put">{p ? <ComputeResultCell result={p.gamma} decimals={4} /> : ''}</td>
                <td data-side="put">{p ? <ComputeResultCell result={p.theta} decimals={4} /> : ''}</td>
                <td data-side="put">{p ? <ComputeResultCell result={p.vega} decimals={4} /> : ''}</td>
                <td data-side="put">{p ? fmtInt(p.open_interest) : ''}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main table component
// ---------------------------------------------------------------------------

export default function OptionChainTable({ root, onRowClick, initialFilters }) {
  const { filters, chainData, loading, fetchChain, updateFilters } = useOptionsChain(
    root,
    initialFilters,
  );
  const { expirations, loading: expirationsLoading } = useOptionExpirations(root);

  // Trigger initial fetch when root changes (mount or root prop update).
  useEffect(() => {
    if (root && root !== filters.root) {
      updateFilters({ root });
    }
  }, [root, filters.root, updateFilters]);

  // Snap min/max to valid contract expirations once the list loads. Without
  // this, the seeded defaults (last_trade_date for min, last_trade_date+90d
  // for max) almost never coincide with actual expiration days. Both
  // default to the LATEST available so the chain opens on the most recent
  // expiration; the user can broaden the window from there.
  useEffect(() => {
    if (!expirations || expirations.length === 0) return;
    const latest = expirations[expirations.length - 1];
    const updates = {};
    if (!filters.expirationMin || !expirations.includes(filters.expirationMin)) {
      updates.expirationMin = latest;
    }
    if (!filters.expirationMax || !expirations.includes(filters.expirationMax)) {
      updates.expirationMax = latest;
    }
    if (Object.keys(updates).length > 0) {
      updateFilters(updates);
    }
  }, [expirations, filters.expirationMin, filters.expirationMax, updateFilters]);

  // Display order: latest expiration on top of the dropdown.
  const expirationOptions = useMemo(
    () => [...expirations].reverse(),
    [expirations],
  );

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

  // Distinct, non-empty, sorted cycles observed in the current chain.
  // Drives the cycle dropdown options. Empty chain → empty list (the
  // dropdown still renders the "All cycles" sentinel option for the
  // user's current selection but no cycle options yet).
  const cycleOptions = useMemo(() => {
    if (!rows) return [];
    return [...new Set(rows.map((r) => r.expiration_cycle).filter(Boolean))].sort();
  }, [rows]);

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h2 className={styles.title}>{filters.root || 'Option Chain'}</h2>
        {chainData && !chainData.error && (
          <span className={styles.meta}>
            {(rows || []).length.toLocaleString()} contracts
            {chainData.date ? ` · as of ${chainData.date}` : ''}
            {chainData.underlying_price &&
              chainData.underlying_price.value != null &&
              ` · Underlying price (S) on ${chainData.date}: ${Number(chainData.underlying_price.value).toFixed(2)}`}
          </span>
        )}
      </div>

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
          Expiration min
          <select
            className={styles.filterSelect}
            value={
              filters.expirationMin && expirations.includes(filters.expirationMin)
                ? filters.expirationMin
                : ''
            }
            onChange={(e) => updateFilters({ expirationMin: e.target.value || null })}
            disabled={expirationsLoading || expirations.length === 0}
          >
            {expirationsLoading && <option value="">Loading…</option>}
            {!expirationsLoading && expirations.length === 0 && (
              <option value="">No expirations</option>
            )}
            {expirationOptions.map((exp) => (
              <option key={exp} value={exp}>{exp}</option>
            ))}
          </select>
        </label>
        <label className={styles.filterLabel}>
          Expiration max
          <select
            className={styles.filterSelect}
            value={
              filters.expirationMax && expirations.includes(filters.expirationMax)
                ? filters.expirationMax
                : ''
            }
            onChange={(e) => updateFilters({ expirationMax: e.target.value || null })}
            disabled={expirationsLoading || expirations.length === 0}
          >
            {expirationsLoading && <option value="">Loading…</option>}
            {!expirationsLoading && expirations.length === 0 && (
              <option value="">No expirations</option>
            )}
            {expirationOptions.map((exp) => (
              <option key={exp} value={exp}>{exp}</option>
            ))}
          </select>
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
        <label className={styles.filterLabel}>
          Cycle
          <select
            className={styles.filterSelect}
            value={filters.expirationCycle ?? ''}
            onChange={(e) =>
              updateFilters({
                expirationCycle: e.target.value === '' ? null : e.target.value,
              })
            }
          >
            <option value="">All cycles</option>
            {cycleOptions.map((cyc) => (
              <option key={cyc} value={cyc}>{cyc}</option>
            ))}
          </select>
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
        <MergedChainTable
          rows={rows}
          collection={filters.root}
          onRowClick={onRowClick}
        />
      )}
    </div>
  );
}

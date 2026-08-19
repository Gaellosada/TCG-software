// Per-rung readout for laddered multi-entry runs (F4.1).
//
// PURE presentational component over the run response `days[]`. It renders ONLY
// the laddered days (those carrying a non-empty `entries[]`); for a non-laddered
// run every day lacks `entries`, so the component renders nothing (returns null)
// — it never disturbs the day-level calendar or the weekday/regime/event views,
// which continue to key on the retained one-row-per-day aggregate.
//
// Each rung row shows its entry time, strike, status, sizing `contracts` weight,
// the per-contract P&L, and the rung's weighted dollar CONTRIBUTION to the day.
// By construction the day aggregate equals the sum of the rungs' contributions,
// which the header restates so the aggregation is legible.

function fmtUsd(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  const sign = v < 0 ? '-' : '';
  return `${sign}$${Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

function fmtNum(v, digits = 2) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  return v.toLocaleString('en-US', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

// "2025-02-03T15:00:00Z" -> "15:00Z". Defensive: returns the raw string on a
// shape it can't parse.
function fmtEntryTime(iso) {
  if (typeof iso !== 'string') return '';
  const m = /T(\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]}Z` : iso;
}

/**
 * @param {{ days?: Array<object> }} props - the run response `days[]`.
 */
export default function LadderEntriesView({ days }) {
  const laddered = (days || []).filter(
    (d) => d && Array.isArray(d.entries) && d.entries.length > 0,
  );
  if (laddered.length === 0) return null;

  return (
    <section data-testid="ladder-entries-view">
      <h3>Laddered entries (per rung)</h3>
      {laddered.map((day) => {
        const dayUsd = day.pnl ? day.pnl.total_pnl_usd : null;
        return (
          <div key={day.date} data-testid={`ladder-day-${day.date}`} data-date={day.date}>
            <h4>
              {day.date}
              {' — day total '}
              <span data-testid={`ladder-day-total-${day.date}`}>{fmtUsd(dayUsd)}</span>
              {` (${day.entries.length} rungs)`}
            </h4>
            <table>
              <thead>
                <tr>
                  <th>Entry</th>
                  <th>Strike</th>
                  <th>Status</th>
                  <th>Contracts</th>
                  <th>P&L / contract</th>
                  <th>Contribution</th>
                </tr>
              </thead>
              <tbody>
                {day.entries.map((e, i) => {
                  const perContract = e.pnl ? e.pnl.total_pnl_usd : null;
                  return (
                    <tr
                      key={`${day.date}-${e.entry_ts || i}`}
                      data-testid="ladder-entry-row"
                      data-status={e.status}
                    >
                      <td>{fmtEntryTime(e.entry_ts)}</td>
                      <td>{e.strike != null ? fmtNum(e.strike, 2) : '—'}</td>
                      <td>{e.status === 'ok' ? 'traded' : (e.skip_reason || e.status)}</td>
                      <td>{fmtNum(e.contracts, 3)}</td>
                      <td>{fmtUsd(perContract)}</td>
                      <td data-testid="ladder-entry-contribution">
                        {fmtUsd(e.weighted_pnl_usd)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      })}
    </section>
  );
}

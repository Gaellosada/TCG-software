import { describe, it, expect } from 'vitest';
import { buildCsv } from './chartCsv';

describe('buildCsv', () => {
  it('returns empty string for empty input', () => {
    expect(buildCsv([])).toBe('');
    expect(buildCsv(null)).toBe('');
    expect(buildCsv(undefined)).toBe('');
  });

  it('exports a single line trace', () => {
    const csv = buildCsv([
      { type: 'scatter', name: 'Close', x: ['2024-01-01', '2024-01-02'], y: [100, 101] },
    ]);
    expect(csv).toBe('date,Close\n2024-01-01,100\n2024-01-02,101\n');
  });

  it('excludes traces hidden via visible=false or legendonly', () => {
    const csv = buildCsv([
      { type: 'scatter', name: 'A', x: ['d1'], y: [1] },
      { type: 'scatter', name: 'B', x: ['d1'], y: [2], visible: false },
      { type: 'scatter', name: 'C', x: ['d1'], y: [3], visible: 'legendonly' },
    ]);
    expect(csv).toBe('date,A\nd1,1\n');
  });

  it('excludes decorative traces (hoverinfo=skip)', () => {
    const csv = buildCsv([
      { type: 'scatter', name: 'Price', x: ['d1'], y: [1] },
      { type: 'scatter', name: 'Rebalance', x: ['d1'], y: [0], hoverinfo: 'skip' },
    ]);
    expect(csv).toBe('date,Price\nd1,1\n');
  });

  it('unions x values across traces with different dates', () => {
    const csv = buildCsv([
      { type: 'scatter', name: 'A', x: ['d1', 'd3'], y: [10, 30] },
      { type: 'scatter', name: 'B', x: ['d2', 'd3'], y: [200, 300] },
    ]);
    expect(csv).toBe('date,A,B\nd1,10,\nd2,,200\nd3,30,300\n');
  });

  it('expands candlestick traces into OHLC columns', () => {
    const csv = buildCsv([
      {
        type: 'candlestick',
        name: 'AAPL',
        x: ['d1'],
        open: [10], high: [12], low: [9], close: [11],
      },
    ]);
    expect(csv).toBe('date,AAPL_open,AAPL_high,AAPL_low,AAPL_close\nd1,10,12,9,11\n');
  });

  it('deduplicates colliding trace names', () => {
    const csv = buildCsv([
      { type: 'scatter', name: 'Series', x: ['d1'], y: [1] },
      { type: 'scatter', name: 'Series', x: ['d1'], y: [2] },
    ]);
    expect(csv).toBe('date,Series,Series_2\nd1,1,2\n');
  });

  it('falls back to series_N for anonymous traces', () => {
    const csv = buildCsv([
      { type: 'scatter', x: ['d1'], y: [5] },
    ]);
    expect(csv).toBe('date,series_1\nd1,5\n');
  });

  it('escapes commas, quotes, and newlines in names and values', () => {
    const csv = buildCsv([
      { type: 'scatter', name: 'A,B', x: ['d"1'], y: ['line\nbreak'] },
    ]);
    expect(csv).toBe('date,"A,B"\n"d""1","line\nbreak"\n');
  });

  it('guards CSV formula injection for =, +, @ (but keeps negative numbers intact)', () => {
    const csv = buildCsv([
      { type: 'scatter', name: '=HYPERLINK("evil")', x: ['d1', 'd2'], y: ['+1+1', -100] },
      { type: 'scatter', name: '@cmd', x: ['d1'], y: [1] },
    ]);
    // Headers: `=...` gets prefixed with `'` then CSV-wrapped due to embedded `"`;
    // `@cmd` gets prefixed with `'` but needs no wrapping.
    expect(csv).toContain('"\'=HYPERLINK(""evil"")"');
    expect(csv).toContain("'@cmd");
    // Values: `+1+1` prefixed with `'`; `-100` preserved as-is.
    expect(csv).toContain("d1,'+1+1,1\n");
    expect(csv).toContain('d2,-100,\n');
  });

  it('skips traces missing required data arrays', () => {
    const csv = buildCsv([
      { type: 'scatter', name: 'NoY', x: ['d1'] },
      { type: 'scatter', name: 'OK', x: ['d1'], y: [1] },
    ]);
    expect(csv).toBe('date,OK\nd1,1\n');
  });
});

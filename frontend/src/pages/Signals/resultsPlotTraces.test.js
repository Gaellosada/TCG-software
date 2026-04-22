// Pure-JS tests for the Results trace helpers.
//
// Covers trace construction without rendering React / Plotly.

import { describe, it, expect } from 'vitest';
import {
  buildInputTraces,
  aggregateRealizedPnl,
  buildResultsPlot,
  computeSubplotDomains,
  countOwnPanelIndicators,
  partitionIndicators,
  buildEventMarkerTraces,
  buildIndicatorTraces,
  buildBlockWeightSignMap,
  EVENT_MARKER,
} from './resultsPlotTraces';

const ts = [1577923200000, 1578009600000, 1578268800000];
const dates = ts.map((m) => new Date(m));

function positionWithPrice(id, prices) {
  return {
    input_id: id,
    instrument: { type: 'spot', collection: 'INDEX', instrument_id: id },
    values: [0, 0, 0],
    clipped_mask: [false, false, false],
    price: { label: `${id}.close`, values: prices },
  };
}

describe('buildInputTraces', () => {
  it('emits one line trace per input that has a price', () => {
    const positions = [
      positionWithPrice('X', [100, 101, 102]),
      { input_id: 'Y', instrument: {}, values: [], clipped_mask: [], price: null },
      positionWithPrice('Z', [50, 51, 52]),
    ];
    const traces = buildInputTraces(positions, dates);
    expect(traces).toHaveLength(2);
    expect(traces[0].name).toMatch(/X/);
    expect(traces[0].mode).toBe('lines');
    expect(traces[1].name).toMatch(/Z/);
  });

  it('skips inputs without a price (does not synthesize)', () => {
    const positions = [{ input_id: 'X', price: null }];
    expect(buildInputTraces(positions, dates)).toHaveLength(0);
  });

  it('sets yaxis when opts.yaxis is provided', () => {
    const positions = [positionWithPrice('X', [1, 2, 3])];
    const traces = buildInputTraces(positions, dates, { yaxis: 'y3' });
    expect(traces[0].yaxis).toBe('y3');
  });

  it('does not set yaxis when opts.yaxis is omitted', () => {
    const positions = [positionWithPrice('X', [1, 2, 3])];
    const traces = buildInputTraces(positions, dates);
    expect(traces[0].yaxis).toBeUndefined();
  });
});

describe('aggregateRealizedPnl', () => {
  it('sums element-wise across inputs', () => {
    const out = aggregateRealizedPnl([[1, 2, 3], [10, 20, 30]], 3);
    expect(out).toEqual([11, 22, 33]);
  });
  it('treats non-finite entries as 0', () => {
    const out = aggregateRealizedPnl([[NaN, 1, null], [2, Infinity, 3]], 3);
    expect(out).toEqual([2, 1, 3]);
  });
  it('returns null when the payload is absent or empty', () => {
    expect(aggregateRealizedPnl(undefined, 3)).toBeNull();
    expect(aggregateRealizedPnl([], 3)).toBeNull();
    expect(aggregateRealizedPnl([[NaN, NaN]], 2)).toBeNull();
  });
});

describe('buildBlockWeightSignMap', () => {
  it('maps entry block ids to sign(weight)', () => {
    const rules = {
      entries: [
        { id: 'e1', input_id: 'X', weight: 50, conditions: [] },
        { id: 'e2', input_id: 'Y', weight: -30, conditions: [] },
        { id: 'e3', input_id: 'Z', weight: 0, conditions: [] },
      ],
      exits: [],
    };
    const map = buildBlockWeightSignMap(rules);
    expect(map.e1).toBe(1);
    expect(map.e2).toBe(-1);
    expect(map.e3).toBe(0);
  });

  it('maps exit block ids to sign(weight) of the target entry (by name)', () => {
    const rules = {
      entries: [
        { id: 'e1', name: 'Alpha', input_id: 'X', weight: 75, conditions: [] },
        { id: 'e2', name: 'Beta', input_id: 'X', weight: -25, conditions: [] },
      ],
      exits: [
        { id: 'x1', input_id: 'X', weight: 0, target_entry_block_name: 'Alpha', conditions: [] },
        { id: 'x2', input_id: 'X', weight: 0, target_entry_block_name: 'Beta', conditions: [] },
        { id: 'x3', input_id: 'X', weight: 0, target_entry_block_name: 'unknown', conditions: [] },
      ],
    };
    const map = buildBlockWeightSignMap(rules);
    expect(map.x1).toBe(1);
    expect(map.x2).toBe(-1);
    expect(map.x3).toBe(0); // dangling target → neutral fallback
  });

  it('returns {} for null / malformed rules', () => {
    expect(buildBlockWeightSignMap(null)).toEqual({});
    expect(buildBlockWeightSignMap(undefined)).toEqual({});
    expect(buildBlockWeightSignMap({})).toEqual({});
  });
});

describe('buildEventMarkerTraces', () => {
  const positions = [positionWithPrice('X', [100, 101, 102])];

  it('emits markers at fired_indices by default (long entry w/ positive weight)', () => {
    const events = [
      {
        input_id: 'X',
        block_id: 'b1',
        kind: 'entry',
        fired_indices: [0, 2],
        latched_indices: [0],
        active_indices: [0, 1, 2],
      },
    ];
    const blockWeightSigns = { b1: 1 };
    const traces = buildEventMarkerTraces(events, positions, dates, undefined, { blockWeightSigns });
    expect(traces).toHaveLength(1);
    expect(traces[0].mode).toBe('markers');
    expect(traces[0].x).toHaveLength(2);
    expect(traces[0].y).toEqual([100, 102]);
    // Long entry → green filled triangle-up
    expect(traces[0].marker.symbol).toBe(EVENT_MARKER.entry[1].symbol);
    expect(traces[0].marker.color).toBe(EVENT_MARKER.entry[1].color);
  });

  it('uses open variants for exit kinds', () => {
    const events = [
      {
        input_id: 'X', block_id: 'x1', kind: 'exit',
        fired_indices: [1], latched_indices: [1], target_entry_block_name: 'e1',
      },
    ];
    // Exit targets a LONG entry (positive weight) → green open triangle-down
    const [trace] = buildEventMarkerTraces(
      events, positions, dates, undefined, { blockWeightSigns: { x1: 1 } },
    );
    expect(trace.marker.symbol).toBe('triangle-down-open');
    expect(trace.marker.color).toBe(EVENT_MARKER.exit[1].color);
  });

  it('applies the short entry / short exit colour convention via weight sign', () => {
    // Short entry: kind=entry + negative-weight block → red filled triangle-down
    const entry = buildEventMarkerTraces(
      [{ input_id: 'X', block_id: 'b2', kind: 'entry', fired_indices: [1], latched_indices: [1] }],
      positions, dates, undefined, { blockWeightSigns: { b2: -1 } },
    );
    // Short exit: kind=exit + targets a negative-weight entry → red OPEN triangle-up
    const exit = buildEventMarkerTraces(
      [{ input_id: 'X', block_id: 'b3', kind: 'exit', fired_indices: [2], latched_indices: [2], target_entry_block_name: 'b2' }],
      positions, dates, undefined, { blockWeightSigns: { b3: -1 } },
    );
    expect(entry[0].marker.color).toBe('#ef4444');
    expect(entry[0].marker.symbol).toBe('triangle-down');
    expect(exit[0].marker.color).toBe('#ef4444');
    expect(exit[0].marker.symbol).toBe('triangle-up-open');
  });

  it('falls back to neutral grey square for entries with zero weight', () => {
    // Backend rejects weight==0 entries, but the defensive fallback
    // must render a neutral square rather than crash.
    const events = [
      { input_id: 'X', block_id: 'b0', kind: 'entry', fired_indices: [1], latched_indices: [1] },
    ];
    const [trace] = buildEventMarkerTraces(
      events, positions, dates, undefined, { blockWeightSigns: { b0: 0 } },
    );
    expect(trace.marker.symbol).toBe('square');
    expect(trace.marker.color).toBe(EVENT_MARKER.entry[0].color);
  });

  it('falls back to neutral for events whose block_id is missing from the sign map', () => {
    // Omitting the sign for a block should not throw — render neutral.
    const events = [
      { input_id: 'X', block_id: 'unknown', kind: 'entry', fired_indices: [1] },
    ];
    const [trace] = buildEventMarkerTraces(
      events, positions, dates, undefined, { blockWeightSigns: {} },
    );
    expect(trace.marker.symbol).toBe('square'); // neutral entry symbol
    expect(trace.marker.color).toBe(EVENT_MARKER.entry[0].color);
  });

  it('uses fired_indices by default even when latched_indices is absent', () => {
    const events = [
      { input_id: 'X', block_id: 'b1', kind: 'entry', fired_indices: [1] },
    ];
    const [trace] = buildEventMarkerTraces(
      events, positions, dates, undefined, { blockWeightSigns: { b1: 1 } },
    );
    expect(trace.x).toHaveLength(1);
    expect(trace.y).toEqual([101]);
  });

  it('skips events whose input has no price (no synthesis)', () => {
    const pos = [{ input_id: 'X', price: null }];
    const events = [{ input_id: 'X', block_id: 'b1', kind: 'entry', fired_indices: [0] }];
    expect(buildEventMarkerTraces(
      events, pos, dates, undefined, { blockWeightSigns: { b1: 1 } },
    )).toHaveLength(0);
  });

  it('drops bars where the price is null (no synthesis)', () => {
    const pos = [positionWithPrice('X', [100, null, 102])];
    const events = [{ input_id: 'X', block_id: 'b1', kind: 'entry', fired_indices: [0, 1, 2] }];
    const [trace] = buildEventMarkerTraces(
      events, pos, dates, undefined, { blockWeightSigns: { b1: 1 } },
    );
    expect(trace.x).toHaveLength(2);
    expect(trace.y).toEqual([100, 102]);
  });

  it('ignores unknown kinds gracefully', () => {
    const events = [{ input_id: 'X', block_id: 'b1', kind: 'not_a_kind', fired_indices: [0] }];
    expect(buildEventMarkerTraces(
      events, positions, dates, undefined, { blockWeightSigns: { b1: 1 } },
    )).toHaveLength(0);
  });

  it('returns [] for empty inputs', () => {
    expect(buildEventMarkerTraces([], positions, dates)).toEqual([]);
    expect(buildEventMarkerTraces(undefined, positions, dates)).toEqual([]);
  });

  it('sets yaxis on traces when yaxis parameter is provided', () => {
    const events = [
      { input_id: 'X', block_id: 'b1', kind: 'entry', fired_indices: [0] },
    ];
    const traces = buildEventMarkerTraces(
      events, positions, dates, 'y2', { blockWeightSigns: { b1: 1 } },
    );
    expect(traces[0].yaxis).toBe('y2');
  });
});

describe('buildIndicatorTraces', () => {
  it('emits one line per indicator entry on y2 by default', () => {
    const inds = [
      { input_id: 'X', indicator_id: 'sma', series: [1, 2, 3] },
      { input_id: 'X', indicator_id: 'rsi', series: [4, 5, 6] },
    ];
    const traces = buildIndicatorTraces(inds, dates);
    expect(traces).toHaveLength(2);
    expect(traces[0].yaxis).toBe('y2');
    expect(traces[0].name).toMatch(/sma/);
    expect(traces[0].line.dash).toBe('dot');
  });

  it('uses custom yaxis when provided', () => {
    const inds = [{ input_id: 'X', indicator_id: 'sma', series: [1, 2, 3] }];
    const traces = buildIndicatorTraces(inds, dates, 0, 'y5');
    expect(traces[0].yaxis).toBe('y5');
  });

  it('skips entries without a series array', () => {
    const inds = [{ indicator_id: 'x', series: null }];
    expect(buildIndicatorTraces(inds, dates)).toHaveLength(0);
  });
});

describe('partitionIndicators', () => {
  it('separates ownPanel indicators from overlay indicators', () => {
    const indicators = [
      { indicator_id: 'sma', ownPanel: false, series: [1, 2, 3] },
      { indicator_id: 'rsi', ownPanel: true, series: [50, 60, 70] },
      { indicator_id: 'ema', series: [4, 5, 6] },
      { indicator_id: 'macd', ownPanel: true, series: [0.1, 0.2, 0.3] },
    ];
    const { overlay, ownPanel } = partitionIndicators(indicators);
    expect(overlay).toHaveLength(2);
    expect(ownPanel).toHaveLength(2);
    expect(overlay[0].indicator_id).toBe('sma');
    expect(overlay[1].indicator_id).toBe('ema');
    expect(ownPanel[0].indicator_id).toBe('rsi');
    expect(ownPanel[1].indicator_id).toBe('macd');
  });

  it('returns empty arrays for non-array input', () => {
    const { overlay, ownPanel } = partitionIndicators(undefined);
    expect(overlay).toEqual([]);
    expect(ownPanel).toEqual([]);
  });

  it('puts all indicators in overlay when none have ownPanel', () => {
    const indicators = [
      { indicator_id: 'sma', series: [1, 2] },
      { indicator_id: 'ema', ownPanel: false, series: [3, 4] },
    ];
    const { overlay, ownPanel } = partitionIndicators(indicators);
    expect(overlay).toHaveLength(2);
    expect(ownPanel).toHaveLength(0);
  });
});

/* ================================================================== */
/*  computeSubplotDomains                                              */
/* ================================================================== */

describe('computeSubplotDomains', () => {
  it('returns 2 domains when there are no ownPanel indicators', () => {
    const domains = computeSubplotDomains(0);
    expect(domains).toHaveLength(2);
    // Top domain should be higher than bottom domain
    expect(domains[0][1]).toBeGreaterThan(domains[1][1]);
    // All values between 0 and 1
    for (const [lo, hi] of domains) {
      expect(lo).toBeGreaterThanOrEqual(0);
      expect(hi).toBeLessThanOrEqual(1);
      expect(hi).toBeGreaterThan(lo);
    }
  });

  it('returns 2 + N domains for N ownPanel indicators', () => {
    const domains = computeSubplotDomains(3);
    expect(domains).toHaveLength(5);
    // Each domain has lo < hi
    for (const [lo, hi] of domains) {
      expect(hi).toBeGreaterThan(lo);
    }
  });

  it('domains are ordered top-down (highest first)', () => {
    const domains = computeSubplotDomains(2);
    for (let i = 0; i < domains.length - 1; i++) {
      // Upper bound of domain[i] > upper bound of domain[i+1]
      expect(domains[i][1]).toBeGreaterThan(domains[i + 1][1]);
    }
  });

  it('domains do not overlap', () => {
    const domains = computeSubplotDomains(2);
    // Sorted top-down, so domain[i].lower >= domain[i+1].upper
    for (let i = 0; i < domains.length - 1; i++) {
      expect(domains[i][0]).toBeGreaterThanOrEqual(domains[i + 1][1]);
    }
  });

  it('all domains fit within [0, 1]', () => {
    const domains = computeSubplotDomains(5);
    for (const [lo, hi] of domains) {
      expect(lo).toBeGreaterThanOrEqual(0);
      expect(hi).toBeLessThanOrEqual(1);
    }
  });
});

/* ================================================================== */
/*  buildResultsPlot — unified subplot builder                         */
/* ================================================================== */

describe('buildResultsPlot', () => {
  it('returns empty when result is null', () => {
    const r = buildResultsPlot(null);
    expect(r.hasData).toBe(false);
    expect(r.traces).toEqual([]);
  });

  it('returns empty when timestamps array is empty', () => {
    const r = buildResultsPlot({ timestamps: [], positions: [] });
    expect(r.hasData).toBe(false);
  });

  it('returns traces with correct structure for basic result', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      realized_pnl: [[0, 1, 2]],
      indicators: [],
      events: [],
    };
    const { traces, layoutOverrides, hasData } = buildResultsPlot(result);
    expect(hasData).toBe(true);
    // Top subplot: P&L + capital. Bottom subplot: 1 price.
    expect(traces.length).toBeGreaterThanOrEqual(3);
    // layoutOverrides should at least have yaxis + yaxis2
    expect(layoutOverrides.yaxis).toBeDefined();
    expect(layoutOverrides.yaxis2).toBeDefined();
  });

  it('all traces share the same x-axis (no xaxis2/xaxis3)', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      realized_pnl: [[0, 1, 2]],
      indicators: [
        { input_id: 'X', indicator_id: 'sma', series: [1, 2, 3], ownPanel: false },
        { input_id: 'X', indicator_id: 'rsi', series: [50, 60, 70], ownPanel: true },
      ],
      events: [],
    };
    const { traces, layoutOverrides } = buildResultsPlot(result);
    // No trace should reference xaxis other than default 'x'
    for (const t of traces) {
      if (t.xaxis) {
        expect(t.xaxis).toBe('x');
      }
    }
    // Layout should only have one xaxis
    expect(layoutOverrides.xaxis).toBeDefined();
    expect(layoutOverrides.xaxis2).toBeUndefined();
    expect(layoutOverrides.xaxis3).toBeUndefined();
  });

  it('has no yaxis with side:right anywhere', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      realized_pnl: [[0, 1, 2]],
      indicators: [
        { input_id: 'X', indicator_id: 'sma', series: [1, 2, 3], ownPanel: false },
        { input_id: 'X', indicator_id: 'rsi', series: [50, 60, 70], ownPanel: true },
      ],
      events: [],
    };
    const { layoutOverrides } = buildResultsPlot(result);
    for (const [key, value] of Object.entries(layoutOverrides)) {
      if (key.startsWith('yaxis') && typeof value === 'object') {
        expect(value.side).not.toBe('right');
      }
    }
  });

  it('assigns top subplot traces (prices + P&L + capital) to default y-axis', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      realized_pnl: [[0, 1, 2]],
      indicators: [],
      events: [],
    };
    const { traces } = buildResultsPlot(result);
    // Top subplot price trace should have no yaxis (defaults to 'y')
    const topPrice = traces.find((t) => t.name.includes('X') && !t.yaxis);
    expect(topPrice).toBeDefined();
    // P&L trace should be on default y-axis (no yaxis property)
    const pnl = traces.find((t) => t.name === 'realized P&L');
    expect(pnl).toBeDefined();
    expect(pnl.yaxis).toBeUndefined();
    // Capital trace should also be on default y-axis
    const cap = traces.find((t) => t.name === 'capital');
    expect(cap).toBeDefined();
    expect(cap.yaxis).toBeUndefined();
  });

  it('assigns bottom subplot traces (including event markers) to y2', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      indicators: [{ input_id: 'X', indicator_id: 'sma', series: [1, 2, 3] }],
      events: [{
        input_id: 'X', block_id: 'b1', kind: 'entry',
        fired_indices: [1], latched_indices: [1], active_indices: [1],
      }],
    };
    const signalRules = {
      entries: [{ id: 'b1', input_id: 'X', weight: 50, conditions: [] }],
      exits: [],
    };
    const { traces } = buildResultsPlot(result, { signalRules });
    // Bottom subplot price traces should be on y2
    const bottomPrices = traces.filter((t) => t.name.includes('X') && t.yaxis === 'y2');
    expect(bottomPrices.length).toBeGreaterThanOrEqual(1);
    // Indicator traces on y2
    const indTraces = traces.filter((t) => t.name && t.name.startsWith('ind:'));
    expect(indTraces.length).toBeGreaterThanOrEqual(1);
    for (const t of indTraces) {
      expect(t.yaxis).toBe('y2');
    }
    // Event marker traces on y2 — positive-weight entry → "long entry" label
    const eventTraces = traces.filter((t) => t.name && t.name.includes('long entry'));
    expect(eventTraces.length).toBeGreaterThanOrEqual(1);
    for (const t of eventTraces) {
      expect(t.yaxis).toBe('y2');
    }
  });

  it('picks long vs short markers via signalRules passed through opts', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      indicators: [],
      events: [
        { input_id: 'X', block_id: 'b1', kind: 'entry', fired_indices: [0], latched_indices: [0] },
        { input_id: 'X', block_id: 'b2', kind: 'entry', fired_indices: [1], latched_indices: [1] },
      ],
    };
    const signalRules = {
      entries: [
        { id: 'b1', input_id: 'X', weight: 40, conditions: [] },   // long
        { id: 'b2', input_id: 'X', weight: -60, conditions: [] },  // short
      ],
      exits: [],
    };
    const { traces } = buildResultsPlot(result, { signalRules });
    const longTrace = traces.find((t) => t.name && t.name.startsWith('long entry'));
    const shortTrace = traces.find((t) => t.name && t.name.startsWith('short entry'));
    expect(longTrace).toBeDefined();
    expect(shortTrace).toBeDefined();
    expect(longTrace.marker.color).toBe('#10b981');
    expect(shortTrace.marker.color).toBe('#ef4444');
  });

  it('assigns ownPanel indicators to y3, y4, etc.', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      indicators: [
        { input_id: 'X', indicator_id: 'rsi', series: [50, 60, 70], ownPanel: true },
        { input_id: 'X', indicator_id: 'macd', series: [0.1, 0.2, 0.3], ownPanel: true },
      ],
      events: [],
    };
    const { traces, layoutOverrides } = buildResultsPlot(result);
    const rsiTrace = traces.find((t) => t.name && t.name.includes('rsi'));
    const macdTrace = traces.find((t) => t.name && t.name.includes('macd'));
    expect(rsiTrace.yaxis).toBe('y3');
    expect(macdTrace.yaxis).toBe('y4');
    // Layout should have matching yaxis entries with domain arrays
    expect(layoutOverrides.yaxis3).toBeDefined();
    expect(layoutOverrides.yaxis3.title.text).toBe('rsi');
    expect(layoutOverrides.yaxis3.domain).toBeDefined();
    expect(layoutOverrides.yaxis4).toBeDefined();
    expect(layoutOverrides.yaxis4.title.text).toBe('macd');
    expect(layoutOverrides.yaxis4.domain).toBeDefined();
  });

  it('layout has domain arrays on yaxis and yaxis2', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      indicators: [],
      events: [],
    };
    const { layoutOverrides } = buildResultsPlot(result);
    expect(layoutOverrides.yaxis.domain).toBeDefined();
    expect(layoutOverrides.yaxis.domain).toHaveLength(2);
    expect(layoutOverrides.yaxis2.domain).toBeDefined();
    expect(layoutOverrides.yaxis2.domain).toHaveLength(2);
    // Top domain is higher than bottom domain
    expect(layoutOverrides.yaxis.domain[1]).toBeGreaterThan(layoutOverrides.yaxis2.domain[1]);
  });

  it('excludes ownPanel indicators with empty series', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      indicators: [
        { input_id: 'X', indicator_id: 'empty', series: [], ownPanel: true },
      ],
      events: [],
    };
    const { layoutOverrides } = buildResultsPlot(result);
    // No yaxis3 because the empty ownPanel is excluded
    expect(layoutOverrides.yaxis3).toBeUndefined();
  });

  it('excludes ownPanel indicators from the bottom subplot overlay', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      indicators: [
        { input_id: 'X', indicator_id: 'sma', series: [1, 2, 3], ownPanel: false },
        { input_id: 'X', indicator_id: 'rsi', series: [50, 60, 70], ownPanel: true },
      ],
      events: [],
    };
    const { traces } = buildResultsPlot(result);
    // sma should appear as overlay on y2
    const smaTrace = traces.find((t) => t.name && t.name.includes('sma'));
    expect(smaTrace).toBeDefined();
    expect(smaTrace.yaxis).toBe('y2');
    // rsi should NOT appear on y2
    const rsiTraces = traces.filter((t) => t.name && t.name.includes('rsi'));
    for (const t of rsiTraces) {
      expect(t.yaxis).not.toBe('y2');
    }
  });
});

/* ================================================================== */
/*  countOwnPanelIndicators                                            */
/* ================================================================== */

describe('countOwnPanelIndicators', () => {
  it('returns 0 for null result', () => {
    expect(countOwnPanelIndicators(null)).toBe(0);
  });

  it('returns 0 when no ownPanel indicators', () => {
    const result = {
      indicators: [{ indicator_id: 'sma', series: [1], ownPanel: false }],
    };
    expect(countOwnPanelIndicators(result)).toBe(0);
  });

  it('counts only ownPanel indicators with data', () => {
    const result = {
      indicators: [
        { indicator_id: 'rsi', series: [50, 60], ownPanel: true },
        { indicator_id: 'macd', series: [0.1], ownPanel: true },
        { indicator_id: 'empty', series: [], ownPanel: true },
        { indicator_id: 'sma', series: [1, 2], ownPanel: false },
      ],
    };
    expect(countOwnPanelIndicators(result)).toBe(2);
  });
});

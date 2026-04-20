// Pure-JS tests for the Results 2-plot trace helpers (iter-5 ask #6).
//
// Covers trace construction without rendering React / Plotly.

import { describe, it, expect } from 'vitest';
import {
  buildInputTraces,
  aggregateRealizedPnl,
  buildTopPlot,
  buildBottomPlot,
  buildOwnPanelPlots,
  partitionIndicators,
  buildEventMarkerTraces,
  buildIndicatorTraces,
  buildClipSummary,
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

describe('buildTopPlot', () => {
  it('returns empty when timestamps are missing', () => {
    const r = buildTopPlot({ positions: [] });
    expect(r.hasData).toBe(false);
  });

  it('includes price traces only when pnl missing', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
    };
    const { traces, layoutOverrides, hasData } = buildTopPlot(result);
    expect(hasData).toBe(true);
    expect(traces).toHaveLength(1);
    expect(layoutOverrides.yaxis2).toBeUndefined();
  });

  it('adds a realized P&L trace on a right y-axis when payload provided', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [1, 2, 3])],
      realized_pnl: [[0, 1, 2]],
    };
    const { traces, layoutOverrides } = buildTopPlot(result);
    expect(traces).toHaveLength(2);
    const pnl = traces.find((t) => t.name === 'realized P&L');
    expect(pnl).toBeDefined();
    expect(pnl.yaxis).toBe('y2');
    expect(layoutOverrides.yaxis2.overlaying).toBe('y');
    expect(layoutOverrides.yaxis2.side).toBe('right');
  });
});

describe('buildEventMarkerTraces', () => {
  const positions = [positionWithPrice('X', [100, 101, 102])];
  it('emits markers at latched_indices on the price line', () => {
    const events = [
      { input_id: 'X', block_id: 'b1', kind: 'long_entry', fired_indices: [0], latched_indices: [0, 2] },
    ];
    const traces = buildEventMarkerTraces(events, positions, dates);
    expect(traces).toHaveLength(1);
    expect(traces[0].mode).toBe('markers');
    expect(traces[0].x).toHaveLength(2);
    expect(traces[0].y).toEqual([100, 102]);
    expect(traces[0].marker.symbol).toBe(EVENT_MARKER.long_entry.symbol);
    expect(traces[0].marker.color).toBe(EVENT_MARKER.long_entry.color);
  });

  it('uses open variants for exit kinds', () => {
    const events = [
      { input_id: 'X', block_id: 'b1', kind: 'long_exit', latched_indices: [1] },
    ];
    const [trace] = buildEventMarkerTraces(events, positions, dates);
    expect(trace.marker.symbol).toBe('triangle-down-open');
  });

  it('applies the short_entry / short_exit colour convention', () => {
    const entry = buildEventMarkerTraces(
      [{ input_id: 'X', block_id: 'b2', kind: 'short_entry', latched_indices: [1] }],
      positions, dates,
    );
    const exit = buildEventMarkerTraces(
      [{ input_id: 'X', block_id: 'b3', kind: 'short_exit', latched_indices: [2] }],
      positions, dates,
    );
    expect(entry[0].marker.color).toBe('#ef4444');
    expect(entry[0].marker.symbol).toBe('triangle-down');
    expect(exit[0].marker.color).toBe('#ef4444');
    expect(exit[0].marker.symbol).toBe('triangle-up-open');
  });

  it('falls back to fired_indices when latched_indices is absent', () => {
    const events = [
      { input_id: 'X', block_id: 'b1', kind: 'long_entry', fired_indices: [1] },
    ];
    const [trace] = buildEventMarkerTraces(events, positions, dates);
    expect(trace.x).toHaveLength(1);
    expect(trace.y).toEqual([101]);
  });

  it('skips events whose input has no price (no synthesis)', () => {
    const pos = [{ input_id: 'X', price: null }];
    const events = [{ input_id: 'X', kind: 'long_entry', latched_indices: [0] }];
    expect(buildEventMarkerTraces(events, pos, dates)).toHaveLength(0);
  });

  it('drops bars where the price is null (no synthesis)', () => {
    const pos = [positionWithPrice('X', [100, null, 102])];
    const events = [{ input_id: 'X', kind: 'long_entry', latched_indices: [0, 1, 2] }];
    const [trace] = buildEventMarkerTraces(events, pos, dates);
    expect(trace.x).toHaveLength(2);
    expect(trace.y).toEqual([100, 102]);
  });

  it('ignores unknown kinds gracefully', () => {
    const events = [{ input_id: 'X', kind: 'not_a_kind', latched_indices: [0] }];
    expect(buildEventMarkerTraces(events, positions, dates)).toHaveLength(0);
  });

  it('returns [] for empty inputs', () => {
    expect(buildEventMarkerTraces([], positions, dates)).toEqual([]);
    expect(buildEventMarkerTraces(undefined, positions, dates)).toEqual([]);
  });
});

describe('buildIndicatorTraces', () => {
  it('emits one line per indicator entry on y2', () => {
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

  it('skips entries without a series array', () => {
    const inds = [{ indicator_id: 'x', series: null }];
    expect(buildIndicatorTraces(inds, dates)).toHaveLength(0);
  });
});

describe('buildBottomPlot', () => {
  it('merges inputs + indicators + events into one trace list', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [10, 20, 30])],
      indicators: [{ input_id: 'X', indicator_id: 'sma', series: [11, 19, 29] }],
      events: [{ input_id: 'X', block_id: 'b1', kind: 'long_entry', latched_indices: [1] }],
    };
    const { traces, layoutOverrides, hasData } = buildBottomPlot(result);
    expect(hasData).toBe(true);
    // 1 input + 1 indicator + 1 event-marker trace
    expect(traces).toHaveLength(3);
    expect(layoutOverrides.yaxis2.side).toBe('right');
  });

  it('omits the y2 axis when there are no indicators', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [10, 20, 30])],
      indicators: [],
      events: [],
    };
    const { layoutOverrides } = buildBottomPlot(result);
    expect(layoutOverrides.yaxis2).toBeUndefined();
  });
});

describe('buildClipSummary', () => {
  it('returns null when result.clipped is false', () => {
    expect(buildClipSummary({ clipped: false, positions: [] })).toBeNull();
  });

  it('lists each position with a positive clip count', () => {
    const result = {
      clipped: true,
      positions: [
        { input_id: 'X', clipped_mask: [true, false, true] },
        { input_id: 'Y', clipped_mask: [false, false, false] },
        { input_id: 'Z', clipped_mask: [true] },
      ],
    };
    const sum = buildClipSummary(result);
    expect(sum.rows).toEqual([
      { instrument: 'X', count: 2 },
      { instrument: 'Z', count: 1 },
    ]);
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

describe('buildBottomPlot — ownPanel filtering', () => {
  it('excludes ownPanel indicators from the bottom plot', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [10, 20, 30])],
      indicators: [
        { input_id: 'X', indicator_id: 'sma', series: [11, 19, 29], ownPanel: false },
        { input_id: 'X', indicator_id: 'rsi', series: [50, 60, 70], ownPanel: true },
      ],
      events: [],
    };
    const { traces } = buildBottomPlot(result);
    const indicatorNames = traces.filter((t) => t.name && t.name.startsWith('ind:')).map((t) => t.name);
    expect(indicatorNames.some((n) => n.includes('sma'))).toBe(true);
    expect(indicatorNames.some((n) => n.includes('rsi'))).toBe(false);
  });
});

describe('buildOwnPanelPlots', () => {
  it('returns empty array when no ownPanel indicators', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [10, 20, 30])],
      indicators: [
        { input_id: 'X', indicator_id: 'sma', series: [11, 19, 29], ownPanel: false },
      ],
      events: [],
    };
    expect(buildOwnPanelPlots(result)).toHaveLength(0);
  });

  it('returns empty array when result is null', () => {
    expect(buildOwnPanelPlots(null)).toHaveLength(0);
  });

  it('returns one plot per ownPanel indicator with correct structure', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [10, 20, 30])],
      indicators: [
        { input_id: 'X', indicator_id: 'sma', series: [11, 19, 29], ownPanel: false },
        { input_id: 'X', indicator_id: 'rsi', series: [50, 60, 70], ownPanel: true },
        { input_id: 'X', indicator_id: 'macd', series: [0.1, 0.2, 0.3], ownPanel: true },
      ],
      events: [],
    };
    const panels = buildOwnPanelPlots(result);
    expect(panels).toHaveLength(2);

    // First panel: rsi
    expect(panels[0].title).toBe('rsi-X');
    expect(panels[0].downloadFilename).toBe('signal-indicator-rsi-X');
    expect(panels[0].hasData).toBe(true);
    // Should have input traces + 1 indicator trace
    const rsiTraces = panels[0].traces;
    expect(rsiTraces.length).toBeGreaterThanOrEqual(2); // at least 1 input + 1 indicator
    const indTrace = rsiTraces.find((t) => t.yaxis === 'y2');
    expect(indTrace).toBeDefined();
    expect(indTrace.y).toEqual([50, 60, 70]);
    expect(indTrace.name).toMatch(/rsi/);
    // Layout has y2 for the indicator
    expect(panels[0].layoutOverrides.yaxis2.side).toBe('right');
    expect(panels[0].layoutOverrides.yaxis2.title.text).toBe('rsi');

    // Second panel: macd
    expect(panels[1].title).toBe('macd-X');
    expect(panels[1].downloadFilename).toBe('signal-indicator-macd-X');
  });

  it('marks hasData false when indicator series is empty', () => {
    const result = {
      timestamps: ts,
      positions: [positionWithPrice('X', [10, 20, 30])],
      indicators: [
        { input_id: 'X', indicator_id: 'empty', series: [], ownPanel: true },
      ],
      events: [],
    };
    const panels = buildOwnPanelPlots(result);
    expect(panels).toHaveLength(1);
    expect(panels[0].hasData).toBe(false);
  });
});

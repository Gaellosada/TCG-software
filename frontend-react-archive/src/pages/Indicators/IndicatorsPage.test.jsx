// @vitest-environment jsdom
//
// Unit tests for helpers exported from IndicatorsPage.jsx.
//
// Scope is deliberately narrow — the rest of the page is exercised via
// its constituent components' tests (EditorPanel, IndicatorsList,
// ParamsPanel, etc.). This file pins down invariants that only make
// sense at the page-level glue code.
//
// The shared Chart component is mocked so importing IndicatorsPage does
// NOT pull Plotly (which references ``self``) into the jsdom environment.

import { describe, it, expect, vi } from 'vitest';

vi.mock('../../components/Chart', () => {
  // eslint-disable-next-line react/prop-types
  function ChartStub() { return null; }
  return { default: ChartStub };
});

// Import AFTER vi.mock so the stub is wired.
import { hydrateDefault } from './IndicatorsPage';

// Minimal parseable Python signature so paramParser produces a spec.
// Uses a single ``close`` series and one int param — mirrors the SMA
// entry's shape without pulling in the real registry.
const FAKE_CODE = `def compute(series, window: int = 20):
    s = series['close']
    return s`;

function makeDef(overrides = {}) {
  return {
    id: 'fake',
    name: 'Fake',
    readonly: true,
    category: 'trend',
    code: FAKE_CODE,
    params: {},
    seriesMap: {},
    doc: 'fake doc',
    ownPanel: false,
    ...overrides,
  };
}

describe('hydrateDefault', () => {
  it('propagates registry chartMode into the hydrated object', () => {
    // Regression guard — prior to the round-trip fix, the explicit field
    // enumeration in hydrateDefault silently dropped ``chartMode``, so
    // any ``'markers'`` or ``'lines+markers'`` hint set by a registry
    // author was invisible at chart render time.
    const def = makeDef({ chartMode: 'markers' });
    const hydrated = hydrateDefault(def, undefined);
    expect(hydrated.chartMode).toBe('markers');
  });

  it('also propagates lines+markers hint', () => {
    const def = makeDef({ chartMode: 'lines+markers' });
    const hydrated = hydrateDefault(def, undefined);
    expect(hydrated.chartMode).toBe('lines+markers');
  });

  it('omits chartMode when the registry entry does not declare it', () => {
    // No ``chartMode`` key → hydrated object does not carry the key
    // either, so ``IndicatorChart``'s ``indicator?.chartMode || 'lines'``
    // fallback kicks in cleanly.
    const def = makeDef();
    const hydrated = hydrateDefault(def, undefined);
    expect(hydrated).not.toHaveProperty('chartMode');
  });

  it('keeps chartMode sourced from the registry even when a defaultState overlay is present', () => {
    // ``defaultState`` in localStorage only carries ``params`` and
    // ``seriesMap`` — ``chartMode`` is registry-only and MUST NOT be
    // overridable by stale or crafted localStorage content.
    const def = makeDef({ chartMode: 'markers' });
    const savedEntry = {
      params: { window: 42 },
      seriesMap: { close: 'close' },
      // Intentionally attempt to pollute chartMode via the saved entry.
      chartMode: 'lines',
    };
    const hydrated = hydrateDefault(def, savedEntry);
    expect(hydrated.chartMode).toBe('markers');
    // Sanity — user param overlay still wins for the fields it owns.
    expect(hydrated.params.window).toBe(42);
  });
});

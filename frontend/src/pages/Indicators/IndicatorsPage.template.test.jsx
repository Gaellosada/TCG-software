// @vitest-environment jsdom
//
// Tests for the AI-ready "new custom indicator" seed template
// (``NEW_CODE_TEMPLATE`` in IndicatorsPage.jsx). Two properties:
//
//   (a) PHRASES — the template carries the constraint phrases a user/AI needs
//       (pandas, import, np, the ``def compute(series`` signature, math,
//       f-strings) and contains no JS-template-literal hazards.
//   (b) SEEDING — creating a new custom indicator seeds the editor with exactly
//       this template.
//
// (a) asserts against the exported constant directly (fast, deterministic).
// (b) drives the real add flow through a full render and reads the value that
//     reaches EditorPanel. The backend ``test_indicator_template.py`` is the
//     anti-drift guard that the template actually validates + runs in the
//     sandbox — kept out of the frontend on purpose.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, act, cleanup } from '@testing-library/react';

// Mock Chart so importing the page doesn't pull Plotly (references ``self``).
vi.mock('../../components/Chart', () => ({ default: () => <div data-testid="chart-stub" /> }));

// Capture IndicatorsList callbacks (to drive onAdd) and EditorPanel props
// (to read the seeded code). We intentionally do NOT stub EditorPanel to an
// empty div — we need its ``code`` prop.
let listProps = {};
let editorProps = {};
vi.mock('./IndicatorsList', () => ({
  default: (props) => { listProps = props; return <div data-testid="list-stub" />; },
}));
vi.mock('./EditorPanel', () => ({
  default: (props) => { editorProps = props; return <div data-testid="editor-stub" />; },
}));
vi.mock('./ParamsPanel', () => ({ default: () => <div /> }));
vi.mock('./IndicatorChart', () => ({ default: () => <div /> }));

const resolveDefaultIndexInstrumentMock = vi.fn(() => Promise.resolve({ ok: true, data: null }));
vi.mock('../../api/indicators', () => ({
  computeIndicator: vi.fn(() => Promise.resolve({})),
  resolveDefaultIndexInstrument: (...a) => resolveDefaultIndexInstrumentMock(...a),
}));
vi.mock('../../api/options', () => ({
  getOptionRoots: vi.fn(() => Promise.resolve({ roots: [] })),
}));

// One hardcoded default so the page has a stable readonly entry.
vi.mock('./defaultIndicators', () => ({
  DEFAULT_INDICATORS: [{
    id: 'sma', name: 'SMA', readonly: true, category: 'trend',
    code: "def compute(series, window: int = 20):\n    s = series['close']\n    return s",
    params: { window: 20 }, seriesMap: {}, doc: '', ownPanel: false,
  }],
}));

const mockListIndicators = vi.fn(() => Promise.resolve([]));
const mockCreateIndicator = vi.fn(() => Promise.resolve({}));
vi.mock('../../api/persistence', () => ({
  listIndicators: (...a) => mockListIndicators(...a),
  listSignals: vi.fn(() => Promise.resolve([])),
  listPortfolios: vi.fn(() => Promise.resolve([])),
  createIndicator: (...a) => mockCreateIndicator(...a),
  updateIndicator: vi.fn(() => Promise.resolve({})),
  archiveIndicator: vi.fn(() => Promise.resolve(null)),
  setIndicatorLocked: vi.fn(() => Promise.resolve({})),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: () => false,
}));

// Import AFTER the mocks so wiring is in place. NEW_CODE_TEMPLATE is the
// exported seed constant.
import IndicatorsPage, { NEW_CODE_TEMPLATE } from './IndicatorsPage';

beforeEach(() => {
  listProps = {};
  editorProps = {};
  mockListIndicators.mockReset().mockResolvedValue([]);
  mockCreateIndicator.mockReset().mockResolvedValue({});
  try { localStorage.clear(); } catch { /* ignore */ }
});
afterEach(cleanup);

describe('NEW_CODE_TEMPLATE — constraint phrases (a)', () => {
  it('mentions the contract terms an AI needs to get compute() right', () => {
    // The header documents what tcg/engine/indicator_exec.py enforces.
    expect(NEW_CODE_TEMPLATE).toContain('def compute(series');
    expect(NEW_CODE_TEMPLATE).toContain('pandas'); // "no pandas" — nothing to import
    expect(NEW_CODE_TEMPLATE).toContain('import');
    expect(NEW_CODE_TEMPLATE).toContain('np');
    expect(NEW_CODE_TEMPLATE).toContain('math');
    expect(NEW_CODE_TEMPLATE).toContain('f-strings');
  });

  it('stays a valid JS template literal (no backtick / interpolation)', () => {
    // A backtick or ${ would terminate the literal or inject interpolation,
    // silently corrupting the seeded code.
    expect(NEW_CODE_TEMPLATE).not.toContain('`');
    expect(NEW_CODE_TEMPLATE).not.toContain('${');
  });
});

describe('IndicatorsPage — new custom indicator seeding (b)', () => {
  it('seeds the editor with NEW_CODE_TEMPLATE when a new indicator is added', async () => {
    render(<IndicatorsPage />);

    // Wait for mount: list query resolved and onAdd wired.
    await waitFor(() => expect(mockListIndicators).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(listProps.onAdd).toBeTypeOf('function'));

    // Drive the real add flow.
    await act(async () => { await listProps.onAdd(); });

    // The newly created indicator is auto-selected; its code flows to EditorPanel.
    await waitFor(() => expect(editorProps.code).toBe(NEW_CODE_TEMPLATE));
    // And it was persisted with the same seed code.
    expect(mockCreateIndicator).toHaveBeenCalledTimes(1);
    const created = mockCreateIndicator.mock.calls[0][0];
    expect(created.definition.code).toBe(NEW_CODE_TEMPLATE);
  });
});

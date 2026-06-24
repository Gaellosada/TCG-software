// @vitest-environment jsdom
//
// Iter-3 read-only consistency: when the loaded signal is locked, the editor
// inputs must be genuinely non-interactive (mirrors the Indicators page,
// where `readOnly` is threaded to EditorPanel). We render the REAL InputsPanel
// (only the heavy non-form children are stubbed) and assert a representative
// control — the "+ Add input" button — is disabled while locked and enabled
// while unlocked.
//
// signals-locked-view (iter-1): the blunt `<fieldset disabled>` was replaced
// by an explicit `readOnly` prop threaded into InputsPanel + BlockEditor so a
// locked signal stays VIEWABLE/NAVIGABLE (expand/collapse, tab switching) while
// only EDIT controls are disabled. The third test below now asserts the editor
// body is a plain wrapper (no disabled fieldset) — see
// SignalsPage.lockedView.test.jsx for the full view-vs-edit gating coverage.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

// Stub the block editor + side panels + list, but DO NOT stub InputsPanel —
// we want its real form controls present so we can assert native disabling.
vi.mock('./SignalsList', () => ({ default: () => <div data-testid="signals-list-stub" /> }));
vi.mock('./BlockEditor', () => ({ default: () => <div data-testid="block-editor-stub" /> }));
vi.mock('./ParamsPanel', () => ({ default: () => <div data-testid="params-panel-stub" /> }));
vi.mock('./ResultsView', () => ({ default: () => <div data-testid="results-view-stub" /> }));
vi.mock('../../components/Statistics', () => ({ default: () => <div data-testid="statistics-stub" /> }));
vi.mock('../../components/TradeLog', () => ({ default: () => <div data-testid="trade-log-stub" /> }));
vi.mock('./hydrateIndicators', () => ({ hydrateAvailableIndicators: () => Promise.resolve([]) }));
vi.mock('../../api/signals', () => ({
  computeSignal: vi.fn(),
  collectIndicatorIds: () => new Set(),
}));

const mockListSignals = vi.fn();
vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listSignals: (...a) => mockListSignals(...a),
  createSignal: vi.fn(() => Promise.resolve({})),
  updateSignal: vi.fn(() => Promise.resolve({})),
  archiveSignal: vi.fn(() => Promise.resolve(null)),
  setSignalLocked: vi.fn(() => Promise.resolve({})),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: (err) => !!err && err.status === 423,
}));

import SignalsPage from './SignalsPage';

function persisted(over = {}) {
  return {
    id: 's1', name: 'Sig 1', inputs: [], rules: { entries: [], exits: [], resets: [] },
    settings: { dont_repeat: true }, description: '', locked: false, ...over,
  };
}

beforeEach(() => {
  mockListSignals.mockReset();
  try { localStorage.clear(); } catch { /* ignore */ }
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('SignalsPage — read-only editor inputs when locked', () => {
  it('disables the InputsPanel "+ Add input" control when the loaded signal is locked', async () => {
    mockListSignals.mockResolvedValue([persisted({ id: 's1', locked: true })]);
    render(<SignalsPage />);
    await screen.findByTestId('signal-lock-banner');
    // The real InputsPanel renders its "+ Add input" button. It is gated with
    // an explicit `disabled={readOnly}` (the readOnly prop is true when the
    // signal is locked), so the IDL `.disabled` flag is set directly — no
    // reliance on an ancestor fieldset.
    const addBtn = await screen.findByTestId('inputs-add-btn');
    expect(addBtn.disabled).toBe(true);
  });

  it('leaves the InputsPanel "+ Add input" control enabled when the loaded signal is unlocked', async () => {
    mockListSignals.mockResolvedValue([persisted({ id: 's1', locked: false })]);
    render(<SignalsPage />);
    const addBtn = await screen.findByTestId('inputs-add-btn');
    expect(addBtn.disabled).toBe(false);
    expect(screen.queryByTestId('signal-lock-banner')).toBeNull();
  });

  it('renders the editor body as a plain (non-fieldset) wrapper — readOnly is threaded, not a disabled fieldset', async () => {
    mockListSignals.mockResolvedValue([persisted({ id: 's1', locked: true })]);
    const { unmount } = render(<SignalsPage />);
    await screen.findByTestId('signal-lock-banner');
    // The old blunt <fieldset disabled> is gone — it disabled view-navigation
    // affordances too. The editor body is now a plain div; read-only is driven
    // by the `readOnly` prop on InputsPanel/BlockEditor.
    expect(screen.queryByTestId('signal-editor-fieldset')).toBeNull();
    const body = screen.getByTestId('signal-editor-body');
    expect(body.tagName).toBe('DIV');
    unmount();
  });
});

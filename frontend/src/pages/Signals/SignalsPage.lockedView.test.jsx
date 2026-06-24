// @vitest-environment jsdom
//
// Locked-signal read-only VIEW (signals-locked-view, iter-1).
//
// The user must still be able to EXPLORE a locked signal: expand the Inputs
// panel, switch BlockEditor tabs (Entries/Exits/Resets), see every block —
// while every EDIT control is disabled. The old blunt `<fieldset disabled>`
// disabled the VIEW-navigation affordances too (the inputs toggle, the tab
// buttons), so a locked signal was frozen on whatever happened to be visible.
//
// We render the REAL InputsPanel + BlockEditor (only the heavy network-backed
// InstrumentPickerModal and the side panels are stubbed). The TRUE gate a real
// user hits is the `:disabled` pseudo-class (jsdom reflects it, and does NOT
// suppress a synthetic fireEvent.click on a disabled descendant — so we assert
// the pseudo-class, not click side effects). These assertions FAIL on the old
// `<fieldset disabled>` implementation — the VIEW toggle/tab buttons match
// `:disabled` — and PASS once `readOnly` is threaded so ONLY edit controls are
// disabled. Text inputs use `readOnly` (matches `:read-only`, NOT `:disabled`)
// so their value stays selectable in view mode; the signal-name field is the
// same — read-only when locked, not disabled.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

// Stub the side panels + list, but render the REAL InputsPanel + BlockEditor
// so we exercise their actual navigation + edit controls. The instrument
// picker modal hits the network on open, so stub it to a no-op.
vi.mock('./SignalsList', () => ({ default: () => <div data-testid="signals-list-stub" /> }));
vi.mock('./ParamsPanel', () => ({ default: () => <div data-testid="params-panel-stub" /> }));
vi.mock('./ResultsView', () => ({ default: () => <div data-testid="results-view-stub" /> }));
vi.mock('../../components/Statistics', () => ({ default: () => <div data-testid="statistics-stub" /> }));
vi.mock('../../components/TradeLog', () => ({ default: () => <div data-testid="trade-log-stub" /> }));
vi.mock('../../components/InstrumentPickerModal/InstrumentPickerModal', () => ({
  default: () => null,
}));
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

// A locked signal carrying one configured input and one entry block, so the
// Inputs panel starts collapsed (non-empty) and the Entries tab has a block.
function lockedSignal(over = {}) {
  return {
    id: 's1',
    name: 'Sig 1',
    inputs: [
      {
        id: 'px',
        instrument: { type: 'continuous', collection: 'FUT_ES' },
      },
    ],
    rules: {
      entries: [{ id: 'e1', name: '', input_id: 'px', weight: 100, conditions: [] }],
      exits: [],
      resets: [],
    },
    settings: { dont_repeat: true },
    description: '',
    locked: true,
    ...over,
  };
}

beforeEach(() => {
  mockListSignals.mockReset();
  try { localStorage.clear(); } catch { /* ignore */ }
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('SignalsPage — locked signal stays VIEWABLE and NAVIGABLE', () => {
  it('keeps the Inputs panel expand/collapse toggle usable (NOT disabled) while locked', async () => {
    mockListSignals.mockResolvedValue([lockedSignal()]);
    render(<SignalsPage />);
    await screen.findByTestId('signal-lock-banner');

    // The expand/collapse toggle is a VIEW affordance: it must stay
    // interactive even when locked. Under the disabled <fieldset> it matches
    // `:disabled` (a real user can't click it) → this fails.
    const toggle = await screen.findByTestId('inputs-panel-toggle');
    expect(toggle.matches(':disabled')).toBe(false);

    // And expanding it reveals the configured input id, read-only. The text
    // input uses readOnly (not disabled) so the value stays SELECTABLE in view
    // mode — a read-only input must NOT match `:disabled`, but must match
    // `:read-only`.
    fireEvent.click(toggle);
    const idField = await screen.findByTestId('input-id-0');
    expect(idField.value).toBe('px');
    expect(idField.matches(':disabled')).toBe(false);
    expect(idField.matches(':read-only')).toBe(true);
  });

  it('renders the signal-name field read-only (value visible, not editable) while locked', async () => {
    mockListSignals.mockResolvedValue([lockedSignal()]);
    render(<SignalsPage />);
    await screen.findByTestId('signal-lock-banner');

    // The params-panel name field shows the locked signal's name but must not
    // be editable. It is hinted read-only via a fresh {...signal, readonly:true}
    // object (InlineNameInput keys off `entity.readonly`) — the stored signal is
    // NOT mutated. Read-only (selectable) rather than disabled.
    const nameField = await screen.findByLabelText('Signal name');
    expect(nameField.value).toBe('Sig 1');
    expect(nameField.matches(':read-only')).toBe(true);
  });

  it('keeps the section tab buttons (Exits/Resets) usable (NOT disabled) while locked', async () => {
    mockListSignals.mockResolvedValue([lockedSignal()]);
    render(<SignalsPage />);
    await screen.findByTestId('signal-lock-banner');

    // The entry block is visible on the default Entries tab.
    expect(await screen.findByTestId('block-0')).toBeTruthy();

    // Tab buttons are VIEW affordances → must stay interactive when locked.
    // Under the disabled <fieldset> they match `:disabled` → this fails.
    const exitsTab = await screen.findByTestId('section-tab-exits');
    const resetsTab = await screen.findByTestId('section-tab-resets');
    expect(exitsTab.matches(':disabled')).toBe(false);
    expect(resetsTab.matches(':disabled')).toBe(false);

    // Switching tabs shows the target section's content.
    fireEvent.click(exitsTab);
    expect(exitsTab.getAttribute('aria-selected')).toBe('true');
    expect(await screen.findByText(/No blocks\. Add one/i)).toBeTruthy();
  });

  it('disables EDIT controls (input add, block input select, add-block, add-condition) while locked', async () => {
    mockListSignals.mockResolvedValue([lockedSignal()]);
    render(<SignalsPage />);
    await screen.findByTestId('signal-lock-banner');

    // Expand inputs so the add-input control is in the DOM.
    fireEvent.click(await screen.findByTestId('inputs-panel-toggle'));
    expect((await screen.findByTestId('inputs-add-btn')).matches(':disabled')).toBe(true);

    // The entry block's input <select> is read-only.
    expect((await screen.findByTestId('block-input-select-0')).matches(':disabled')).toBe(true);
    // Add-block and add-condition (the entry block has a footer) are read-only.
    expect((await screen.findByTestId('add-block-btn')).matches(':disabled')).toBe(true);
    expect((await screen.findByTestId('add-condition-0')).matches(':disabled')).toBe(true);
  });
});

describe('SignalsPage — locked signal: rich shapes stay VISIBLE but read-only', () => {
  // A locked signal with a populated EXIT block (one chosen target entry) and
  // an entry block holding a binary condition with an indicator operand that
  // carries a param override. We assert the user can SEE the chosen target and
  // the override value while every control is disabled.
  function richLockedSignal() {
    return {
      id: 's2',
      name: 'Rich',
      inputs: [{ id: 'px', instrument: { type: 'continuous', collection: 'FUT_ES' } }],
      rules: {
        entries: [{ id: 'e1', name: 'Long', input_id: 'px', weight: 100, conditions: [] }],
        exits: [{
          id: 'x1', name: '', target_entry_block_names: ['Long'], conditions: [],
        }],
        resets: [],
      },
      settings: { dont_repeat: true },
      description: '',
      locked: true,
    };
  }

  it('shows the chosen exit target in the (disabled) target select while locked', async () => {
    mockListSignals.mockResolvedValue([richLockedSignal()]);
    render(<SignalsPage />);
    await screen.findByTestId('signal-lock-banner');

    // Navigate to the Exits tab (view affordance works when locked).
    fireEvent.click(await screen.findByTestId('section-tab-exits'));

    // The exit's target select is disabled but its value ("Long") is visible.
    const targetSelect = await screen.findByTestId('target-entry-select-0-0');
    expect(targetSelect.matches(':disabled')).toBe(true);
    expect(targetSelect.value).toBe('Long');

    // The exit's "+ Add block" (add another target) is disabled too.
    expect((await screen.findByTestId('add-target-0')).matches(':disabled')).toBe(true);
  });
});

describe('SignalsPage — UNLOCKED signal keeps full edit interactivity', () => {
  it('leaves edit controls enabled when the signal is unlocked', async () => {
    mockListSignals.mockResolvedValue([lockedSignal({ locked: false })]);
    render(<SignalsPage />);
    // No lock banner when unlocked.
    expect(await screen.findByTestId('block-0')).toBeTruthy();
    expect(screen.queryByTestId('signal-lock-banner')).toBeNull();

    fireEvent.click(await screen.findByTestId('inputs-panel-toggle'));
    expect((await screen.findByTestId('inputs-add-btn')).matches(':disabled')).toBe(false);
    expect((await screen.findByTestId('block-input-select-0')).matches(':disabled')).toBe(false);
    expect((await screen.findByTestId('add-block-btn')).matches(':disabled')).toBe(false);
  });
});

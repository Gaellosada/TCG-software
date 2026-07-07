// @vitest-environment jsdom
//
// BLOCKER-1 regression: editing a Portfolio OPTION leg must NOT silently
// rewrite its `cycle` on modal-open.  Drives the REAL (UNMOCKED)
// InstrumentPickerModal + OptionStreamForm through AddHoldingModal (which pins
// optionHoldRequired=true) so the `heldInit` one-shot actually runs — the
// existing AddHoldingModal.editInPlace.test.jsx MOCKS the modal and therefore
// cannot catch this.  Only the API layer is mocked (mirrors
// InstrumentPickerModal.editmode.test.jsx).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import AddHoldingModal from './AddHoldingModal';

afterEach(cleanup);

vi.mock('../../api/data', () => ({
  listCollections: vi.fn(),
  listInstruments: vi.fn(),
  getAvailableCycles: vi.fn(),
}));
vi.mock('../../api/options', () => ({ getOptionRoots: vi.fn() }));
vi.mock('../../api/persistence', () => ({
  createBasket: vi.fn(),
  listBaskets: vi.fn(),
}));

import { listCollections, listInstruments, getAvailableCycles } from '../../api/data';
import { getOptionRoots } from '../../api/options';
import { listBaskets } from '../../api/persistence';

const MOCK_ROOTS = [
  { collection: 'OPT_SP_500', root_label: 'SP 500', name: 'SP 500', has_greeks: true },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listCollections).mockResolvedValue(['FUT_ES']);
  vi.mocked(listInstruments).mockResolvedValue({ items: [{ symbol: 'SPX' }] });
  vi.mocked(getAvailableCycles).mockResolvedValue(['M']);
  vi.mocked(getOptionRoots).mockResolvedValue({ roots: MOCK_ROOTS });
  vi.mocked(listBaskets).mockResolvedValue([]);
});

// A saved portfolio OPTION leg whose cycle must survive an edit unchanged.
function optionLeg(cycle) {
  return {
    id: 2,
    label: 'OPT_SP_500 P mid',
    weight: 70,
    type: 'option_stream',
    collection: 'OPT_SP_500',
    option_type: 'P',
    cycle,
    maturity: { kind: 'nearest_to_target', target_days: 30 },
    selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05 },
    stream: 'mid',
    roll_offset: { value: 2, unit: 'days' },
    hold_between_rolls: true,
    nav_times: 0.5,
    symbol: null,
    strategy: null,
    adjustment: null,
    rollOffset: 0,
  };
}

async function openEditAndConfirm(leg) {
  const onUpdateLeg = vi.fn();
  render(
    <AddHoldingModal
      isOpen
      onClose={vi.fn()}
      onAddLeg={vi.fn()}
      editLeg={leg}
      onUpdateLeg={onUpdateLeg}
    />,
  );
  // The real modal opens straight into the options drill-down (edit seed).
  await screen.findByTestId('option-stream-form');
  const confirm = screen.getByTestId('option-stream-confirm');
  expect(confirm.disabled).toBe(false);
  fireEvent.click(confirm);
  expect(onUpdateLeg).toHaveBeenCalledOnce();
  return onUpdateLeg.mock.calls[0][0];
}

describe('AddHoldingModal edit (real modal) — cycle is not coerced on open', () => {
  it('preserves cycle:null ("Any") — not rewritten to "M"', async () => {
    const config = await openEditAndConfirm(optionLeg(null));
    expect(config.cycle).toBeNull();
  });

  it("preserves cycle:'W3 Friday' — not rewritten to 'M'", async () => {
    const config = await openEditAndConfirm(optionLeg('W3 Friday'));
    expect(config.cycle).toBe('W3 Friday');
  });
});

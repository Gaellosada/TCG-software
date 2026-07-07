// @vitest-environment jsdom
//
// AddHoldingModal EDIT mode (click-chip-to-edit on the Portfolio surface).
//
// The picker is mocked so a captured `onSelect` simulates the user confirming
// the modal, and captured props let us assert the pre-fill (initialConfig) and
// readOnly are threaded. A small stateful Harness owns the leg list and applies
// `onUpdateLeg` EXACTLY as usePortfolio.updateLeg does
// (`prev.map((l,i) => i===idx ? {...l, ...updates} : l)`) so update-in-place /
// identity-preserved / no-duplicate / other-leg-untouched are all exercised at
// the AddHoldingModal boundary — not just asserted on a spy.

import { useState } from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';
import AddHoldingModal from './AddHoldingModal';
import { legToInitialConfig } from './legConfig';

let capturedOnSelect = null;
let capturedProps = null;
vi.mock('../../components/InstrumentPickerModal/InstrumentPickerModal', () => ({
  // eslint-disable-next-line react/prop-types
  default: (props) => {
    capturedOnSelect = props.onSelect;
    capturedProps = props;
    return <div data-testid="picker-stub" />;
  },
}));

beforeEach(() => {
  capturedOnSelect = null;
  capturedProps = null;
});
afterEach(cleanup);

const CONTINUOUS_LEG = {
  id: 1,
  label: 'My futures label',
  weight: 30,
  type: 'continuous',
  collection: 'FUT_ES',
  strategy: 'front_month',
  adjustment: 'ratio',
  cycle: 'H',
  rollOffset: 3,
  // full-shape null fields (as usePortfolio.addLeg stores them)
  symbol: null,
  option_type: null,
  maturity: null,
  selection: null,
  stream: null,
  roll_offset: null,
  hold_between_rolls: false,
  nav_times: 1.0,
};

const OPTION_LEG = {
  id: 2,
  label: 'OPT_SP_500 P mid',
  weight: 70,
  type: 'option_stream',
  collection: 'OPT_SP_500',
  option_type: 'P',
  cycle: null,
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

// Mirrors usePortfolio.updateLeg for the leg being edited (index `editIndex`).
function EditHarness({ initialLegs, editIndex, readOnly = false, onAddLeg = vi.fn() }) {
  const [legs, setLegs] = useState(initialLegs);
  const onUpdateLeg = (updates) =>
    setLegs((prev) => prev.map((l, i) => (i === editIndex ? { ...l, ...updates } : l)));
  return (
    <>
      <AddHoldingModal
        isOpen
        onClose={() => {}}
        onAddLeg={onAddLeg}
        editLeg={legs[editIndex]}
        onUpdateLeg={onUpdateLeg}
        readOnly={readOnly}
      />
      <div data-testid="legs-json">{JSON.stringify(legs)}</div>
    </>
  );
}

function readLegs() {
  return JSON.parse(screen.getByTestId('legs-json').textContent);
}

describe('AddHoldingModal edit mode — pre-fill & threading', () => {
  it('passes initialConfig derived from the leg via the inverse translation (continuous)', () => {
    render(<EditHarness initialLegs={[CONTINUOUS_LEG]} editIndex={0} />);
    expect(capturedProps).not.toBeNull();
    expect(capturedProps.initialConfig).toEqual(legToInitialConfig(CONTINUOUS_LEG));
    expect(capturedProps.initialConfig).toEqual({
      type: 'continuous',
      collection: 'FUT_ES',
      adjustment: 'ratio',
      cycle: 'H',
      rollOffset: 3,
      strategy: 'front_month',
    });
    // Edit-mode title, and the mid-pin + hold-required option props still apply.
    expect(capturedProps.title).toBe('Edit Holding');
    expect(capturedProps.optionStreamAllowedStreams).toEqual(['mid']);
    expect(capturedProps.optionHoldRequired).toBe(true);
  });

  it('passes initialConfig for an option leg (snake roll_offset + hold/nav preserved)', () => {
    render(<EditHarness initialLegs={[OPTION_LEG]} editIndex={0} />);
    expect(capturedProps.initialConfig).toEqual({
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'P',
      cycle: null,
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05 },
      stream: 'mid',
      roll_offset: { value: 2, unit: 'days' },
      hold_between_rolls: true,
      nav_times: 0.5,
    });
  });

  it('threads readOnly to the picker for a locked portfolio (view-only)', () => {
    render(<EditHarness initialLegs={[CONTINUOUS_LEG]} editIndex={0} readOnly />);
    expect(capturedProps.readOnly).toBe(true);
    // initialConfig still provided so the locked leg is VIEWABLE.
    expect(capturedProps.initialConfig).not.toBeNull();
  });
});

describe('AddHoldingModal edit mode — confirm updates the leg in place', () => {
  it('updates a future leg in place: id/label/weight preserved, config changed, no duplicate, other leg untouched', () => {
    const onAddLeg = vi.fn();
    render(<EditHarness initialLegs={[CONTINUOUS_LEG, OPTION_LEG]} editIndex={0} onAddLeg={onAddLeg} />);
    // Simulate the user confirming the modal with ONE field changed (cycle H -> M).
    act(() => {
      capturedOnSelect({
        type: 'continuous',
        collection: 'FUT_ES',
        strategy: 'front_month',
        adjustment: 'ratio',
        cycle: 'M',
        rollOffset: 3,
      });
    });
    const legs = readLegs();
    expect(legs).toHaveLength(2); // no duplicate appended
    // Edited leg: identity + user fields preserved, config updated.
    expect(legs[0]).toMatchObject({
      id: 1,
      label: 'My futures label',
      weight: 30,
      type: 'continuous',
      collection: 'FUT_ES',
      cycle: 'M',
      rollOffset: 3,
    });
    // Other leg untouched.
    expect(legs[1]).toEqual(OPTION_LEG);
    // Edit must NEVER append via onAddLeg.
    expect(onAddLeg).not.toHaveBeenCalled();
  });

  it('updates an option leg in place: hold_between_rolls / nav_times survive an edit', () => {
    render(<EditHarness initialLegs={[CONTINUOUS_LEG, OPTION_LEG]} editIndex={1} />);
    act(() => {
      capturedOnSelect({
        type: 'option_stream',
        collection: 'OPT_SP_500',
        option_type: 'P',
        cycle: null,
        maturity: { kind: 'nearest_to_target', target_days: 45 }, // changed 30 -> 45
        selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05 },
        stream: 'mid',
        roll_offset: { value: 2, unit: 'days' },
        hold_between_rolls: true,
        nav_times: 0.5,
      });
    });
    const legs = readLegs();
    expect(legs).toHaveLength(2);
    expect(legs[1]).toMatchObject({
      id: 2,
      label: 'OPT_SP_500 P mid',
      weight: 70,
      type: 'option_stream',
      maturity: { kind: 'nearest_to_target', target_days: 45 },
      hold_between_rolls: true,
      nav_times: 0.5,
    });
    expect(legs[0]).toEqual(CONTINUOUS_LEG); // untouched
  });

  it('cancel/close without confirming leaves the leg unchanged', () => {
    render(<EditHarness initialLegs={[CONTINUOUS_LEG]} editIndex={0} />);
    // No capturedOnSelect call = user closed the modal without confirming.
    expect(readLegs()).toEqual([CONTINUOUS_LEG]);
  });
});

describe('AddHoldingModal add mode — unchanged create flow', () => {
  it('with no editLeg, confirm appends via onAddLeg (edit path not taken)', () => {
    const onAddLeg = vi.fn();
    const onUpdateLeg = vi.fn();
    render(
      <AddHoldingModal
        isOpen
        onClose={() => {}}
        onAddLeg={onAddLeg}
        onUpdateLeg={onUpdateLeg}
      />,
    );
    expect(capturedProps.initialConfig).toBeNull(); // create mode
    expect(capturedProps.title).toBe('Add Holding');
    capturedOnSelect({
      type: 'continuous',
      collection: 'FUT_ES',
      strategy: 'front_month',
      adjustment: 'none',
      cycle: null,
      rollOffset: 2,
    });
    expect(onAddLeg).toHaveBeenCalledTimes(1);
    expect(onAddLeg.mock.calls[0][0]).toMatchObject({
      type: 'continuous',
      collection: 'FUT_ES',
      label: 'FUT_ES',
      weight: 100,
    });
    expect(onUpdateLeg).not.toHaveBeenCalled();
  });
});

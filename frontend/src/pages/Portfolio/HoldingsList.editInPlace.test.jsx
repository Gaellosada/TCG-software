// @vitest-environment jsdom
//
// Click-chip-to-edit affordance on the Portfolio HoldingsList instrument cell.
//
// future (continuous) and option (option_stream) legs get a clickable trigger
// that fires onEditLeg(index). spot (instrument) and signal legs do NOT.
//
// The trigger is a role="button" element, NOT a native <button>, ON PURPOSE:
// HoldingsList is wrapped by PortfolioPage in a native <fieldset disabled> when
// the portfolio is locked. A native <button> inside a disabled fieldset is
// disabled in real browsers, which would make a locked leg impossible to VIEW.
// A role="button" span is not a form control, so it stays clickable and opens
// the modal read-only (brief point 6 / test d). This is verified below by a
// tagName guard so a future refactor to <button> can't silently regress it.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import HoldingsList from './HoldingsList';

afterEach(cleanup);

const CONTINUOUS_LEG = {
  id: 'c1', label: 'ES', weight: 40, type: 'continuous',
  collection: 'FUT_ES', strategy: 'front_month', adjustment: 'none', cycle: 'H', rollOffset: 2,
};
const OPTION_LEG = {
  id: 'o1', label: 'SPX P', weight: 30, type: 'option_stream',
  collection: 'OPT_SP_500', option_type: 'P', cycle: null,
  maturity: { kind: 'nearest_to_target', target_days: 30 },
  selection: { kind: 'by_delta', target: -0.1 }, stream: 'mid',
  roll_offset: { value: 2, unit: 'days' }, hold_between_rolls: true, nav_times: 0.5,
};
const SPOT_LEG = {
  id: 's1', label: 'SPY', weight: 30, type: 'instrument', collection: 'equity_etf', symbol: 'SPY',
};

function renderList(props = {}) {
  const onEditLeg = vi.fn();
  render(
    <HoldingsList
      legs={[CONTINUOUS_LEG, OPTION_LEG, SPOT_LEG]}
      legDateRanges={{}}
      onUpdateLeg={vi.fn()}
      onRemoveLeg={vi.fn()}
      onOpenAddModal={vi.fn()}
      onOpenSignalModal={vi.fn()}
      onEditLeg={onEditLeg}
      {...props}
    />,
  );
  return { onEditLeg };
}

describe('<HoldingsList> instrument edit trigger', () => {
  it('fires onEditLeg with the leg index when a future leg instrument is clicked', () => {
    const { onEditLeg } = renderList();
    fireEvent.click(screen.getByTestId('edit-instrument-c1'));
    expect(onEditLeg).toHaveBeenCalledTimes(1);
    expect(onEditLeg).toHaveBeenCalledWith(0); // continuous is index 0
  });

  it('fires onEditLeg with the leg index when an option leg instrument is clicked', () => {
    const { onEditLeg } = renderList();
    fireEvent.click(screen.getByTestId('edit-instrument-o1'));
    expect(onEditLeg).toHaveBeenCalledTimes(1);
    expect(onEditLeg).toHaveBeenCalledWith(1); // option is index 1
  });

  it('activates via keyboard (Enter/Space) for accessibility', () => {
    const { onEditLeg } = renderList();
    const trigger = screen.getByTestId('edit-instrument-c1');
    fireEvent.keyDown(trigger, { key: 'Enter' });
    fireEvent.keyDown(trigger, { key: ' ' });
    expect(onEditLeg).toHaveBeenCalledTimes(2);
  });

  it('does NOT render an edit trigger for a spot/instrument leg', () => {
    renderList();
    expect(screen.queryByTestId('edit-instrument-s1')).toBeNull();
    // The spot symbol is still shown (plain, non-interactive).
    expect(screen.getByText('SPY')).toBeDefined();
  });

  it('uses a role="button" element that is NOT a native <button> (escapes the disabled fieldset)', () => {
    renderList();
    const trigger = screen.getByTestId('edit-instrument-c1');
    expect(trigger.getAttribute('role')).toBe('button');
    expect(trigger.tagName).not.toBe('BUTTON');
    expect(trigger.getAttribute('tabindex')).toBe('0');
  });

  it('title reads "Edit settings" when unlocked and "View settings" when locked', () => {
    const { onEditLeg } = renderList({ readOnly: true });
    const trigger = screen.getByTestId('edit-instrument-c1');
    expect(trigger.getAttribute('title')).toBe('View settings');
    // Still clickable when locked so the leg can be viewed read-only.
    fireEvent.click(trigger);
    expect(onEditLeg).toHaveBeenCalledWith(0);

    cleanup();
    renderList({ readOnly: false });
    expect(screen.getByTestId('edit-instrument-c1').getAttribute('title')).toBe('Edit settings');
  });
});

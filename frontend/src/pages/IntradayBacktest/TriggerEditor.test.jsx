// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

afterEach(cleanup);

import TriggerEditor, {
  TRIGGER_TYPES,
  defaultTrigger,
  serializeTrigger,
  triggerLabel,
} from './TriggerEditor';

// ---------------------------------------------------------------------------
// TriggerEditor unit tests — PINNED v3 early-exit trigger contract (DESIGN.md).
//  - underlying_move { amount, unit: points|percent }
//  - sigma_move      { n }
//  - net_delta       { threshold }
//  - pnl             { amount, unit: points|percent|usd, direction: profit|loss|both }
// ---------------------------------------------------------------------------

describe('TriggerEditor catalogue + serializers', () => {
  it('exposes exactly the 4 pinned trigger types', () => {
    expect(TRIGGER_TYPES.map((t) => t.type)).toEqual([
      'underlying_move', 'sigma_move', 'net_delta', 'pnl',
    ]);
  });

  it('defaultTrigger seeds the pinned param shapes', () => {
    expect(serializeTrigger(defaultTrigger('underlying_move')))
      .toEqual({ type: 'underlying_move', amount: 15, unit: 'points' });
    expect(serializeTrigger(defaultTrigger('sigma_move')))
      .toEqual({ type: 'sigma_move', n: 1.0 });
    expect(serializeTrigger(defaultTrigger('net_delta')))
      .toEqual({ type: 'net_delta', threshold: 0.30 });
    expect(serializeTrigger(defaultTrigger('pnl')))
      .toEqual({ type: 'pnl', amount: 500, unit: 'usd', direction: 'both' });
  });

  it('serializeTrigger coerces string inputs to numbers and drops the _id', () => {
    const s = serializeTrigger({ _id: 'x1', type: 'sigma_move', n: '2.5' });
    expect(s).toEqual({ type: 'sigma_move', n: 2.5 });
    expect(s._id).toBeUndefined();
  });

  it('triggerLabel maps every type to a human label', () => {
    TRIGGER_TYPES.forEach((t) => {
      expect(typeof triggerLabel(t.type)).toBe('string');
      expect(triggerLabel(t.type).length).toBeGreaterThan(0);
    });
  });
});

describe('TriggerEditor row rendering', () => {
  it('renders underlying_move params: amount + unit', () => {
    render(
      <TriggerEditor
        idPrefix="exit"
        trigger={defaultTrigger('underlying_move')}
        onChange={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByLabelText('exit underlying_move amount')).toBeTruthy();
    expect(screen.getByLabelText('exit underlying_move unit')).toBeTruthy();
  });

  it('renders pnl params: amount + unit + direction', () => {
    render(
      <TriggerEditor
        idPrefix="exit"
        trigger={defaultTrigger('pnl')}
        onChange={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByLabelText('exit pnl amount')).toBeTruthy();
    expect(screen.getByLabelText('exit pnl unit')).toBeTruthy();
    expect(screen.getByLabelText('exit pnl direction')).toBeTruthy();
  });

  it('fires onRemove when × clicked', () => {
    const onRemove = vi.fn();
    render(
      <TriggerEditor
        idPrefix="exit"
        trigger={defaultTrigger('net_delta')}
        onChange={() => {}}
        onRemove={onRemove}
      />,
    );
    fireEvent.click(screen.getByLabelText(/remove .* trigger/i));
    expect(onRemove).toHaveBeenCalled();
  });
});

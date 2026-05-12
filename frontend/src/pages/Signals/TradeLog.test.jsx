// @vitest-environment jsdom

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

import TradeLog from './TradeLog';

afterEach(cleanup);

// timestamps[i] = 2024-01-(i+1) at 00:00 UTC, in ms.
const TS = [
  Date.UTC(2024, 0, 1),
  Date.UTC(2024, 0, 2),
  Date.UTC(2024, 0, 3),
  Date.UTC(2024, 0, 4),
  Date.UTC(2024, 0, 5),
];

function pos(inputId, priceValues) {
  return {
    input_id: inputId,
    instrument: { type: 'spot', collection: 'X', instrument_id: 'X' },
    values: priceValues.map(() => 0),
    clipped_mask: priceValues.map(() => false),
    price: { label: 'close', values: priceValues },
  };
}

describe('<TradeLog>', () => {
  it('renders the header collapsed by default with count 0 when no trades', () => {
    render(<TradeLog trades={[]} timestamps={TS} positions={[]} />);
    expect(screen.getByTestId('trade-log')).toBeTruthy();
    const toggle = screen.getByTestId('trade-log-toggle');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.getByTestId('trade-log-count').textContent).toBe('(0)');
    // No body yet — still collapsed.
    expect(screen.queryByTestId('trade-log-empty')).toBeNull();
  });

  it('expands to "No trades" when there are zero trades', () => {
    render(<TradeLog trades={[]} timestamps={TS} positions={[]} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    expect(screen.getByTestId('trade-log-empty').textContent).toBe('No trades');
  });

  it('renders 2 closed trades + 1 open trade with correct columns', () => {
    const trades = [
      {
        input_id: 'X',
        entry_block_id: 'e1',
        entry_block_name: 'EntryA',
        exit_block_id: 'x1',
        exit_block_name: 'ExitA',
        open_bar: 0,
        close_bar: 2,
        direction: 'long',
        signed_weight: 0.6,
      },
      {
        input_id: 'X',
        entry_block_id: 'e2',
        entry_block_name: 'EntryB',
        exit_block_id: 'x2',
        exit_block_name: 'ExitB',
        open_bar: 1,
        close_bar: 3,
        direction: 'short',
        signed_weight: -0.4,
      },
      {
        input_id: 'X',
        entry_block_id: 'e3',
        entry_block_name: 'EntryC',
        exit_block_id: null,
        exit_block_name: null,
        open_bar: 4,
        close_bar: null,
        direction: 'long',
        signed_weight: 0.5,
      },
    ];
    const positions = [pos('X', [100, 102, 110, 108, 105])];
    render(<TradeLog
      trades={trades}
      timestamps={TS}
      positions={positions}
      exitDescriptions={{ x1: 'A exit reason', x2: 'B exit reason' }}
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));

    const rows = screen.getAllByTestId('trade-row');
    expect(rows).toHaveLength(3);

    // Row 1: long, open=100, close=110, realised = (110/100 - 1)*0.6 = 0.06 → "+6.00%"
    expect(rows[0].textContent).toContain('long');
    expect(rows[0].textContent).toContain('100');
    expect(rows[0].textContent).toContain('110');
    expect(rows[0].textContent).toContain('+6.00%');

    // Row 2: short, open=102, close=108, realised = (108/102 - 1)*(-0.4) ≈ -0.0235 → "-2.35%"
    expect(rows[1].textContent).toContain('short');
    expect(rows[1].textContent).toContain('-40%');

    // Row 3: open trade — no close price, no realised P&L
    expect(rows[2].textContent).toContain('open');
    // Realised cell in an open trade is "—"
    const cells = rows[2].querySelectorAll('td');
    expect(cells[7].textContent).toBe('—');
  });

  it('reason cell exposes the exit block description as a tooltip', () => {
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'EntryA',
      exit_block_id: 'x1',
      exit_block_name: 'ExitA',
      open_bar: 0,
      close_bar: 2,
      direction: 'long',
      signed_weight: 0.6,
    }];
    const positions = [pos('X', [100, 105, 110])];
    render(<TradeLog
      trades={trades}
      timestamps={TS}
      positions={positions}
      exitDescriptions={{ x1: 'because RSI > 70' }}
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    const reason = screen.getByTestId('trade-reason');
    expect(reason.getAttribute('title')).toBe('because RSI > 70');
    expect(reason.getAttribute('data-reason-tooltip')).toBe('because RSI > 70');
    expect(reason.textContent).toBe('ExitA');
  });

  it('chronological ordering: rows sorted by open_bar ascending', () => {
    const trades = [
      { input_id: 'X', entry_block_id: 'b', open_bar: 3, close_bar: null, direction: 'long', signed_weight: 0.5 },
      { input_id: 'X', entry_block_id: 'a', open_bar: 1, close_bar: null, direction: 'long', signed_weight: 0.5 },
      { input_id: 'X', entry_block_id: 'c', open_bar: 2, close_bar: null, direction: 'long', signed_weight: 0.5 },
    ];
    const positions = [pos('X', [100, 101, 102, 103])];
    render(<TradeLog trades={trades} timestamps={TS} positions={positions} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    const rows = screen.getAllByTestId('trade-row');
    expect(rows).toHaveLength(3);
    // Ascending by open_bar → a, c, b
    expect(rows[0].getAttribute('data-testid')).toBe('trade-row');
    // Check ordering via entry_block_id embedded in the key — easier: each row's
    // first column is the timestamp at the open_bar. Verify the column order.
    expect(rows[0].querySelectorAll('td')[0].textContent).toContain('2024-01-02');
    expect(rows[1].querySelectorAll('td')[0].textContent).toContain('2024-01-03');
    expect(rows[2].querySelectorAll('td')[0].textContent).toContain('2024-01-04');
  });
});

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
    expect(rows[1].textContent).toContain('-40.00%');

    // Row 3: open trade — no close price, no realised P&L
    expect(rows[2].textContent).toContain('open');
    // P&L cell in an open trade is "—"
    const cells = rows[2].querySelectorAll('td');
    expect(cells[7].textContent).toBe('—');
  });

  it('entry reason and exit reason are separate columns on a closed trade', () => {
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'MyEntry',
      exit_block_id: 'x1',
      exit_block_name: 'MyExit',
      open_bar: 0,
      close_bar: 2,
      direction: 'long',
      signed_weight: 0.5,
    }];
    const positions = [pos('X', [100, 105, 110])];
    render(<TradeLog
      trades={trades}
      timestamps={TS}
      positions={positions}
      entryDescriptions={{ e1: 'entry desc' }}
      exitDescriptions={{ x1: 'exit desc' }}
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));

    const entryReason = screen.getByTestId('trade-entry-reason');
    const exitReason = screen.getByTestId('trade-exit-reason');

    expect(entryReason.textContent).toBe('MyEntry');
    expect(entryReason.getAttribute('title')).toBe('entry desc');
    expect(entryReason.getAttribute('data-reason-tooltip')).toBe('entry desc');

    expect(exitReason.textContent).toBe('MyExit');
    expect(exitReason.getAttribute('title')).toBe('exit desc');
    expect(exitReason.getAttribute('data-reason-tooltip')).toBe('exit desc');
  });

  it('exit reason shows "open" with no tooltip for an open trade', () => {
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'MyEntry',
      exit_block_id: null,
      exit_block_name: null,
      open_bar: 0,
      close_bar: null,
      direction: 'long',
      signed_weight: 0.5,
    }];
    const positions = [pos('X', [100, 105])];
    render(<TradeLog
      trades={trades}
      timestamps={TS}
      positions={positions}
      entryDescriptions={{ e1: 'entry desc' }}
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));

    const exitReason = screen.getByTestId('trade-exit-reason');
    expect(exitReason.textContent).toBe('open');
    expect(exitReason.getAttribute('title')).toBeFalsy();
    expect(exitReason.getAttribute('data-reason-tooltip')).toBe('');
  });

  it('entryDescriptions tooltip flows from the prop', () => {
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
      entryDescriptions={{ e1: 'RSI crossed 30' }}
      exitDescriptions={{ x1: 'RSI crossed 70' }}
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));

    const entryReason = screen.getByTestId('trade-entry-reason');
    expect(entryReason.getAttribute('title')).toBe('RSI crossed 30');
    expect(entryReason.textContent).toBe('EntryA');
  });

  it('shows "(unnamed)" only when block name is truly empty', () => {
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: '',
      exit_block_id: 'x1',
      exit_block_name: '',
      open_bar: 0,
      close_bar: 2,
      direction: 'long',
      signed_weight: 0.5,
    }];
    const positions = [pos('X', [100, 105, 110])];
    render(<TradeLog
      trades={trades}
      timestamps={TS}
      positions={positions}
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));

    expect(screen.getByTestId('trade-entry-reason').textContent).toBe('(unnamed)');
    expect(screen.getByTestId('trade-exit-reason').textContent).toBe('(unnamed)');
  });

  it('P&L toggle: defaults to Realised, switching to Log changes header and value', () => {
    // open=100, close=110, signed_weight=1.0
    // Realised: (110/100 - 1)*1 = 0.10 → +10.00%
    // Log:      ln(110/100)*1  ≈ 0.09531 → +9.53%
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'E',
      exit_block_id: 'x1',
      exit_block_name: 'X',
      open_bar: 0,
      close_bar: 1,
      direction: 'long',
      signed_weight: 1.0,
    }];
    const positions = [pos('X', [100, 110])];
    render(<TradeLog
      trades={trades}
      timestamps={TS}
      positions={positions}
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));

    // Default: Realised P&L header and value
    expect(screen.getByTestId('pnl-col-header').textContent).toBe('Realised P&L');
    const rows = screen.getAllByTestId('trade-row');
    expect(rows[0].textContent).toContain('+10.00%');

    // Switch to Log
    fireEvent.click(screen.getByRole('button', { name: 'Log' }));
    expect(screen.getByTestId('pnl-col-header').textContent).toBe('Log P&L');
    // ln(110/100) ≈ 0.09531, *100 = 9.531 → "+9.53%"
    expect(rows[0].textContent).toContain('+9.53%');

    // Switch back to Realised
    fireEvent.click(screen.getByRole('button', { name: 'Realised' }));
    expect(screen.getByTestId('pnl-col-header').textContent).toBe('Realised P&L');
    expect(rows[0].textContent).toContain('+10.00%');
  });

  it('clicking the P&L toggle does NOT collapse the panel', () => {
    render(<TradeLog trades={[]} timestamps={TS} positions={[]} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    expect(screen.getByTestId('trade-log-empty')).toBeTruthy();

    // Click one of the toggle pills — panel must stay open.
    fireEvent.click(screen.getByRole('button', { name: 'Log' }));
    expect(screen.getByTestId('trade-log-empty')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Realised' }));
    expect(screen.getByTestId('trade-log-empty')).toBeTruthy();
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
    const exitReason = screen.getByTestId('trade-exit-reason');
    expect(exitReason.getAttribute('title')).toBe('because RSI > 70');
    expect(exitReason.getAttribute('data-reason-tooltip')).toBe('because RSI > 70');
    expect(exitReason.textContent).toBe('ExitA');
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
    // Ascending by open_bar → a (1), c (2), b (3). Assert non-decreasing
    // open_bar order across all rows (the previous version was vacuous).
    const openBars = rows.map((r) => Number(r.getAttribute('data-open-bar')));
    for (let i = 1; i < openBars.length; i++) {
      expect(openBars[i]).toBeGreaterThanOrEqual(openBars[i - 1]);
    }
    expect(openBars).toEqual([1, 2, 3]);
    // Each row's first column is the timestamp at open_bar — sanity-check it
    // matches the expected dates.
    expect(rows[0].querySelectorAll('td')[0].textContent).toContain('2024-01-02');
    expect(rows[1].querySelectorAll('td')[0].textContent).toContain('2024-01-03');
    expect(rows[2].querySelectorAll('td')[0].textContent).toContain('2024-01-04');
  });

  it('holding column hidden by default (showHoldingColumn omitted/false)', () => {
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'E',
      exit_block_id: 'x1',
      exit_block_name: 'X',
      open_bar: 0,
      close_bar: 1,
      direction: 'long',
      signed_weight: 0.5,
      holding_id: 'LegA',
      holding_name: 'LegA',
    }];
    const positions = [pos('X', [100, 105])];
    render(<TradeLog trades={trades} timestamps={TS} positions={positions} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    expect(screen.queryByTestId('holding-col-header')).toBeNull();
    expect(screen.queryByTestId('trade-holding')).toBeNull();
  });

  it('holding column visible when showHoldingColumn=true and renders holding_name', () => {
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'E',
      exit_block_id: 'x1',
      exit_block_name: 'X',
      open_bar: 0,
      close_bar: 1,
      direction: 'long',
      signed_weight: 0.5,
      holding_id: 'LegA',
      holding_name: 'LegA',
    }];
    const positions = [pos('X', [100, 105])];
    render(<TradeLog
      trades={trades}
      timestamps={TS}
      positions={positions}
      showHoldingColumn
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    expect(screen.getByTestId('holding-col-header').textContent).toBe('Holding');
    const cell = screen.getByTestId('trade-holding');
    expect(cell.textContent).toBe('LegA');
  });

  it('holding column falls back to holding_id, then em-dash when both missing', () => {
    const trades = [
      {
        input_id: 'X', entry_block_id: 'e1', entry_block_name: 'E',
        exit_block_id: 'x1', exit_block_name: 'X',
        open_bar: 0, close_bar: 1,
        direction: 'long', signed_weight: 0.5,
        holding_id: 'IdOnly', holding_name: null,
      },
      {
        input_id: 'X', entry_block_id: 'e2', entry_block_name: 'E',
        exit_block_id: 'x2', exit_block_name: 'X',
        open_bar: 1, close_bar: null,
        direction: 'long', signed_weight: 0.5,
        // neither field present
      },
    ];
    const positions = [pos('X', [100, 105, 110])];
    render(<TradeLog
      trades={trades}
      timestamps={TS}
      positions={positions}
      showHoldingColumn
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    const cells = screen.getAllByTestId('trade-holding');
    expect(cells).toHaveLength(2);
    expect(cells[0].textContent).toBe('IdOnly');
    expect(cells[1].textContent).toBe('—');
  });
});

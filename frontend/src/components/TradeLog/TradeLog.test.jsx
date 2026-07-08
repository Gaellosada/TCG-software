// @vitest-environment jsdom

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

import TradeLog, { formatSignedPercent, formatQuantity, formatSignedAmount, formatPrice } from './TradeLog';

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

    // Row 3: open trade — close price is em-dash, but P&L uses last price (105 at bar 4)
    // open_bar=4, open_price=105, last_price=105 → PnL=(105/105-1)*0.5=0 → "0.00%"
    expect(rows[2].textContent).toContain('open');
    const cells = rows[2].querySelectorAll('td');
    // Close price column (cells[6]) stays as em-dash for open trades
    expect(cells[6].textContent).toBe('—');
    // P&L column (cells[7]) is computed using last price, not em-dash
    expect(cells[7].textContent).toBe('0.00%');
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

  it('P&L header is a static "Realised P&L" (no mode toggle)', () => {
    // open=100, close=110, signed_weight=1.0 → realised (110/100 - 1)*1 = +10.00%.
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
    render(<TradeLog trades={trades} timestamps={TS} positions={positions} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));

    expect(screen.getByTestId('pnl-col-header').textContent).toBe('Realised P&L');
    const rows = screen.getAllByTestId('trade-row');
    expect(rows[0].textContent).toContain('+10.00%');

    // The Realised/Log mode toggle is GONE (removed with the log P&L mode).
    expect(screen.queryByTestId('pnl-mode-toggle')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Log' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Realised' })).toBeNull();
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

describe('open-trade PnL using last available price', () => {
  it('open trade with valid price series — PnL uses last price; close-price cell shows em-dash', () => {
    // open_bar=0 → open_price=100; last price=120; PnL=(120/100-1)*0.5=0.1 → "+10.00%"
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'EntryA',
      exit_block_id: null,
      exit_block_name: null,
      open_bar: 0,
      close_bar: null,
      direction: 'long',
      signed_weight: 0.5,
    }];
    const positions = [pos('X', [100, 110, 120])];
    render(<TradeLog trades={trades} timestamps={TS} positions={positions} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    const rows = screen.getAllByTestId('trade-row');
    expect(rows).toHaveLength(1);
    const cells = rows[0].querySelectorAll('td');
    // Close price column (index 6 without showHoldingColumn): should be em-dash
    expect(cells[6].textContent).toBe('—');
    // PnL column (index 7): (120/100 - 1) * 0.5 = 0.10 → "+10.00%"
    expect(cells[7].textContent).toBe('+10.00%');
  });

  it('open trade with no positions entry for input_id — PnL is em-dash', () => {
    const trades = [{
      input_id: 'UNKNOWN',
      entry_block_id: 'e1',
      entry_block_name: 'EntryA',
      exit_block_id: null,
      exit_block_name: null,
      open_bar: 0,
      close_bar: null,
      direction: 'long',
      signed_weight: 0.5,
    }];
    // Positions contain 'X', not 'UNKNOWN'
    const positions = [pos('X', [100, 110, 120])];
    render(<TradeLog trades={trades} timestamps={TS} positions={positions} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    const rows = screen.getAllByTestId('trade-row');
    const cells = rows[0].querySelectorAll('td');
    // Both close price and PnL should be em-dash (no price data)
    expect(cells[6].textContent).toBe('—');
    expect(cells[7].textContent).toBe('—');
  });

  it('open trade where last price is null but a prior price is finite — PnL uses last finite price', () => {
    // Price series ends with nulls; last finite value is at index 2 (value=115)
    // open_bar=0 → open_price=100; last finite price=115; PnL=(115/100-1)*1.0=0.15 → "+15.00%"
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'EntryA',
      exit_block_id: null,
      exit_block_name: null,
      open_bar: 0,
      close_bar: null,
      direction: 'long',
      signed_weight: 1.0,
    }];
    const positions = [pos('X', [100, 110, 115, null, null])];
    render(<TradeLog trades={trades} timestamps={TS} positions={positions} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    const rows = screen.getAllByTestId('trade-row');
    const cells = rows[0].querySelectorAll('td');
    // Close price column: em-dash (open trade)
    expect(cells[6].textContent).toBe('—');
    // PnL uses last finite price = 115: (115/100 - 1) * 1.0 = 0.15 → "+15.00%"
    expect(cells[7].textContent).toBe('+15.00%');
  });

  it('closed trade behavior unchanged (regression) — PnL uses actual close bar price', () => {
    // open_bar=0 → price=100; close_bar=2 → price=110; signed_weight=1.0
    // PnL = (110/100 - 1) * 1.0 = 0.10 → "+10.00%"
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'EntryA',
      exit_block_id: 'x1',
      exit_block_name: 'ExitA',
      open_bar: 0,
      close_bar: 2,
      direction: 'long',
      signed_weight: 1.0,
    }];
    // Price series has more values beyond close_bar to ensure we don't accidentally use last price
    const positions = [pos('X', [100, 108, 110, 130, 150])];
    render(<TradeLog trades={trades} timestamps={TS} positions={positions} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    const rows = screen.getAllByTestId('trade-row');
    const cells = rows[0].querySelectorAll('td');
    // Close price column: shows actual close price (110), not em-dash
    expect(cells[6].textContent).toContain('110');
    // PnL uses close_bar price (110), not last price (150): "+10.00%"
    expect(cells[7].textContent).toBe('+10.00%');
  });
});

describe('Size column — quantity counts vs % fallback (shared component)', () => {
  const baseTrade = {
    input_id: 'X',
    entry_block_id: 'e1',
    entry_block_name: 'E',
    exit_block_id: 'x1',
    exit_block_name: 'X',
    open_bar: 0,
    close_bar: 1,
    direction: 'long',
    signed_weight: 0.6,
  };
  const positions = [pos('X', [100, 110])];

  function renderTrade(overrides) {
    render(<TradeLog trades={[{ ...baseTrade, ...overrides }]} timestamps={TS} positions={positions} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    return screen.getByTestId('trade-size');
  }

  it('SC1: finite quantity + unit → renders "<count> <unit>", not a %', () => {
    const cell = renderTrade({ quantity: 12.34, quantity_unit: 'contracts', multiplier: 50 });
    expect(cell.textContent).toBe('12.34 contracts');
    expect(cell.textContent).not.toContain('%');
  });

  it('SC1: large share count is grouped, not scientific', () => {
    const cell = renderTrade({ quantity: 1432, quantity_unit: 'shares', multiplier: 1 });
    expect(cell.textContent).toBe('1,432 shares');
  });

  it('SC3: quantity null (key present) → em-dash, NOT a % fallback', () => {
    const cell = renderTrade({ quantity: null, quantity_unit: 'contracts', multiplier: null });
    expect(cell.textContent).toBe('—');
    expect(cell.textContent).not.toContain('%');
  });

  it('SC4: no quantity KEY (Signals-style payload) → still renders the % fallback', () => {
    // baseTrade has no `quantity` key → shared Signals usage unchanged.
    const cell = renderTrade({});
    expect(cell.textContent).toBe('+60.00%');
  });

  it('tiny fractional futures count renders fractionally without NaN/scientific garbage', () => {
    const cell = renderTrade({ quantity: 0.0004, quantity_unit: 'contracts', multiplier: 50 });
    expect(cell.textContent).toBe('0.0004 contracts');
    expect(cell.textContent).not.toMatch(/e|NaN|Infinity/i);
  });

  it('magnitude only: negative quantity renders unsigned (sign is in Direction column)', () => {
    const cell = renderTrade({ quantity: -7.5, quantity_unit: 'contracts', direction: 'short', signed_weight: -0.6 });
    expect(cell.textContent).toBe('7.5 contracts');
  });
});

describe('formatQuantity', () => {
  it('formats a small fractional count with its unit', () => {
    expect(formatQuantity(0.0004, 'contracts')).toBe('0.0004 contracts');
  });
  it('groups large counts and never uses scientific notation', () => {
    expect(formatQuantity(1432, 'shares')).toBe('1,432 shares');
  });
  it('preserves full integer precision for large counts (no sig-fig rounding)', () => {
    // Regression: maximumSignificantDigits:4 would render 14325 as "14,320".
    expect(formatQuantity(14325, 'shares')).toBe('14,325 shares');
  });
  it('rounds |qty|>=1 to 2 decimals', () => {
    expect(formatQuantity(12.34567, 'contracts')).toBe('12.35 contracts');
  });
  it('keeps sub-1 fractions meaningful (4 significant digits)', () => {
    expect(formatQuantity(0.0004123, 'contracts')).toBe('0.0004123 contracts');
  });
  it('non-finite → em-dash (guards NaN/Infinity/null)', () => {
    expect(formatQuantity(NaN, 'contracts')).toBe('—');
    expect(formatQuantity(Infinity, 'shares')).toBe('—');
    expect(formatQuantity(null, 'shares')).toBe('—');
    expect(formatQuantity(undefined, 'shares')).toBe('—');
  });
  it('missing/blank unit → number only', () => {
    expect(formatQuantity(5)).toBe('5');
    expect(formatQuantity(5, '  ')).toBe('5');
  });
});

describe('formatSignedPercent', () => {
  it('formats +0.1 as "+10.00%" without FP truncation artefacts', () => {
    // Regression: (0.1 * 100) === 10.000000000000009 under Math.trunc — toFixed avoids it.
    expect(formatSignedPercent(0.1)).toBe('+10.00%');
  });

  it('formats -0.1 as "-10.00%"', () => {
    expect(formatSignedPercent(-0.1)).toBe('-10.00%');
  });

  it('formats 0 as "0.00%"', () => {
    expect(formatSignedPercent(0)).toBe('0.00%');
  });
});

describe('roll rows (rolling direct legs)', () => {
  // A continuous leg with one interior roll → two segment rows: open→rolling,
  // rolling→end. Each carries a contract quantity + a backend dollar segment_pnl
  // + a per-leg roll_hover surfaced through the descriptions channel.
  const rollTrades = [
    {
      input_id: 'FUT_SP_500',
      entry_block_id: 'roll:SPX',
      entry_block_name: 'open',
      exit_block_id: 'roll:SPX',
      exit_block_name: 'rolling',
      open_bar: 0,
      close_bar: 1,
      direction: 'long',
      signed_weight: 1.0,
      quantity: 0.02,
      quantity_unit: 'contracts',
      multiplier: 50,
      segment_pnl: 1.0,
      roll_hover: 'rolling FUT_SP_500',
    },
    {
      input_id: 'FUT_SP_500',
      entry_block_id: 'roll:SPX',
      entry_block_name: 'rolling',
      exit_block_id: 'roll:SPX',
      exit_block_name: 'end',
      open_bar: 2,
      close_bar: 4,
      direction: 'long',
      signed_weight: 1.0,
      quantity: 0.02,
      quantity_unit: 'contracts',
      multiplier: 50,
      segment_pnl: -2.0,
      roll_hover: 'rolling FUT_SP_500',
    },
  ];

  it('renders open/rolling/end reasons with the per-leg hover text', () => {
    render(<TradeLog
      trades={rollTrades}
      timestamps={TS}
      positions={[pos('FUT_SP_500', [100, 101, 102, 103, 104])]}
      entryDescriptions={{ 'roll:SPX': 'rolling FUT_SP_500' }}
      exitDescriptions={{ 'roll:SPX': 'rolling FUT_SP_500' }}
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));

    const entryReasons = screen.getAllByTestId('trade-entry-reason');
    const exitReasons = screen.getAllByTestId('trade-exit-reason');
    expect(entryReasons.map((e) => e.textContent)).toEqual(['open', 'rolling']);
    expect(exitReasons.map((e) => e.textContent)).toEqual(['rolling', 'end']);
    // Hover text = "rolling <input name>" on both cells.
    expect(entryReasons[0].getAttribute('title')).toBe('rolling FUT_SP_500');
    expect(exitReasons[1].getAttribute('title')).toBe('rolling FUT_SP_500');
  });

  it('Size cell shows contract counts and P&L cell shows the dollar segment_pnl', () => {
    render(<TradeLog
      trades={rollTrades}
      timestamps={TS}
      positions={[pos('FUT_SP_500', [100, 101, 102, 103, 104])]}
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));

    const sizes = screen.getAllByTestId('trade-size');
    expect(sizes[0].textContent).toBe('0.02 contracts');

    const pnls = screen.getAllByTestId('trade-pnl');
    // segment_pnl is a signed DOLLAR amount, not a percentage.
    expect(pnls[0].textContent).toBe('+1.00');
    expect(pnls[1].textContent).toBe('-2.00');
  });

  it('option roll row shows the contract PREMIUM as open/close price, not the base-100 synthetic', () => {
    // Reported bug: the open price showed 100. An option leg's position series is
    // the base-100 synthetic equity, so the row carries explicit open_price/
    // close_price (the premium); the FE must prefer them over the position series.
    const optionRoll = [{
      input_id: 'P',
      entry_block_id: 'roll:P',
      entry_block_name: 'open',
      exit_block_id: 'roll:P',
      exit_block_name: 'end',
      open_bar: 0,
      close_bar: 2,
      direction: 'short',
      signed_weight: -1.0,
      quantity: 9.76,
      quantity_unit: 'contracts',
      multiplier: 50,
      segment_pnl: 100.0,
      roll_hover: 'rolling OPT_SP_500',
      open_price: 10.25,   // the roll-day entry premium
      close_price: 3.5,    // last observed premium
    }];
    render(<TradeLog
      trades={optionRoll}
      timestamps={TS}
      // position series is the SYNTHETIC equity (starts at 100) — must NOT be shown as price
      positions={[pos('P', [100, 150, 200])]}
    />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));

    const openPrices = screen.getAllByTestId('trade-open-price');
    const closePrices = screen.getAllByTestId('trade-close-price');
    expect(openPrices[0].textContent).toBe(formatPrice(10.25));
    expect(openPrices[0].textContent).not.toBe(formatPrice(100));
    expect(closePrices[0].textContent).toBe(formatPrice(3.5));
  });

  it('non-roll trades keep the percentage P&L (segment_pnl absent)', () => {
    const trades = [{
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'EntryA',
      exit_block_id: 'x1',
      exit_block_name: 'ExitA',
      open_bar: 0,
      close_bar: 2,
      direction: 'long',
      signed_weight: 1.0,
    }];
    render(<TradeLog trades={trades} timestamps={TS} positions={[pos('X', [100, 105, 110])]} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    // (110/100 - 1) * 1.0 = +10.00%.
    expect(screen.getByTestId('trade-pnl').textContent).toBe('+10.00%');
  });

  it('roll row with null segment_pnl renders em-dash (never the synthetic %)', () => {
    // A roll row whose backend segment_pnl is null must NOT fall back to
    // computePnl on the leg synthetic (which would double-invert the short) —
    // it renders em-dash. The position price series is the leg SYNTHETIC equity
    // that rises (a profitable short), which the old fallback would have shown
    // as a large negative percentage.
    const trades = [{
      input_id: 'P',
      entry_block_id: 'roll:P',
      entry_block_name: 'open',
      exit_block_id: 'roll:P',
      exit_block_name: 'end',
      open_bar: 0,
      close_bar: 4,
      direction: 'short',
      signed_weight: -1.0,
      quantity: 3.33,
      quantity_unit: 'contracts',
      multiplier: 50,
      segment_pnl: null,
      roll_hover: 'rolling OPT_SP_500',
    }];
    // Synthetic RISES 100→200 (profitable short); the buggy fallback
    // (200/100−1)·(−1) = −100% must NOT appear.
    render(<TradeLog trades={trades} timestamps={TS} positions={[pos('P', [100, 130, 160, 180, 200])]} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    const cell = screen.getByTestId('trade-pnl');
    expect(cell.textContent).toBe('—');
    expect(cell.textContent).not.toContain('%');
    expect(cell.textContent).not.toContain('-100');
  });

  it('profitable short roll row shows a POSITIVE dollar segment_pnl (not inverted)', () => {
    // The backend now supplies a correctly-signed dollar segment_pnl for a
    // profitable short; it is shown verbatim and positive.
    const trades = [{
      input_id: 'P',
      entry_block_id: 'roll:P',
      entry_block_name: 'open',
      exit_block_id: 'roll:P',
      exit_block_name: 'end',
      open_bar: 0,
      close_bar: 4,
      direction: 'short',
      signed_weight: -1.0,
      quantity: 3.33,
      quantity_unit: 'contracts',
      multiplier: 50,
      segment_pnl: 13.33,
      roll_hover: 'rolling OPT_SP_500',
    }];
    render(<TradeLog trades={trades} timestamps={TS} positions={[pos('P', [100, 130, 160, 180, 200])]} />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    const cell = screen.getByTestId('trade-pnl');
    expect(cell.textContent).toBe('+13.33');
    expect(cell.textContent).not.toContain('-');
  });
});

describe('formatSignedAmount', () => {
  it('formats positive/negative/zero with explicit sign and 2 decimals', () => {
    expect(formatSignedAmount(1)).toBe('+1.00');
    expect(formatSignedAmount(-2.5)).toBe('-2.50');
    expect(formatSignedAmount(0)).toBe('0.00');
    expect(formatSignedAmount(1234.5)).toBe('+1,234.50');
  });
  it('non-finite → em-dash', () => {
    expect(formatSignedAmount(NaN)).toBe('—');
    expect(formatSignedAmount(undefined)).toBe('—');
  });
});

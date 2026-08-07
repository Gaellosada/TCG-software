// @vitest-environment jsdom
//
// End-to-end (picker -> legConfig -> compute wire) for the cash_rate leg. The
// picker's Rate tab emits a cash_rate descriptor; AddHoldingModal turns it into
// a leg (label + weight 100), and the compute-body builder emits the exact
// backend contract: {type:'cash_rate', data_source:'v2', cash_rate:{...}} + w100.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import AddHoldingModal from './AddHoldingModal';
import { buildPortfolioComputeBody } from './computeBodyBuilder';

afterEach(cleanup);

// Stub the picker so a click invokes its captured onSelect with the descriptor
// the real Rate tab would emit. Also captures the props so we can assert the
// Rate tab is opted into.
let capturedOnSelect = null;
let capturedPickerProps = null;
vi.mock('../../components/InstrumentPickerModal/InstrumentPickerModal', () => ({
  // eslint-disable-next-line react/prop-types
  default: (props) => {
    capturedOnSelect = props.onSelect;
    capturedPickerProps = props;
    return <div data-testid="picker-stub" />;
  },
}));

describe('AddHoldingModal — cash_rate (Rate) leg mapping', () => {
  it('opts into the Rate tab in add mode', () => {
    render(<AddHoldingModal isOpen onClose={vi.fn()} onAddLeg={vi.fn()} />);
    expect(capturedPickerProps.allowRate).toBe(true);
  });

  it('maps a rate descriptor to a cash_rate leg (v2, weight 100) and emits the contract', () => {
    const onAddLeg = vi.fn();
    const onClose = vi.fn();
    render(<AddHoldingModal isOpen onClose={onClose} onAddLeg={onAddLeg} />);
    fireEvent.click(screen.getByTestId('picker-stub')); // ensure mounted

    capturedOnSelect({
      type: 'cash_rate',
      collection: 'RATE',
      instrument_id: 'RATE_US_CMT_1M',
      data_source: 'v2',
    });

    expect(onAddLeg).toHaveBeenCalledTimes(1);
    const leg = onAddLeg.mock.calls[0][0];
    expect(leg).toMatchObject({
      type: 'cash_rate',
      dataSource: 'v2',
      weight: 100,
      cash_rate: {
        collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'percent', compound: true,
      },
    });
    expect(onClose).toHaveBeenCalledTimes(1);

    // Feed the produced leg through the compute-body builder: the wire leg must
    // be the exact backend contract.
    const { body } = buildPortfolioComputeBody({ legs: [leg], rebalance: 'none' });
    expect(body.legs[leg.label]).toEqual({
      type: 'cash_rate',
      data_source: 'v2',
      cash_rate: {
        collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'percent', compound: true,
      },
    });
    expect(body.weights[leg.label]).toBe(100);
  });
});

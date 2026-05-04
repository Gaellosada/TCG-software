// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import InstrumentPickerModal from './InstrumentPickerModal';

afterEach(cleanup);

vi.mock('../../api/data', () => ({
  listCollections: vi.fn(),
  listInstruments: vi.fn(),
  getAvailableCycles: vi.fn(),
}));

vi.mock('../../api/options', () => ({
  getOptionRoots: vi.fn(),
}));

import { listCollections, listInstruments, getAvailableCycles } from '../../api/data';
import { getOptionRoots } from '../../api/options';

const MOCK_ROOTS = [
  { collection: 'OPT_SP_500', root_label: 'SP 500', name: 'SP 500', has_greeks: true },
  { collection: 'OPT_VIX', root_label: 'VIX', name: 'VIX', has_greeks: false },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_SP_500']);
  vi.mocked(listInstruments).mockResolvedValue({ items: [{ symbol: 'SPX' }] });
  vi.mocked(getAvailableCycles).mockResolvedValue(['M']);
  vi.mocked(getOptionRoots).mockResolvedValue({ roots: MOCK_ROOTS });
});

async function flushAsync() {
  // Wait for async-loaded state to settle
  await waitFor(() => {
    // groups have rendered if at least one Indexes/Futures/Options label is visible
    expect(
      screen.queryAllByText(/Indexes|Assets|Futures|Options/).length,
    ).toBeGreaterThan(0);
  });
}

describe('<InstrumentPickerModal>', () => {
  it('returns null when isOpen is false', () => {
    const { container } = render(
      <InstrumentPickerModal isOpen={false} onClose={vi.fn()} onSelect={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders all 4 categories by default (with Options)', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    // Wait for fut collections + option roots
    await waitFor(() => {
      expect(screen.getByText('Futures')).toBeTruthy();
      expect(screen.getByText('Options')).toBeTruthy();
    });
    expect(screen.getByText('Indexes')).toBeTruthy();
    expect(screen.getByText('Assets')).toBeTruthy();
  });

  it('hiddenCategories=["options"] hides the Options tab', async () => {
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        hiddenCategories={['options']}
      />,
    );
    await flushAsync();
    await waitFor(() => {
      expect(screen.getByText('Futures')).toBeTruthy();
    });
    expect(screen.queryByText('Options')).toBeNull();
    expect(screen.queryByTestId('picker-options-toggle')).toBeNull();
  });

  it('hiddenCategories=["options"] does not call getOptionRoots', async () => {
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        hiddenCategories={['options']}
      />,
    );
    await flushAsync();
    await waitFor(() => {
      expect(screen.getByText('Futures')).toBeTruthy();
    });
    expect(getOptionRoots).not.toHaveBeenCalled();
  });

  it('hiddenCategories=["futures","options"] hides both', async () => {
    render(
      <InstrumentPickerModal
        isOpen={true}
        onClose={vi.fn()}
        onSelect={vi.fn()}
        hiddenCategories={['futures', 'options']}
      />,
    );
    await flushAsync();
    expect(screen.queryByText('Options')).toBeNull();
    expect(screen.queryByText('Futures')).toBeNull();
  });

  it('clicking Options enters drill-down and shows the OptionStreamForm', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => {
      expect(screen.getByTestId('option-stream-form')).toBeTruthy();
    });
    expect(screen.getByLabelText('Root')).toBeTruthy();
    expect(screen.getByTestId('option-stream-confirm')).toBeTruthy();
  });

  it('confirming emits an option_stream-shaped object via onSelect and closes', async () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={onClose} onSelect={onSelect} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());

    const confirm = screen.getByTestId('option-stream-confirm');
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);

    expect(onSelect).toHaveBeenCalledOnce();
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.type).toBe('option_stream');
    expect(emitted.collection).toBe('OPT_SP_500');
    expect(emitted.option_type).toMatch(/^[CP]$/);
    expect(emitted.maturity).toBeTruthy();
    expect(emitted.maturity.kind).toBeTruthy();
    expect(emitted.selection).toBeTruthy();
    expect(emitted.selection.kind).toBeTruthy();
    expect(typeof emitted.stream).toBe('string');
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('back button returns from options drill-down to category list', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());

    // Header should now show 'Options' as title
    expect(screen.getByRole('heading', { name: 'Options' })).toBeTruthy();
    // Back button → leaves drill-down
    const backBtn = screen.getByText('←');
    fireEvent.click(backBtn);
    await waitFor(() => expect(screen.queryByTestId('option-stream-form')).toBeNull());
  });

  it('still emits a spot selection from the existing flow (regression)', async () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<InstrumentPickerModal isOpen={true} onClose={onClose} onSelect={onSelect} />);
    await flushAsync();
    // Open Indexes group (or Assets)
    const indexBtn = screen.getByText('Indexes');
    fireEvent.click(indexBtn);
    await waitFor(() => expect(screen.getByText('SPX')).toBeTruthy());
    fireEvent.click(screen.getByText('SPX'));
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect.mock.calls[0][0]).toMatchObject({
      type: 'spot',
      instrument_id: 'SPX',
    });
  });

  it('disables Confirm when validation fails (greek stream + no-greeks root)', async () => {
    render(<InstrumentPickerModal isOpen={true} onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());

    // Pick OPT_VIX (no greeks) and stream=gamma
    fireEvent.change(screen.getByLabelText('Root'), { target: { value: 'OPT_VIX' } });
    fireEvent.change(screen.getByLabelText('Stream'), { target: { value: 'gamma' } });

    const confirm = screen.getByTestId('option-stream-confirm');
    expect(confirm.disabled).toBe(true);
  });
});

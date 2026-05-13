// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import BlockHeader from './BlockHeader';

afterEach(() => { cleanup(); });

// Minimal v4 block fixture for entries
function entryBlock(weight = 50) {
  return { id: 'blk-1', input_id: '', weight, conditions: [] };
}

// Exit block fixture
function exitBlock() {
  return { id: 'blk-2', conditions: [], target_entry_block_name: '' };
}

const NO_INPUTS = [];
const noop = () => {};

// Helper: render BlockHeader for an entry block and return utilities
function renderEntry(weight, extraProps = {}) {
  const onChange = vi.fn();
  const onDelete = vi.fn();
  render(
    <BlockHeader
      block={entryBlock(weight)}
      section="entries"
      inputs={NO_INPUTS}
      onChange={onChange}
      onDelete={onDelete}
      blockIndex={1}
      {...extraProps}
    />,
  );
  return { onChange, onDelete };
}

// ---------------------------------------------------------------------------
// % suffix
// ---------------------------------------------------------------------------

describe('BlockHeader — % suffix', () => {
  it('renders the % suffix glyph next to the weight input for an entry block', () => {
    renderEntry(50);
    // The suffix span is aria-hidden; query by its text content
    const suffix = screen.getByText('%');
    expect(suffix).toBeDefined();
  });

  it('does NOT render a weight input or % suffix for exit blocks', () => {
    render(
      <BlockHeader
        block={exitBlock()}
        section="exits"
        inputs={NO_INPUTS}
        entryBlocks={[]}
        onChange={noop}
        onDelete={noop}
        blockIndex={1}
      />,
    );
    expect(screen.queryByTestId('block-weight-0')).toBeNull();
    expect(screen.queryByText('%')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Badge — three weight-sign branches
// ---------------------------------------------------------------------------

describe('BlockHeader — badge (weight > 0)', () => {
  it('shows "long" badge text when weight is positive', () => {
    renderEntry(75);
    const badge = screen.getByTestId('block-badge-0');
    expect(badge.textContent).toBe('long');
  });

  it('badge has aria-label "direction: long"', () => {
    renderEntry(75);
    const badge = screen.getByTestId('block-badge-0');
    expect(badge.getAttribute('aria-label')).toBe('direction: long');
  });

  it('applies badgeLong class for positive weight', () => {
    renderEntry(1);
    const badge = screen.getByTestId('block-badge-0');
    // CSS Modules generate mangled class names; verify via aria-label + text
    expect(badge.textContent).toBe('long');
    expect(badge.getAttribute('aria-label')).toBe('direction: long');
  });
});

describe('BlockHeader — badge (weight < 0)', () => {
  it('shows "short" badge text when weight is negative', () => {
    renderEntry(-40);
    const badge = screen.getByTestId('block-badge-0');
    expect(badge.textContent).toBe('short');
  });

  it('badge has aria-label "direction: short"', () => {
    renderEntry(-40);
    const badge = screen.getByTestId('block-badge-0');
    expect(badge.getAttribute('aria-label')).toBe('direction: short');
  });

  it('applies short badge for weight = -100 (boundary)', () => {
    renderEntry(-100);
    expect(screen.getByTestId('block-badge-0').textContent).toBe('short');
  });
});

describe('BlockHeader — badge (weight == 0)', () => {
  it('shows neutral badge (—) when weight is zero', () => {
    renderEntry(0);
    const badge = screen.getByTestId('block-badge-0');
    expect(badge.textContent).toBe('—');
  });

  it('badge has aria-label "direction: neutral" when weight is zero', () => {
    renderEntry(0);
    const badge = screen.getByTestId('block-badge-0');
    expect(badge.getAttribute('aria-label')).toBe('direction: neutral');
  });
});

describe('BlockHeader — badge (no badge on exits)', () => {
  it('does not render a badge span for exit blocks', () => {
    render(
      <BlockHeader
        block={exitBlock()}
        section="exits"
        inputs={NO_INPUTS}
        entryBlocks={[]}
        onChange={noop}
        onDelete={noop}
        blockIndex={1}
      />,
    );
    expect(screen.queryByTestId('block-badge-0')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Weight input: signed [-100, +100], clamp on blur
// ---------------------------------------------------------------------------

describe('BlockHeader — weight input accepts signed values', () => {
  it('clamps weight to +100 on blur when value exceeds upper bound', () => {
    const { onChange } = renderEntry(50);
    const input = screen.getByTestId('block-weight-0');
    fireEvent.change(input, { target: { value: '150' } });
    fireEvent.blur(input, { target: { value: '150' } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ weight: 100 }));
  });

  it('clamps weight to -100 on blur when value is below lower bound', () => {
    const { onChange } = renderEntry(-50);
    const input = screen.getByTestId('block-weight-0');
    fireEvent.change(input, { target: { value: '-200' } });
    fireEvent.blur(input, { target: { value: '-200' } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ weight: -100 }));
  });

  it('accepts negative weight (short signal)', () => {
    const { onChange } = renderEntry(0);
    const input = screen.getByTestId('block-weight-0');
    fireEvent.change(input, { target: { value: '-25' } });
    fireEvent.blur(input, { target: { value: '-25' } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ weight: -25 }));
  });

  it('accepts +100 boundary (full long, no leverage)', () => {
    const { onChange } = renderEntry(0);
    const input = screen.getByTestId('block-weight-0');
    fireEvent.change(input, { target: { value: '100' } });
    fireEvent.blur(input, { target: { value: '100' } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ weight: 100 }));
  });

  it('accepts -100 boundary (full short, no leverage)', () => {
    const { onChange } = renderEntry(0);
    const input = screen.getByTestId('block-weight-0');
    fireEvent.change(input, { target: { value: '-100' } });
    fireEvent.blur(input, { target: { value: '-100' } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ weight: -100 }));
  });

  it('commits weight=0 on blur for empty string input', () => {
    const { onChange } = renderEntry(50);
    const input = screen.getByTestId('block-weight-0');
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.blur(input, { target: { value: '' } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ weight: 0 }));
  });

  it('reflects current weight from block prop as input value', () => {
    renderEntry(33);
    const input = screen.getByTestId('block-weight-0');
    expect(Number(input.value)).toBe(33);
  });
});

// ---------------------------------------------------------------------------
// section prop → label
// ---------------------------------------------------------------------------

describe('BlockHeader — section label', () => {
  it('shows "entry on" for entries section', () => {
    renderEntry(10);
    expect(screen.getByText('entry on')).toBeDefined();
  });

  it('shows "exit on" for exits section', () => {
    render(
      <BlockHeader
        block={exitBlock()}
        section="exits"
        inputs={NO_INPUTS}
        entryBlocks={[]}
        onChange={noop}
        onDelete={noop}
        blockIndex={1}
      />,
    );
    expect(screen.getByText('exit on')).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Delete button
// ---------------------------------------------------------------------------

describe('BlockHeader — delete button', () => {
  it('renders the delete button', () => {
    renderEntry(10);
    expect(screen.getByTestId('remove-block-0')).toBeDefined();
  });

  it('opens the confirm dialog on delete button click', () => {
    renderEntry(10);
    fireEvent.click(screen.getByTestId('remove-block-0'));
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// require-reset <select> — per CONTRACT §5
// Dropdown appears on entries+exits and is suppressed on resets.
// ---------------------------------------------------------------------------

describe('BlockHeader — require-reset select', () => {
  const RESET_BLOCKS = [
    { id: 'r1', name: 'Arm Long' },
    { id: 'r2', name: '' },           // unnamed → "Reset 2" label
    { id: 'r3', name: '   ' },        // whitespace-only → "Reset 3" label
  ];

  function renderEntryWithResets(blockOverrides = {}, resetBlocks = RESET_BLOCKS) {
    const block = { ...entryBlock(50), ...blockOverrides };
    const onChange = vi.fn();
    render(
      <BlockHeader
        block={block}
        section="entries"
        inputs={NO_INPUTS}
        resetBlocks={resetBlocks}
        onChange={onChange}
        onDelete={noop}
        blockIndex={1}
      />,
    );
    return { onChange };
  }

  it('renders the require-reset select on entry blocks', () => {
    renderEntryWithResets();
    expect(screen.getByTestId('require-reset-select-0')).toBeDefined();
  });

  it('renders the require-reset select on exit blocks', () => {
    render(
      <BlockHeader
        block={exitBlock()}
        section="exits"
        inputs={NO_INPUTS}
        entryBlocks={[]}
        resetBlocks={RESET_BLOCKS}
        onChange={noop}
        onDelete={noop}
        blockIndex={1}
      />,
    );
    expect(screen.getByTestId('require-reset-select-0')).toBeDefined();
  });

  it('does NOT render the require-reset select on reset blocks (Sign 4)', () => {
    render(
      <BlockHeader
        block={{ id: 'r1', conditions: [] }}
        section="resets"
        inputs={NO_INPUTS}
        resetBlocks={RESET_BLOCKS}
        onChange={noop}
        onDelete={noop}
        blockIndex={1}
      />,
    );
    expect(screen.queryByTestId('require-reset-select-0')).toBeNull();
  });

  it('selecting an option sets requires_reset_block_id to the reset id (not its name)', () => {
    const { onChange } = renderEntryWithResets();
    const sel = screen.getByTestId('require-reset-select-0');
    fireEvent.change(sel, { target: { value: 'r1' } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ requires_reset_block_id: 'r1' }),
    );
  });

  it('selecting "None" sets requires_reset_block_id to null (not empty string)', () => {
    const { onChange } = renderEntryWithResets({ requires_reset_block_id: 'r1' });
    const sel = screen.getByTestId('require-reset-select-0');
    fireEvent.change(sel, { target: { value: '' } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ requires_reset_block_id: null }),
    );
  });

  it('falls back to "Reset N" (1-based) when a reset block has no name', () => {
    renderEntryWithResets();
    const sel = screen.getByTestId('require-reset-select-0');
    const labels = Array.from(sel.querySelectorAll('option')).map((o) => o.textContent);
    // Index 0 is "None"; r1 has a name; r2 + r3 use the fallback.
    expect(labels).toEqual(['None', 'Arm Long', 'Reset 2', 'Reset 3']);
  });
});

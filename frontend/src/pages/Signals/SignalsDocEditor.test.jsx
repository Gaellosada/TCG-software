// @vitest-environment jsdom
//
// Regression for T1: per-signal documentation editor.
//
// Root cause: SignalsPage passed `section={activeTab}` (always 'entries' or
// 'exits') to BlockEditor. BlockEditor computed `activeTab = sectionProp ||
// internalTab`; since sectionProp was truthy, clicking the Documentation tab
// (which calls setInternalTab('doc')) had no effect — internalTab became 'doc'
// but activeTab stayed sectionProp. DocView was never rendered.
//
// Fix: SignalsPage no longer passes `section` or `onSectionChange` to
// BlockEditor, so `activeTab = sectionProp || internalTab` = internalTab and
// the Documentation tab works correctly.
//
// These tests use BlockEditor directly:
//   - "pre-fix interface" tests: pass section="entries" (the old SignalsPage
//     prop) — assert the bug is present so that reverting SignalsPage breaks
//     these assumptions and highlights the regression.
//   - "post-fix interface" tests: no section prop (the new SignalsPage prop) —
//     assert the doc editor works end-to-end.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

afterEach(() => { cleanup(); });

import BlockEditor from './BlockEditor';
import { emptyRules } from './storage';

vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => []),
  listInstruments: vi.fn(async () => ({ items: [], total: 0, skip: 0, limit: 0 })),
  getAvailableCycles: vi.fn(async () => []),
}));

function renderEditor(extraProps = {}) {
  const onRulesChange = vi.fn();
  const onDocChange = vi.fn();
  const utils = render(
    <BlockEditor
      rules={emptyRules()}
      onRulesChange={onRulesChange}
      inputs={[]}
      indicators={[]}
      doc=""
      onDocChange={onDocChange}
      {...extraProps}
    />,
  );
  return { ...utils, onRulesChange, onDocChange };
}

describe('pre-fix interface: section prop blocks the doc tab', () => {
  it('with section="entries", clicking doc tab does not activate it', async () => {
    renderEditor({ section: 'entries' });
    const docTab = screen.getByTestId('section-tab-doc');
    await act(async () => { fireEvent.click(docTab); });
    expect(docTab.getAttribute('aria-selected')).toBe('false');
    expect(screen.queryByRole('button', { name: /edit documentation/i })).toBeNull();
  });
});

describe('post-fix interface: no section prop — doc tab works', () => {
  it('Documentation tab becomes aria-selected after click', async () => {
    renderEditor();
    const docTab = screen.getByTestId('section-tab-doc');
    expect(docTab.getAttribute('aria-selected')).toBe('false');
    await act(async () => { fireEvent.click(docTab); });
    expect(docTab.getAttribute('aria-selected')).toBe('true');
  });

  it('clicking Documentation tab renders DocView (Edit button visible)', async () => {
    renderEditor();
    await act(async () => { fireEvent.click(screen.getByTestId('section-tab-doc')); });
    expect(screen.getByRole('button', { name: /edit documentation/i })).toBeTruthy();
  });

  it('editing calls onDocChange with typed value', async () => {
    const user = userEvent.setup();
    const { onDocChange } = renderEditor({ doc: '' });
    await act(async () => { fireEvent.click(screen.getByTestId('section-tab-doc')); });
    await user.click(screen.getByRole('button', { name: /edit documentation/i }));
    const textarea = screen.getByRole('textbox', { name: /indicator documentation/i });
    await user.type(textarea, 'Strategy notes');
    textarea.blur();
    expect(onDocChange).toHaveBeenCalledWith('Strategy notes');
  });

  it('existing doc value is shown in read mode before editing', async () => {
    renderEditor({ doc: '# My Signal' });
    await act(async () => { fireEvent.click(screen.getByTestId('section-tab-doc')); });
    expect(screen.getByRole('heading', { level: 1, name: /my signal/i })).toBeTruthy();
  });

  it('switching back from doc to Entries tab shows blocks panel', async () => {
    renderEditor();
    await act(async () => { fireEvent.click(screen.getByTestId('section-tab-doc')); });
    await act(async () => { fireEvent.click(screen.getByTestId('section-tab-entries')); });
    expect(screen.queryByRole('button', { name: /edit documentation/i })).toBeNull();
    expect(screen.getByTestId('add-block-btn')).toBeTruthy();
  });
});

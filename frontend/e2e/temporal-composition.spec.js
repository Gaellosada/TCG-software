import { test, expect } from '@playwright/test';

// ---------------------------------------------------------------------------
// block-temporal-composition v1 — the NON-DEGRADATION proof.
//
// A prior cycle passed API tests but shipped a UI gap: a feature that "worked"
// in the editor silently ran as something else on the backend. The antidote is
// a REAL UI test that drives the Signals editor end-to-end and asserts the
// request body sent to /api/signals/compute CONTAINS the block-level temporal
// ``links`` — proving the chain does NOT silently degrade to plain CNF.
//
// This spec is fully MOCKED (no live backend), modelled on the persistence-
// mock pattern in signals-block-layout.spec.js (page.route + route.fulfill).
// It is COMMITTED so CI runs it. Any live-backend variant is kept UNTRACKED.
//
// Coverage:
//   A. ROUND-TRIP / rehydrate — the persisted "saved payload" (GET) carries a
//      2-condition entry chain (links {1:N}); the editor rehydrates it as a
//      THEN link (NOT two AND'd conditions).
//   B. AUTHORING / save — toggling a fresh gap to THEN in the editor flushes a
//      PUT whose rules carry ``links`` (proves the save path is wired).
//   C. NON-DEGRADATION — clicking Run posts a compute body whose
//      rules.entries[0].links === {1:N} (proves the engine sees the sequence).
// ---------------------------------------------------------------------------

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';

// A fully-runnable v7 SignalOut doc: one configured spot input, an entry block
// with TWO complete (constant-operand) conditions linked A→(5 bars)→B, and an
// exit block targeting that entry. Constant operands keep the run gate green
// without any indicator hydration. ``links`` is the flat
// { successorIdx: withinBars } map keyed by the successor condition's index.
const CHAIN_SIGNAL = {
  id: 'sig-chain',
  name: 'Chain Signal',
  category: 'RESEARCH',
  locked: false,
  description: '',
  inputs: [
    { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: '^GSPC' } },
  ],
  rules: {
    entries: [
      {
        id: 'blk-entry',
        name: 'Entry1',
        input_id: 'X',
        weight: 100,
        enabled: true,
        description: '',
        conditions: [
          { op: 'gt', lhs: { kind: 'instrument', input_id: 'X', field: 'close' }, rhs: { kind: 'constant', value: 0 } },
          { op: 'gt', lhs: { kind: 'instrument', input_id: 'X', field: 'close' }, rhs: { kind: 'constant', value: 1 } },
        ],
        links: { 1: 5 },
        requires_reset_block_id: null,
        requires_reset_count: 1,
      },
    ],
    exits: [
      {
        id: 'blk-exit',
        name: 'Exit1',
        enabled: true,
        description: '',
        target_entry_block_names: ['Entry1'],
        conditions: [
          { op: 'lt', lhs: { kind: 'instrument', input_id: 'X', field: 'close' }, rhs: { kind: 'constant', value: 0 } },
        ],
        requires_reset_block_id: null,
        requires_reset_count: 1,
      },
    ],
    resets: [],
  },
  settings: { dont_repeat: true },
};

const COMPUTE_RESPONSE = {
  timestamps: [1577923200000, 1578009600000, 1578268800000],
  positions: [
    {
      input_id: 'X',
      instrument: { type: 'spot', collection: 'INDEX', instrument_id: '^GSPC' },
      values: [0, 1, 0],
      clipped_mask: [false, false, false],
      price: { label: '^GSPC.close', values: [3200, 3250, 3275] },
    },
  ],
  indicators: [],
  events: [],
  realized_pnl: [[0, 50, 75]],
  trades: [],
  clipped: false,
  diagnostics: {},
};

/**
 * Wire up every backend endpoint the Signals page touches. ``capture`` is a
 * mutable object the test reads after the interaction:
 *   capture.computeBody — the parsed body POSTed to /api/signals/compute
 *   capture.putBodies   — every parsed body PUT to /api/persistence/signals/:id
 */
async function mockBackend(page, capture, { listDoc = CHAIN_SIGNAL } = {}) {
  capture.putBodies = [];
  capture.computeBody = null;

  // Persistence: GET list returns the seeded doc; PUT/lock/etc. echo a doc and
  // record their bodies so the save path can be asserted.
  await page.route('**/api/persistence/signals**', async (route) => {
    const req = route.request();
    const method = req.method();
    if (method === 'GET') {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify([listDoc]),
      });
      return;
    }
    if (method === 'PUT') {
      try { capture.putBodies.push(JSON.parse(req.postData() || '{}')); } catch { /* ignore */ }
    }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(listDoc),
    });
  });
  await page.route('**/api/persistence/indicators**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/api/persistence/portfolios**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/api/persistence/baskets**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });

  // Data endpoints for the instrument picker / input panel.
  await page.route('**/api/data/collections*', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ collections: ['INDEX'] }),
    });
  });
  await page.route('**/api/data/INDEX*', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        items: [{ symbol: '^GSPC', asset_class: 'INDEX', collection: 'INDEX' }],
        total: 1, skip: 0, limit: 500,
      }),
    });
  });
  await page.route('**/api/options/**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });

  // The compute endpoint — capture the POST body, then return a valid result.
  await page.route('**/api/signals/compute', async (route) => {
    try { capture.computeBody = JSON.parse(route.request().postData() || '{}'); } catch { /* ignore */ }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(COMPUTE_RESPONSE),
    });
  });
}

test.describe('block-temporal-composition — chain does NOT degrade to CNF', () => {
  test('A. a persisted chain rehydrates as a THEN link (not two AND conditions)', async ({ page }) => {
    const capture = {};
    await mockBackend(page, capture);
    await page.goto(`${BASE}/signals`);

    // The seeded signal auto-selects → the entry block + its two conditions render.
    await expect(page.getByTestId('block-editor')).toBeVisible();
    await expect(page.getByTestId('block-0')).toBeVisible();
    await expect(page.getByTestId('condition-0-0')).toBeVisible();
    await expect(page.getByTestId('condition-0-1')).toBeVisible();

    // The gap before condition 2 (successor index 1) shows the THEN toggle —
    // proving the chain rehydrated. A degraded-to-CNF load would show "AND".
    const linkToggle = page.getByTestId('link-toggle-0-1');
    await expect(linkToggle).toBeVisible();
    await expect(linkToggle).toHaveText('THEN');

    // The per-link window reflects the persisted 5 bars.
    await expect(page.getByTestId('link-window-0-1')).toHaveValue('5');
  });

  test('B. toggling a fresh gap to THEN flushes a PUT carrying links (save path wired)', async ({ page }) => {
    // Seed a CNF variant (no links) so we can author the chain in the editor.
    const cnfDoc = JSON.parse(JSON.stringify(CHAIN_SIGNAL));
    delete cnfDoc.rules.entries[0].links;
    const capture = {};
    await mockBackend(page, capture, { listDoc: cnfDoc });
    await page.goto(`${BASE}/signals`);

    await expect(page.getByTestId('block-0')).toBeVisible();
    // Starts as AND (CNF).
    const toggle = page.getByTestId('link-toggle-0-1');
    await expect(toggle).toHaveText('AND');

    // Toggle AND → THEN; the editor writes links{1:5} and autosave debounces a PUT.
    await toggle.click();
    await expect(toggle).toHaveText('THEN');

    // Wait for the debounced autosave PUT to land and assert it carried links.
    await expect.poll(
      () => {
        const withLinks = capture.putBodies.find(
          (b) => b && b.rules && Array.isArray(b.rules.entries)
            && b.rules.entries[0] && b.rules.entries[0].links
            && Number(b.rules.entries[0].links['1']) === 5,
        );
        return withLinks ? 'found' : 'not-yet';
      },
      { timeout: 8000, message: 'no PUT body carried entries[0].links {1:5}' },
    ).toBe('found');
  });

  test('C. clicking Run posts a compute body that CONTAINS links (non-degradation)', async ({ page }) => {
    const capture = {};
    await mockBackend(page, capture);
    await page.goto(`${BASE}/signals`);

    await expect(page.getByTestId('block-0')).toBeVisible();

    // The run gate should be satisfied (configured input, weighted entry with
    // complete conditions, an exit targeting it). Run.
    const runBtn = page.getByTestId('run-signal-btn');
    await expect(runBtn).toBeEnabled();
    await runBtn.click();

    // The compute POST must have fired with the chain intact.
    await expect.poll(() => (capture.computeBody ? 'sent' : 'pending'), { timeout: 8000 })
      .toBe('sent');

    const entry = capture.computeBody.spec.rules.entries[0];
    // THE non-degradation assertion: links survived the wire. A silent CNF
    // degradation would omit this key and the backend would run plain AND.
    expect(entry.links, 'compute body dropped entries[0].links — it degraded to CNF!').toBeTruthy();
    expect(Number(entry.links['1'])).toBe(5);
    // And the conditions are still both present (the chain didn't lose a leg).
    expect(entry.conditions).toHaveLength(2);
    // Resets must never carry links.
    expect(capture.computeBody.spec.rules.resets.every((r) => !('links' in r))).toBe(true);
  });

  test('D. a cross_count condition ships count/window on the compute body', async ({ page }) => {
    // Seed a signal whose entry has a single cross_above with count=3 within 10.
    const crossDoc = JSON.parse(JSON.stringify(CHAIN_SIGNAL));
    crossDoc.id = 'sig-cross';
    crossDoc.name = 'Cross Signal';
    crossDoc.rules.entries[0].conditions = [
      {
        op: 'cross_above',
        lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
        rhs: { kind: 'constant', value: 0 },
        count: 3, window: 10,
      },
    ];
    delete crossDoc.rules.entries[0].links; // single condition → no chain
    const capture = {};
    await mockBackend(page, capture, { listDoc: crossDoc });
    await page.goto(`${BASE}/signals`);

    await expect(page.getByTestId('block-0')).toBeVisible();
    // The cross controls are visible (count > 1).
    await expect(page.getByTestId('cross-controls-0-0')).toBeVisible();
    await expect(page.getByTestId('cross-count-0-0')).toHaveValue('3');

    const runBtn = page.getByTestId('run-signal-btn');
    await expect(runBtn).toBeEnabled();
    await runBtn.click();

    await expect.poll(() => (capture.computeBody ? 'sent' : 'pending'), { timeout: 8000 })
      .toBe('sent');
    const cond = capture.computeBody.spec.rules.entries[0].conditions[0];
    expect(cond.count).toBe(3);
    expect(cond.window).toBe(10);
  });

  test('E. all-or-nothing — toggling a 3-condition block ships a FULL chain {1,2} (no partial chain)', async ({ page }) => {
    // Seed a CNF 3-condition entry so we author the chain in the editor. The
    // backend rejects a partial chain (links must cover every successor gap),
    // so the FE must emit BOTH gaps when the block is sequenced.
    const threeCnf = JSON.parse(JSON.stringify(CHAIN_SIGNAL));
    threeCnf.id = 'sig-three';
    threeCnf.rules.entries[0].conditions = [
      { op: 'gt', lhs: { kind: 'instrument', input_id: 'X', field: 'close' }, rhs: { kind: 'constant', value: 0 } },
      { op: 'gt', lhs: { kind: 'instrument', input_id: 'X', field: 'close' }, rhs: { kind: 'constant', value: 1 } },
      { op: 'gt', lhs: { kind: 'instrument', input_id: 'X', field: 'close' }, rhs: { kind: 'constant', value: 2 } },
    ];
    delete threeCnf.rules.entries[0].links;
    const capture = {};
    await mockBackend(page, capture, { listDoc: threeCnf });
    await page.goto(`${BASE}/signals`);

    await expect(page.getByTestId('block-0')).toBeVisible();
    await expect(page.getByTestId('condition-0-2')).toBeVisible();
    // Toggle the SECOND gap → the whole block becomes a chain (both gaps).
    await page.getByTestId('link-toggle-0-2').click();
    // Both gaps now read THEN.
    await expect(page.getByTestId('link-toggle-0-1')).toHaveText('THEN');
    await expect(page.getByTestId('link-toggle-0-2')).toHaveText('THEN');

    const runBtn = page.getByTestId('run-signal-btn');
    await expect(runBtn).toBeEnabled();
    await runBtn.click();
    await expect.poll(() => (capture.computeBody ? 'sent' : 'pending'), { timeout: 8000 })
      .toBe('sent');
    const entry = capture.computeBody.spec.rules.entries[0];
    // FULL coverage — both successor gaps present (backend would 400 a partial).
    expect(Object.keys(entry.links).sort()).toEqual(['1', '2']);
    expect(Number(entry.links['1'])).toBeGreaterThanOrEqual(1);
    expect(Number(entry.links['2'])).toBeGreaterThanOrEqual(1);
  });
});

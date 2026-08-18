// @vitest-environment jsdom
/**
 * ``URL_ENUMS`` in ``ObjectDetail.jsx`` is a THIRD copy of the filter-enum
 * domain: the backend owns ``_SERIE_TYPE_VALUES`` / ``_FREQ_VALUES`` /
 * ``_OPTION_TYPE_VALUES`` in ``tcg/core/api/data_v2.py`` (pinned against the
 * reader frozensets by ``tests/unit/test_api_data_v2.py``), and the frontend
 * re-declares them here to drop URL values the backend would 400 on. Nothing
 * pinned that third copy, so a backend enum addition (say ``serie_type=trades``)
 * would leave ``urlEnum`` silently dropping the new value from shared links.
 *
 * This test reads the backend tuples straight out of the Python source and
 * asserts ``URL_ENUMS`` equals them, so the copies cannot diverge unnoticed.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Chart pulls in Plotly (canvas); this test never renders, but importing
// ObjectDetail evaluates the whole module tree, so stub it out.
import { vi } from 'vitest';
vi.mock('../../components/Chart', () => ({ default: () => null }));
vi.mock('../../api/dataV2', () => ({
  listObjectsV2: vi.fn(),
  getObjectDetailV2: vi.fn(),
  getObjectFacetsV2: vi.fn(),
  getObjectSeriesV2: vi.fn(),
  getSeriesV2: vi.fn(),
  getContinuousFuturesV2: vi.fn(),
  getV2FuturesCycles: vi.fn(),
  getContinuousOptionsV2: vi.fn(),
}));

import { URL_ENUMS } from './ObjectDetail';

/** Extract a Python string-tuple literal ``NAME = ("a", "b", ...)``. */
function backendTuple(src, name) {
  const m = src.match(new RegExp(`${name}\\s*=\\s*\\(([^)]*)\\)`));
  if (!m) throw new Error(`could not find ${name} in data_v2.py`);
  return m[1]
    .split(',')
    .map((s) => s.trim().replace(/^['"]|['"]$/g, ''))
    .filter((s) => s.length > 0);
}

// Vitest runs with the ``frontend`` package dir as cwd; the backend lives one up.
const SRC = readFileSync(
  resolve(process.cwd(), '../tcg/core/api/data_v2.py'),
  'utf8',
);

describe('URL_ENUMS is pinned to the backend enum domains', () => {
  it('serie_type matches _SERIE_TYPE_VALUES', () => {
    expect(new Set(URL_ENUMS.serie_type))
      .toEqual(new Set(backendTuple(SRC, '_SERIE_TYPE_VALUES')));
  });

  it('freq matches _FREQ_VALUES', () => {
    expect(new Set(URL_ENUMS.freq))
      .toEqual(new Set(backendTuple(SRC, '_FREQ_VALUES')));
  });

  it('option_type matches _OPTION_TYPE_VALUES', () => {
    expect(new Set(URL_ENUMS.option_type))
      .toEqual(new Set(backendTuple(SRC, '_OPTION_TYPE_VALUES')));
  });
});

// Registry of read-only default indicators shipped with the UI.
//
// Each entry lives in its own file under ``./defaults/`` and uses the
// typed-signature convention the backend enforces:
//   def compute(series, <name>: <int|float|bool> = <literal>, ...)
// and accesses named series via ``series['label']`` so the frontend can
// auto-render one slot per label.
//
// Contract per entry (see ``./defaults/*.js``):
//   - ``id``        stable kebab-case string; used as key in localStorage
//                   defaultState and MUST match the filename.
//   - ``name``      user-visible label (not editable).
//   - ``readonly``  always ``true`` — code + name locked; params + series
//                   picks are still user-editable per-session.
//   - ``category``  one of ``'trend' | 'momentum' | 'volatility' |
//                   'pattern' | 'statistical'``. Single source of truth for
//                   grouping the indicator in the library UI and for the
//                   registry partition checks in ``defaultIndicators.test.js``.
//   - ``compatibleAssetTypes`` array of asset-type literals from
//                   ``./assetTypes.js`` (``'index' | 'equity' | 'option'``)
//                   declaring which input streams the indicator is validated
//                   against. The 9 surviving v1 defaults all ship as
//                   ``['index', 'equity']`` (option streams are out of scope
//                   for the legacy library — see Wave 2c additions).
//   - ``chartShape`` string — currently always ``'time-series'`` for v1.
//                   Cross-sectional renderers are deferred.
//   - ``code``      Python source string.
//   - ``params``    always ``{}`` — the UI derives it from the parsed
//                   signature at load time.
//   - ``seriesMap`` always ``{}`` — the UI derives it from parsed labels.
//   - ``ownPanel``  bool — false overlays indicator on price; true stacks
//                   indicator as a separate subplot below price.
//   - ``chartMode`` optional string — passed through to Plotly trace.mode
//                   for the indicator trace. Default ``'lines'``. Use
//                   ``'markers'`` for indicators whose output is sparse
//                   (mostly NaN, e.g. ``swing-pivots``) so the points are
//                   actually visible on the chart. ``'lines+markers'`` is
//                   also accepted for hybrid rendering.
//
// IMPORTANT: the name and code here are the canonical source of truth.
// They are NEVER overwritten by localStorage contents — only per-session
// param / series picks persist (see ``defaultState`` in storage.js).
//
// Stale ``defaultState`` overlays keyed by an id not in this registry are
// silently ignored by ``IndicatorsPage.hydrateDefault`` — no migration
// step is required when entries are added, removed, or renamed.
//
// Library shape (post Wave 2c additions): 11 default entries.
//   Trend:        sma, ema
//   Momentum:     rsi, macd-line, macd-signal, macd-histogram
//   Volatility:   historical-vol, atm-contract-iv, term-structure-slope
//   Pattern:      swing-pivots
//   Statistical:  percentile-filtered-return
//
// See ``docs/indicators.md`` for the full reference and
// ``docs/design-decisions.md`` for the rationale behind the prune.

// --- Trend ------------------------------------------------------------
import sma from './defaults/sma';
import ema from './defaults/ema';

// --- Momentum ---------------------------------------------------------
import rsi from './defaults/rsi';
import macdLine from './defaults/macd-line';
import macdSignal from './defaults/macd-signal';
import macdHistogram from './defaults/macd-histogram';

// --- Volatility -------------------------------------------------------
import historicalVol from './defaults/historical-vol';
import atmContractIv from './defaults/atm-contract-iv';
import termStructureSlope from './defaults/term-structure-slope';

// --- Pattern ----------------------------------------------------------
import swingPivots from './defaults/swing-pivots';

// --- Statistical ------------------------------------------------------
import percentileFilteredReturn from './defaults/percentile-filtered-return';

export const DEFAULT_INDICATORS = [
  // Trend
  sma,
  ema,
  // Momentum
  rsi,
  macdLine,
  macdSignal,
  macdHistogram,
  // Volatility
  historicalVol,
  atmContractIv,
  termStructureSlope,
  // Pattern
  swingPivots,
  // Statistical
  percentileFilteredReturn,
];

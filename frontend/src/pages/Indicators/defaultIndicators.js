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
// Library shape (post 2026-04 pruning rework): 5 canonical logical
// indicators (10 JS entries — SMA, EMA, RSI, MACD triple, Bollinger quad)
// plus 13 legacy-port entries translated from the Java simulator. See
// ``docs/indicators.md`` for the full reference and ``docs/design-decisions.md``
// for the rationale behind the pruning and the per-port behavioural notes.

// --- Trend ------------------------------------------------------------
import sma from './defaults/sma';
import ema from './defaults/ema';

// --- Momentum ---------------------------------------------------------
import rsi from './defaults/rsi';
import macdLine from './defaults/macd-line';
import macdSignal from './defaults/macd-signal';
import macdHistogram from './defaults/macd-histogram';
import impetus from './defaults/impetus';
import weightedImpetus from './defaults/weighted-impetus';
import centredSlope from './defaults/centred-slope';
import slopeAcceleration from './defaults/slope-acceleration';

// --- Volatility -------------------------------------------------------
import atr from './defaults/atr';
import bollingerUpper from './defaults/bollinger-upper';
import bollingerMiddle from './defaults/bollinger-middle';
import bollingerLower from './defaults/bollinger-lower';
import bollingerPercentB from './defaults/bollinger-percent-b';

// --- Pattern ----------------------------------------------------------
import engulfmentPattern from './defaults/engulfment-pattern';
import engulfmentExit from './defaults/engulfment-exit';
import swingPivots from './defaults/swing-pivots';
import trailingExtreme from './defaults/trailing-extreme';

// --- Statistical ------------------------------------------------------
import absoluteMean from './defaults/absolute-mean';
import slopeStatistics from './defaults/slope-statistics';
import rollingPercentileBands from './defaults/rolling-percentile-bands';
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
  impetus,
  weightedImpetus,
  centredSlope,
  slopeAcceleration,
  // Volatility
  atr,
  bollingerUpper,
  bollingerMiddle,
  bollingerLower,
  bollingerPercentB,
  // Pattern
  engulfmentPattern,
  engulfmentExit,
  swingPivots,
  trailingExtreme,
  // Statistical
  absoluteMean,
  slopeStatistics,
  rollingPercentileBands,
  percentileFilteredReturn,
];

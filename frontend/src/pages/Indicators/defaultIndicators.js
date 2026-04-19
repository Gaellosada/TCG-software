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
//   - ``code``      Python source string.
//   - ``params``    always ``{}`` — the UI derives it from the parsed
//                   signature at load time.
//   - ``seriesMap`` always ``{}`` — the UI derives it from parsed labels.
//
// IMPORTANT: the name and code here are the canonical source of truth.
// They are NEVER overwritten by localStorage contents — only per-session
// param / series picks persist (see ``defaultState`` in storage.js).
//
// Stale ``defaultState`` overlays keyed by an id not in this registry
// (e.g. the legacy ``sma-20`` seed) are silently ignored by
// ``IndicatorsPage.hydrateDefault`` — no migration step required.

// --- Trend ------------------------------------------------------------
import sma from './defaults/sma';
import ema from './defaults/ema';
import wma from './defaults/wma';
import dema from './defaults/dema';
import tema from './defaults/tema';
import kama from './defaults/kama';

// --- Momentum ---------------------------------------------------------
import rsi from './defaults/rsi';
import roc from './defaults/roc';
import momentum from './defaults/momentum';
import macdLine from './defaults/macd-line';
import macdSignal from './defaults/macd-signal';
import macdHistogram from './defaults/macd-histogram';

// --- Volatility -------------------------------------------------------
import bollingerUpper from './defaults/bollinger-upper';
import bollingerMiddle from './defaults/bollinger-middle';
import bollingerLower from './defaults/bollinger-lower';
import bollingerPercentB from './defaults/bollinger-percent-b';
import rollingStddev from './defaults/rolling-stddev';

// --- Derived ----------------------------------------------------------
import logReturn from './defaults/log-return';
import simpleReturn from './defaults/simple-return';
import rollingZscore from './defaults/rolling-zscore';
import rollingMin from './defaults/rolling-min';
import rollingMax from './defaults/rolling-max';

export const DEFAULT_INDICATORS = [
  // Trend
  sma,
  ema,
  wma,
  dema,
  tema,
  kama,
  // Momentum
  rsi,
  roc,
  momentum,
  macdLine,
  macdSignal,
  macdHistogram,
  // Volatility
  bollingerUpper,
  bollingerMiddle,
  bollingerLower,
  bollingerPercentB,
  rollingStddev,
  // Derived
  logReturn,
  simpleReturn,
  rollingZscore,
  rollingMin,
  rollingMax,
];

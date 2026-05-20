/**
 * Discriminated-union descriptor types emitted by the instrument picker
 * subsystem. Mirrors React's `InputInstrument` v3 shape (with the basket
 * `kind` discriminator separating saved-ref from inline composer).
 */

export interface TcgSpotInstrumentRef {
  type: 'spot';
  collection: string;
  instrument_id: string;
}

export interface TcgContinuousInstrumentRef {
  type: 'continuous';
  collection: string;
  strategy: 'front_month';
  adjustment: 'none' | 'ratio' | 'difference' | string;
  cycle: string | null;
  rollOffset: number;
}

export interface TcgOptionStreamRef {
  type: 'option_stream';
  collection: string;
  option_type: 'C' | 'P';
  cycle: string | null;
  maturity: Record<string, unknown>;
  selection: Record<string, unknown>;
  stream: string;
}

export interface TcgSavedBasketRef {
  type: 'basket';
  kind: 'saved';
  basket_id: string;
}

export interface TcgInlineBasketRef {
  type: 'basket';
  kind: 'inline';
  asset_class: 'future' | 'option' | 'index' | 'equity';
  legs: Array<{ instrument: TcgInstrumentLeg; weight: number }>;
}

export type TcgInstrumentLeg =
  | TcgSpotInstrumentRef
  | TcgContinuousInstrumentRef
  | TcgOptionStreamRef;

export type TcgInstrumentDescriptor =
  | TcgSpotInstrumentRef
  | TcgContinuousInstrumentRef
  | TcgOptionStreamRef
  | TcgSavedBasketRef
  | TcgInlineBasketRef;

export type TcgBasketAssetClass = 'future' | 'option' | 'index' | 'equity';

/**
 * Map a basket asset_class to the leg's `instrument.type`. The composer's
 * per-class branch enforces this mapping structurally (a strict-mismatched
 * basket is impossible to produce).
 */
export function tcgInstrumentTypeForAssetClass(
  assetClass: TcgBasketAssetClass,
): TcgInstrumentLeg['type'] {
  if (assetClass === 'future') return 'continuous';
  if (assetClass === 'option') return 'option_stream';
  return 'spot';
}

/**
 * Map an asset_class to the candidate collection prefixes it spans.
 */
export function tcgCollectionsForAssetClass(
  assetClass: TcgBasketAssetClass,
  allCollections: ReadonlyArray<string>,
): string[] {
  if (assetClass === 'future') return allCollections.filter((c) => c.startsWith('FUT_'));
  if (assetClass === 'option') return allCollections.filter((c) => c.startsWith('OPT_'));
  if (assetClass === 'index') return allCollections.filter((c) => c === 'INDEX');
  if (assetClass === 'equity') return allCollections.filter((c) => c === 'ETF');
  return [];
}

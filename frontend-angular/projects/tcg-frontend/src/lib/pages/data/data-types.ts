/**
 * Discriminated-union of instrument selections the Data page can hold.
 * Library-internal — not re-exported from public-api.
 */

export interface TcgDataSelectionInstrument {
  type?: undefined | 'instrument';
  symbol: string;
  collection: string;
}

export interface TcgDataSelectionContinuous {
  type: 'continuous';
  collection: string;
}

export interface TcgDataSelectionOption {
  type: 'option';
  collection: string;
  instrument_id: string | null;
  expiry: string | null;
  strike: number | null;
  optionType: 'C' | 'P' | null;
  last_trade_date: string | null;
  expiration_last: string | null;
}

export type TcgDataSelection =
  | TcgDataSelectionInstrument
  | TcgDataSelectionContinuous
  | TcgDataSelectionOption;

export interface TcgContractRef {
  collection: string;
  instrument_id: string;
  expiry: string | null;
  strike: number | null;
  optionType: 'C' | 'P' | null;
}

export type TcgOptionsViewTab = 'chain' | 'continuous' | 'snapshot';

/** Type guard. */
export function tcgIsOptionSelection(
  sel: TcgDataSelection | null,
): sel is TcgDataSelectionOption {
  return !!sel && sel.type === 'option';
}

/** Type guard. */
export function tcgIsContinuousSelection(
  sel: TcgDataSelection | null,
): sel is TcgDataSelectionContinuous {
  return !!sel && sel.type === 'continuous';
}

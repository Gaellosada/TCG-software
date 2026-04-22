// Fetch the date range for a signal leg: for each input, resolve the
// instrument range; return the overlap (latest start, earliest end).
import { getInstrumentPrices, getContinuousSeries } from '../../api/data';
import { formatDateInt } from '../../utils/format';

export async function fetchSignalLegRange(leg) {
  const inputs = leg.signalSpec?.inputs || [];
  const configured = inputs.filter((inp) => inp.instrument);
  if (configured.length === 0) {
    return { id: leg.id, start: null, end: null };
  }

  const inputRanges = await Promise.all(
    configured.map(async (inp) => {
      try {
        let dates;
        const inst = inp.instrument;
        if (inst.type === 'continuous') {
          const res = await getContinuousSeries(inst.collection, {
            strategy: inst.strategy || 'front_month',
            adjustment: inst.adjustment || 'none',
            cycle: inst.cycle || undefined,
            rollOffset: inst.rollOffset || 0,
          });
          dates = res?.dates;
        } else {
          const res = await getInstrumentPrices(
            inst.collection,
            inst.instrument_id || inst.symbol,
          );
          dates = res?.dates;
        }
        if (dates && dates.length > 0) {
          return {
            start: formatDateInt(dates[0]),
            end: formatDateInt(dates[dates.length - 1]),
          };
        }
        return null;
      } catch {
        return null;
      }
    }),
  );

  const valid = inputRanges.filter(Boolean);
  if (valid.length === 0) {
    return { id: leg.id, start: null, end: null };
  }

  // Overlap = latest start, earliest end
  const start = valid.reduce((a, b) => (a.start > b.start ? a : b)).start;
  const end = valid.reduce((a, b) => (a.end < b.end ? a : b)).end;

  if (start <= end) {
    return { id: leg.id, start, end };
  }
  return { id: leg.id, start: null, end: null };
}

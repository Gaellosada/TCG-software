// Pure stringification of a leg list into a stable key used to gate
// the date-range refetch effect in usePortfolio. Only data-affecting
// fields contribute — label/weight changes must NOT retrigger the
// fetch. Changing the key format invalidates the cache heuristic.
export function legsToRangesKey(legs) {
  return legs.map((l) => {
    if (l.type === 'signal') {
      // Include input instruments so re-binding triggers a refetch
      const inputKeys = (l.signalSpec?.inputs || []).map((inp) => {
        const inst = inp.instrument;
        if (!inst) return 'null';
        if (inst.type === 'continuous') return `c:${inst.collection}:${inst.strategy}:${inst.adjustment}:${inst.cycle}:${inst.rollOffset}`;
        return `i:${inst.collection}:${inst.instrument_id}`;
      }).join(',');
      return `s:${l.signalId}:[${inputKeys}]`;
    }
    if (l.type === 'option_stream') {
      const m = l.maturity || {};
      const s = l.selection || {};
      // Include adjustment + roll_offset (mirrors the continuous key's
      // adjustment/rollOffset): roll_offset shifts which contract is picked
      // per date, so changing it must retrigger the range refetch.
      return `o:${l.collection}:${l.option_type}:${l.cycle}:${JSON.stringify(m)}:${JSON.stringify(s)}:${l.stream}:${l.adjustment ?? 'none'}:${l.roll_offset ?? 0}`;
    }
    if (l.type === 'continuous') return `c:${l.collection}:${l.strategy}:${l.adjustment}:${l.cycle}:${l.rollOffset}`;
    return `i:${l.collection}:${l.symbol}`;
  }).join('|');
}

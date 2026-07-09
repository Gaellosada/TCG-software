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
      // Include roll_offset (the unified {value, unit} object — JSON-encoded so
      // both parts are in the key): it shifts which contract is picked, so
      // changing it must retrigger the range refetch. Option streams carry no
      // back-adjustment, so there is no adjustment segment.
      return `o:${l.collection}:${l.option_type}:${l.cycle}:${JSON.stringify(m)}:${JSON.stringify(s)}:${l.stream}:${JSON.stringify(l.roll_offset ?? { value: 0, unit: 'days' })}`;
    }
    // rank shifts which contract is held (NTH_NEAREST), so it must be part of
    // the range-refetch key; ``?? 1`` keeps a non-nth_nearest leg's key stable.
    if (l.type === 'continuous') return `c:${l.collection}:${l.strategy}:${l.adjustment}:${l.cycle}:${l.rollOffset}:${l.rank ?? 1}`;
    return `i:${l.collection}:${l.symbol}`;
  }).join('|');
}

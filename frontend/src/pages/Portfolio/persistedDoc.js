// Convert a persisted portfolio doc's legs into in-memory legs.
//
// Shared by usePortfolio.loadFromPersisted (which stamps a local React-key id
// afterwards) AND the saved-list cache-status detection (which must build the
// compute body from the EXACT same legs, or the row "cached" icons would key
// off a different body than the compute path). Does NOT stamp an id — that is a
// UI concern the caller adds; the compute body ignores id anyway.

export function persistedDocToLegs(doc) {
  const backendLegs = doc && Array.isArray(doc.legs) ? doc.legs : [];
  return backendLegs.map((l) => {
    const leg = { ...l };
    // Backward-compat: an option PRICE leg (mid/bs_mid) is hold-ON-only (the
    // backend rejects hold-off). A portfolio saved before that rule has no
    // hold_between_rolls, so coerce it — identical to the old inline logic in
    // loadFromPersisted so the resulting body/key matches exactly.
    if (leg.type === 'option_stream' && (leg.stream === 'mid' || leg.stream === 'bs_mid')) {
      leg.hold_between_rolls = true;
      if (typeof leg.nav_times !== 'number') leg.nav_times = 1.0;
    }
    return leg;
  });
}

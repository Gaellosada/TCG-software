// Pre-flight gating for the Run button on the Indicators page.
//
// Two orthogonal flows live here:
//
//   1. Slot completeness (``areAllSlotsFilled`` /
//      ``computeRunDisabledReason``) — every series label declared in
//      the parsed signature must have a SeriesRef picked.
//
//   2. Asset-type compatibility (``computeAssetCompatibility``) — when
//      the indicator declares ``compatibleAssetTypes``, every filled
//      series slot must agree on a single inferred asset_type AND that
//      type must be in the indicator's compat list. The function
//      returns a structured envelope so callers can branch on a typed
//      ``reason`` instead of regexing a tooltip string (Sign 10).
//
// User-visible Run-disabled tooltip strings are preserved verbatim.
import { inferAssetType } from './assetTypes';

/**
 * A SeriesRef counts as "filled" when its identifying fields are all
 * present. The variants:
 *   - ``spot``         — needs ``collection`` AND ``instrument_id``.
 *   - ``continuous``   — identified by ``collection`` alone (rolled
 *                        from a futures family, no instrument_id).
 *   - ``option_stream``— identified by ``collection``, ``option_type``,
 *                        a ``maturity`` rule, a ``selection`` rule, and
 *                        a ``stream`` field. No ``instrument_id``.
 *   - legacy / typeless — defaulted to spot semantics.
 */
function isRefFilled(picked) {
  if (!picked || !picked.collection) return false;
  if (picked.type === 'continuous') return true;
  if (picked.type === 'option_stream') {
    return !!picked.option_type
      && !!picked.maturity
      && !!picked.selection
      && typeof picked.stream === 'string'
      && picked.stream.length > 0;
  }
  return !!picked.instrument_id;
}

export function areAllSlotsFilled(selectedIndicator, seriesLabels) {
  return !!selectedIndicator
    && seriesLabels.length > 0
    && seriesLabels.every((lbl) => isRefFilled(selectedIndicator.seriesMap?.[lbl]));
}

export function computeRunDisabledReason(selectedIndicator, seriesLabels) {
  if (!selectedIndicator) return 'Select an indicator first';
  if (!selectedIndicator.code || !selectedIndicator.code.trim()) return 'Add code before running';
  const emptyLabel = seriesLabels.find(
    (lbl) => !isRefFilled(selectedIndicator.seriesMap?.[lbl]),
  );
  if (emptyLabel) return `Fill series slot: ${emptyLabel}`;
  // Asset-type compatibility check — only meaningful once all slots
  // are filled, so it lives after the empty-slot guard.
  const compat = computeAssetCompatibility(selectedIndicator);
  if (!compat.ok) {
    if (compat.reason === 'slot_conflict') {
      return `Series slots disagree on asset type (${compat.types.join(', ')})`;
    }
    if (compat.reason === 'incompatible_asset') {
      return `Requires ${compat.accepted_asset_types.join(' or ')} data; current asset is ${compat.asset_type}`;
    }
  }
  // Option-stream-specific pre-flight (tautological by_delta+delta).
  // Catches a deterministic backend-422 before firing the request.
  const streamSanity = computeOptionStreamSanity(selectedIndicator);
  if (!streamSanity.ok && streamSanity.reason === 'tautological_option_stream') {
    return `Series slot ${streamSanity.label}: by_delta selection with stream='delta' is tautological`;
  }
  return 'Cannot run';
}

/**
 * Derive a single asset_type from a fully-populated seriesMap by
 * running ``inferAssetType`` on every filled slot.
 *
 * Returns:
 *   - { ok: true, asset_type: 'index'|'equity'|'option' }
 *       all slots agreed on one type.
 *   - { ok: true, asset_type: null }
 *       no inferable type (e.g. unknown collection on every slot, or
 *       no slots at all). Callers should not block on this — the
 *       backend's compat check is the canonical decider.
 *   - { ok: false, reason: 'slot_conflict', types: ['index','option'] }
 *       slots disagreed. Caller MUST refuse to run (Sign 10 — never
 *       silently pick one).
 */
export function deriveAssetTypeFromSeriesMap(seriesMap) {
  if (!seriesMap || typeof seriesMap !== 'object') {
    return { ok: true, asset_type: null };
  }
  const seen = new Set();
  for (const entry of Object.values(seriesMap)) {
    if (!entry) continue;
    const t = inferAssetType(entry);
    if (t == null) continue;
    seen.add(t);
  }
  if (seen.size === 0) return { ok: true, asset_type: null };
  if (seen.size === 1) {
    const [only] = seen;
    return { ok: true, asset_type: only };
  }
  return {
    ok: false,
    reason: 'slot_conflict',
    types: Array.from(seen).sort(),
  };
}

/**
 * Pre-flight asset-type compatibility check for an indicator.
 *
 * Returns one of:
 *   - { ok: true }
 *       indicator has no compat declaration (treat as universally
 *       compatible — typically a user-authored custom) OR every slot
 *       agrees on a type that's in the indicator's compat list OR no
 *       slot was inferable (let the backend decide).
 *   - { ok: false, reason: 'slot_conflict', types: [...] }
 *       slots disagree on asset_type — never run.
 *   - { ok: false, reason: 'incompatible_asset',
 *       asset_type, accepted_asset_types }
 *       slots agree on a type but it's not in the compat list.
 *
 * ``compatibleAssetTypes`` semantics:
 *   * ``undefined`` / ``null`` → universally compatible (back-compat
 *     for user-authored indicators that never went through Wave 2a).
 *   * ``[]`` (empty array)    → universally compatible — same as null.
 *     We deliberately do NOT treat an empty array as "compatible with
 *     nothing" because that's a footgun for misconfigured registry
 *     entries and would silently disable the Run button forever.
 *   * non-empty array          → strict compat; asset_type must be in.
 */
export function computeAssetCompatibility(indicator) {
  if (!indicator) return { ok: true };
  const compat = indicator.compatibleAssetTypes;
  // No declared compat → allow (back-compat).
  if (compat == null || (Array.isArray(compat) && compat.length === 0)) {
    return { ok: true };
  }
  if (!Array.isArray(compat)) {
    // Defensive: log loudly via console.warn AND treat as universally
    // compatible rather than silently blocking. Sign 10 — never silent.
    // eslint-disable-next-line no-console
    console.warn('[runGate] indicator.compatibleAssetTypes is not an array', compat);
    return { ok: true };
  }
  const derived = deriveAssetTypeFromSeriesMap(indicator.seriesMap);
  if (!derived.ok) return derived; // slot_conflict
  if (derived.asset_type == null) {
    // Cannot classify any slot — defer to backend.
    return { ok: true };
  }
  if (compat.includes(derived.asset_type)) return { ok: true };
  return {
    ok: false,
    reason: 'incompatible_asset',
    asset_type: derived.asset_type,
    accepted_asset_types: compat.slice(),
  };
}

/**
 * Pre-flight sanity for option_stream series refs.
 *
 * Two FRONTEND-detectable failure modes:
 *
 *   1. Tautological selection: ``selection.kind === 'by_delta'`` paired
 *      with ``stream === 'delta'`` returns the target delta by
 *      construction — backend rejects with HTTP 422 +
 *      ``TAUTOLOGICAL_OPTION_STREAM``. We can pre-flight this purely
 *      from the seriesMap (Sign 6 — never fire a request the backend
 *      will deterministically reject).
 *
 *   2. Stream-unavailable-for-root requires the backend's
 *      ``OptionRootInfo.has_greeks`` metadata; the frontend does not
 *      hold it, so this code is detected only AFTER a request — see
 *      ``runGateForBackendError`` below.
 *
 * Returns:
 *   - { ok: true }
 *       no option_stream slot OR no tautological combo found.
 *   - { ok: false, reason: 'tautological_option_stream', label, stream }
 *       a slot is tautological — never run.
 */
export function computeOptionStreamSanity(indicator) {
  if (!indicator || !indicator.seriesMap || typeof indicator.seriesMap !== 'object') {
    return { ok: true };
  }
  for (const [label, ref] of Object.entries(indicator.seriesMap)) {
    if (!ref || ref.type !== 'option_stream') continue;
    const selectionKind = ref.selection && ref.selection.kind;
    if (selectionKind === 'by_delta' && ref.stream === 'delta') {
      return {
        ok: false,
        reason: 'tautological_option_stream',
        label,
        stream: ref.stream,
      };
    }
  }
  return { ok: true };
}

/**
 * Map a typed backend error envelope (from
 * ``utils/errorEnvelope.normalizeErrorEnvelope``) onto a Run-button
 * disabled reason.
 *
 * Used when a sticky error from the prior compute call is still
 * displayed and the user has not changed any input — the Run button
 * stays disabled with a typed tooltip rather than re-firing the same
 * deterministic failure (Sign 6 — no silent retries).
 *
 * Returns ``null`` when the error is not one this gate handles
 * (caller falls back to the legacy "Cannot run" tooltip).
 */
export function runGateForBackendError(error) {
  if (!error || typeof error !== 'object') return null;
  const code = typeof error.error_code === 'string' ? error.error_code : null;
  if (code === 'TAUTOLOGICAL_OPTION_STREAM') {
    return "Tautological selection: by_delta with stream='delta' returns the target delta by construction";
  }
  if (code === 'STREAM_UNAVAILABLE_FOR_ROOT') {
    const root = typeof error.root === 'string' && error.root ? error.root : 'this option root';
    const streams = Array.isArray(error.unavailable_streams) && error.unavailable_streams.length
      ? error.unavailable_streams.join(', ')
      : 'requested stream';
    return `Stream unavailable for ${root}: ${streams} not available on this option root`;
  }
  return null;
}

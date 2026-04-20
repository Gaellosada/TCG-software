// Pure utilities that derive an indicator's spec from its Python source.
//
// Two distinct responsibilities live here:
//   1. Parse the ``def compute(series, window: int = 20, ...):`` signature
//      to get the typed parameter list (name, type, default).
//   2. Scan the body for ``series['label']`` / ``series["label"]`` accesses
//      so the UI can render one slot per unique label.
//
// Both stay regex-level on purpose — the editor is plain text and a full
// Python parser is overkill. To avoid false positives from string
// literals / comments we first compute a mask of byte ranges that are
// inside comments or string literals and skip any match that starts
// inside a masked range.
//
// Supported param types: ``int``, ``float``, ``bool``. Defaults accept
// signed decimal + scientific notation (``1e3``, ``-2.5e-4``,
// ``1.5e+10``) and the bare literals ``True`` / ``False``. Anything else
// (no annotation, non-whitelisted type, non-literal default) is silently
// skipped — the backend will surface a proper error at Run time.

// Scientific notation friendly. Rejects bare leading ``.`` like ``.5``
// to stay consistent with the previous regex; that form is vanishingly
// rare in user signatures.
const NUMERIC_LITERAL_RE = /^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$|^[+-]?\.\d+(?:[eE][+-]?\d+)?$/;

function parseNumericDefault(raw) {
  const t = String(raw).trim();
  if (!NUMERIC_LITERAL_RE.test(t)) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** Mask ranges inside comments / string literals. */
function maskedRanges(code) {
  const ranges = [];
  const n = code.length;
  let i = 0;
  while (i < n) {
    const c = code[i];
    if ((c === '"' || c === "'") && code.slice(i, i + 3) === c.repeat(3)) {
      const quote = c.repeat(3);
      const start = i;
      const end = code.indexOf(quote, i + 3);
      if (end === -1) { ranges.push([start, n]); return ranges; }
      ranges.push([start, end + 3]);
      i = end + 3;
      continue;
    }
    if (c === '"' || c === "'") {
      const start = i;
      i += 1;
      while (i < n && code[i] !== c) {
        if (code[i] === '\\' && i + 1 < n) { i += 2; continue; }
        if (code[i] === '\n') break;
        i += 1;
      }
      if (i < n && code[i] === c) i += 1;
      ranges.push([start, i]);
      continue;
    }
    if (c === '#') {
      const start = i;
      while (i < n && code[i] !== '\n') i += 1;
      ranges.push([start, i]);
      continue;
    }
    i += 1;
  }
  return ranges;
}

function isInside(ranges, index) {
  for (const [a, b] of ranges) if (index >= a && index < b) return true;
  return false;
}

/**
 * Split a Python argument list (content between the outer parens) on
 * top-level commas — commas inside parens/brackets/braces are ignored.
 */
function splitTopLevelCommas(src) {
  const out = [];
  let depth = 0;
  let buf = '';
  let i = 0;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    if (c === '"' || c === "'") {
      // Consume full string literal.
      const triple = src.slice(i, i + 3) === c.repeat(3);
      if (triple) {
        const end = src.indexOf(c.repeat(3), i + 3);
        if (end === -1) { buf += src.slice(i); i = n; continue; }
        buf += src.slice(i, end + 3); i = end + 3; continue;
      }
      let j = i + 1;
      while (j < n && src[j] !== c) {
        if (src[j] === '\\' && j + 1 < n) { j += 2; continue; }
        j += 1;
      }
      if (j < n) j += 1;
      buf += src.slice(i, j);
      i = j;
      continue;
    }
    if (c === '(' || c === '[' || c === '{') { depth += 1; buf += c; i += 1; continue; }
    if (c === ')' || c === ']' || c === '}') { depth -= 1; buf += c; i += 1; continue; }
    if (c === ',' && depth === 0) {
      out.push(buf);
      buf = '';
      i += 1;
      continue;
    }
    buf += c;
    i += 1;
  }
  if (buf.trim() !== '' || out.length > 0) out.push(buf);
  return out;
}

/**
 * Locate the ``def compute(...)`` signature and return the text between
 * the outer parens, honouring nested parens/brackets and strings. Returns
 * null if no well-formed signature is found.
 */
function extractComputeArgs(code) {
  const ignore = maskedRanges(code);
  const re = /\bdef\s+compute\s*\(/g;
  let m;
  while ((m = re.exec(code)) !== null) {
    if (isInside(ignore, m.index)) continue;
    const openIdx = m.index + m[0].length - 1; // index of '('
    // Find the matching close paren.
    let depth = 1;
    let j = openIdx + 1;
    const n = code.length;
    while (j < n && depth > 0) {
      const c = code[j];
      if (c === '"' || c === "'") {
        const triple = code.slice(j, j + 3) === c.repeat(3);
        if (triple) {
          const end = code.indexOf(c.repeat(3), j + 3);
          if (end === -1) return null;
          j = end + 3; continue;
        }
        let k = j + 1;
        while (k < n && code[k] !== c) {
          if (code[k] === '\\' && k + 1 < n) { k += 2; continue; }
          if (code[k] === '\n') break;
          k += 1;
        }
        j = (k < n ? k + 1 : n);
        continue;
      }
      if (c === '#') {
        while (j < n && code[j] !== '\n') j += 1;
        continue;
      }
      if (c === '(' || c === '[' || c === '{') depth += 1;
      else if (c === ')' || c === ']' || c === '}') depth -= 1;
      if (depth === 0) return code.slice(openIdx + 1, j);
      j += 1;
    }
    return null;
  }
  return null;
}

function parseAnnotation(rawType) {
  const t = String(rawType).trim();
  if (t === 'int') return 'int';
  if (t === 'float') return 'float';
  if (t === 'bool') return 'bool';
  return null;
}

function parseTypedDefault(rawDefault, type) {
  const t = String(rawDefault).trim();
  if (type === 'bool') {
    if (t === 'True') return true;
    if (t === 'False') return false;
    return null;
  }
  const n = parseNumericDefault(t);
  if (n === null) return null;
  if (type === 'int') {
    // Accept integer-valued literals only.
    if (!Number.isInteger(n)) return null;
    return n;
  }
  return n; // float
}

function parseParamsFromSignature(argsSrc) {
  if (argsSrc === null) return [];
  const pieces = splitTopLevelCommas(argsSrc);
  const out = [];
  // First positional arg must be ``series`` — by convention — skip it
  // regardless of annotation / default.
  let first = true;
  for (const raw of pieces) {
    const piece = raw.trim();
    if (piece === '' || piece === '*' || piece === '/') { first = false; continue; }
    if (piece.startsWith('**') || piece.startsWith('*')) { first = false; continue; }
    if (first) { first = false; continue; }
    // Require shape: name : Type = default
    const eqIdx = piece.indexOf('=');
    if (eqIdx < 0) continue;
    const lhs = piece.slice(0, eqIdx).trim();
    const rhs = piece.slice(eqIdx + 1).trim();
    const colonIdx = lhs.indexOf(':');
    if (colonIdx < 0) continue;
    const name = lhs.slice(0, colonIdx).trim();
    const annot = lhs.slice(colonIdx + 1).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) continue;
    const type = parseAnnotation(annot);
    if (!type) {
      // Unsupported annotation — e.g. ``str``, ``list``, ``Optional[int]``.
      // Silent-skip is preserved at runtime (the backend will surface the
      // proper error at Run time) but we warn the author so typos and
      // unsupported types don't silently vanish from the UI.
      // eslint-disable-next-line no-console
      console.warn(
        `[paramParser] skipping parameter ${JSON.stringify(name)}: unsupported type ${JSON.stringify(annot)} (expected int | float | bool)`,
      );
      continue;
    }
    const def = parseTypedDefault(rhs, type);
    if (def === null) {
      // Non-literal default (e.g. ``abs(-1)``) or type/literal mismatch
      // (e.g. ``bool = 0``, ``int = 1.5``). Same silent-skip rationale as
      // above — warn so the author knows why the UI shows no control.
      // eslint-disable-next-line no-console
      console.warn(
        `[paramParser] skipping parameter ${JSON.stringify(name)}: non-literal or type-mismatched default ${JSON.stringify(rhs)} for annotation ${JSON.stringify(type)}`,
      );
      continue;
    }
    out.push({ name, type, default: def });
  }
  return out;
}

function parseSeriesLabels(code) {
  if (!code || typeof code !== 'string') return [];
  const ignore = maskedRanges(code);
  const re = /\bseries\s*\[\s*(['"])([A-Za-z_][A-Za-z0-9_]*)\1\s*\]/g;
  const seen = new Set();
  const out = [];
  let m;
  while ((m = re.exec(code)) !== null) {
    if (isInside(ignore, m.index)) continue;
    const label = m[2];
    if (!seen.has(label)) {
      seen.add(label);
      out.push(label);
    }
  }
  return out;
}

/**
 * Parse a full indicator source into its spec.
 *
 * Returns: { params: [{name, type, default}], seriesLabels: [string] }
 * — always an object, never throws. A malformed signature yields empty
 * params; a body with no ``series['...']`` accesses yields an empty
 * ``seriesLabels``.
 */
export function parseIndicatorSpec(code) {
  if (!code || typeof code !== 'string' || !code.trim()) {
    return { params: [], seriesLabels: [] };
  }
  let argsSrc = null;
  try { argsSrc = extractComputeArgs(code); } catch { argsSrc = null; }
  const params = parseParamsFromSignature(argsSrc);
  const seriesLabels = parseSeriesLabels(code);
  return { params, seriesLabels };
}

/**
 * Merge existing stored values with the freshly parsed typed params.
 *
 * - Preserves an existing value for a name still present IF its runtime
 *   type still matches the declared annotation (int/float → number,
 *   bool → boolean). ``int`` is flexible enough to accept any finite
 *   number; strict integer-ness is not enforced here since users are
 *   allowed to type e.g. 20.0 in a float box.
 * - Fills new names from the parsed default.
 * - Drops names not in the parsed spec.
 *
 * Output: ``{ [name]: number | boolean }``.
 */
export function reconcileParams(existing, parsedParams) {
  const src = existing && typeof existing === 'object' ? existing : {};
  const out = {};
  for (const p of parsedParams || []) {
    const prev = src[p.name];
    if (p.type === 'bool') {
      out[p.name] = (typeof prev === 'boolean') ? prev : !!p.default;
    } else {
      // int or float
      out[p.name] = (typeof prev === 'number' && Number.isFinite(prev))
        ? prev
        : (typeof p.default === 'number' && Number.isFinite(p.default) ? p.default : 0);
    }
  }
  return out;
}

/**
 * Merge existing ``seriesMap`` with the freshly parsed label list.
 *
 * - Preserves existing picks for labels still present.
 * - Drops labels no longer referenced.
 * - Adds new labels with a ``null`` slot (user must fill them in).
 *
 * Output: ``{ [label]: {collection, instrument_id} | null }``.
 */
export function reconcileSeriesMap(existing, labels) {
  const src = existing && typeof existing === 'object' ? existing : {};
  const out = {};
  for (const lbl of labels || []) {
    const prev = src[lbl];
    if (prev && typeof prev === 'object' && prev.collection && prev.instrument_id) {
      out[lbl] = { collection: prev.collection, instrument_id: prev.instrument_id };
    } else {
      out[lbl] = null;
    }
  }
  return out;
}

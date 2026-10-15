/** Formatting helpers. Values travel as decimal strings; we never coerce missing to 0. */
import type { MeasurementBasis, Modality, ContextMode, TimeExtent, TypedValue, MissingReason } from '../api/types';
import { formatNumber, separators } from './locale';

export const MISSING_REASON_LABEL: Record<MissingReason, string> = {
  not_collected: 'not collected',
  unavailable: 'unavailable',
  not_applicable: 'not applicable',
  suppressed: 'suppressed',
  source_error: 'source error',
  unmatched_semantics: 'unmatched semantics',
  outside_support: 'outside support',
  not_extracted: 'not extracted',
};

export function missingReasonLabel(reason: string | null | undefined): string {
  if (!reason) return 'reason not stated';
  return (MISSING_REASON_LABEL as Record<string, string>)[reason] ?? reason.replace(/_/g, ' ');
}

export const MODALITY_LABEL: Record<Modality, string> = {
  reported_observation: 'Reported observation',
  selected_estimate: 'Selected estimate',
  forecast: 'Forecast',
  identified_causal_estimate: 'Identified causal estimate',
  assumption_conditioned_scenario: 'Assumption-conditioned scenario',
  unsupported: 'Unsupported',
  metadata: 'Metadata',
  evaluation: 'Evaluation',
};

export const CONTEXT_MODE_LABEL: Record<ContextMode, string> = {
  evidence: 'Evidence',
  belief: 'Belief release',
  simulation: 'Simulation',
  evaluation: 'Evaluation',
  population: 'Synthetic population',
  registry: 'Registry metadata',
  operations: 'Operations',
};

export function modalityLabel(m: string | null | undefined): string {
  if (!m) return 'not stated';
  return (MODALITY_LABEL as Record<string, string>)[m] ?? m.replace(/_/g, ' ');
}

export function contextModeLabel(m: string | null | undefined): string {
  if (!m) return 'not stated';
  return (CONTEXT_MODE_LABEL as Record<string, string>)[m] ?? m;
}

/**
 * Group the integer part of a decimal string without float conversion (separators of the app
 * locale). Fractions longer than `maxFractionDigits` are rounded half-up for display; with
 * `padFraction`, a value that has a fraction is shown with exactly `maxFractionDigits` digits
 * (e.g. US$ to the cent: "57,082,396,439.60").
 */
export function formatDecimalString(value: string, maxFractionDigits = 6, padFraction = false): string {
  const trimmed = value.trim();
  const m = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(trimmed);
  if (!m) return trimmed;
  if (m[4] !== undefined) {
    // Exponent form: fall back to Number formatting only for display.
    const n = Number(trimmed);
    return Number.isFinite(n) ? formatNumber(n, { maximumSignificantDigits: 15 }) : trimmed;
  }
  let intPart = (m[2] || '0').replace(/^0+(?=\d)/, '');
  let frac = m[3] ?? '';
  if (frac.length > maxFractionDigits) {
    // Keep the carry: 0.9999999 -> "1", 1999.99999995 -> "2,000".
    const r = roundFraction(intPart, frac, maxFractionDigits);
    intPart = r.int.replace(/^0+(?=\d)/, '');
    frac = r.frac;
  }
  const { group, decimal } = separators();
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, group);
  const hadFraction = /[1-9]/.test(m[3] ?? '');
  frac = frac.replace(/0+$/, '');
  if (padFraction && hadFraction && maxFractionDigits > 0) frac = frac.padEnd(maxFractionDigits, '0');
  const isZero = /^0*$/.test(intPart) && !/[1-9]/.test(frac);
  const sign = m[1] === '-' && !isZero ? '−' : '';
  return `${sign}${grouped}${frac ? `${decimal}${frac}` : ''}`;
}

/**
 * Display precision per unit. Simulation outputs and estimates travel as exact decimal strings
 * with many digits (e.g. "57082396439.600717"); printing all of them implies a precision the
 * inputs do not have. US$ are shown to the cent and percentages to 4 decimals; everything else
 * keeps up to 6. The exact string is never altered: it stays available on hover/expand and is
 * what CSV/JSON exports carry.
 */
export const DISPLAY_FRACTION_DIGITS: Record<string, { digits: number; pad: boolean }> = {
  'unit.usd': { digits: 2, pad: true },
  'unit.percent': { digits: 4, pad: false },
  'unit.percent_of_baseline_gdp': { digits: 4, pad: false },
};

export interface DisplayDecimal {
  /** Display text (rounded for display, locale separators). */
  text: string;
  /** The exact decimal string as served. */
  exact: string;
  /** Whether display rounding dropped digits (so the exact value differs from the text). */
  rounded: boolean;
}

/** Display form of an exact decimal string for a given unit; see DISPLAY_FRACTION_DIGITS. */
export function displayDecimal(value: string, unitRef?: string | null): DisplayDecimal {
  const p = (unitRef && DISPLAY_FRACTION_DIGITS[unitRef]) || { digits: 6, pad: false };
  const exact = String(value).trim();
  const m = /^[+-]?\d*(?:\.(\d*))?$/.exec(exact);
  const significantFrac = m ? (m[1] ?? '').replace(/0+$/, '') : '';
  return {
    text: formatDecimalString(exact, p.digits, p.pad),
    exact,
    rounded: !!m && significantFrac.length > p.digits,
  };
}

function roundFraction(intPart: string, frac: string, digits: number): { int: string; frac: string } {
  // Display-only rounding (half up) of a long fraction; the exact string remains available via title.
  const keep = frac.slice(0, digits);
  const next = frac.charCodeAt(digits) - 48;
  if (next < 5) return { int: intPart, frac: keep };
  const digitsArr = (intPart + keep).split('').map(Number);
  for (let i = digitsArr.length - 1; i >= 0; i--) {
    if (digitsArr[i]! < 9) {
      digitsArr[i] = digitsArr[i]! + 1;
      break;
    }
    digitsArr[i] = 0;
    if (i === 0) digitsArr.unshift(1);
  }
  const s = digitsArr.join('');
  return { int: s.slice(0, s.length - keep.length), frac: s.slice(s.length - keep.length) };
}

/** A compact magnitude label (e.g. "68.2 billion") for headline display. */
export function compactMagnitude(value: string): string | null {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  const abs = Math.abs(n);
  const scales: [number, string][] = [
    [1e12, 'trillion'],
    [1e9, 'billion'],
    [1e6, 'million'],
  ];
  for (const [s, label] of scales) {
    if (abs >= s) return `${formatNumber(n / s, { maximumFractionDigits: 2 })} ${label}`;
  }
  return null;
}

const UNIT_LABEL: Record<string, string> = {
  'unit.usd': 'US$',
  'unit.persons': 'persons',
  'unit.percent': '%',
  'unit.percent_of_baseline_gdp': '% of baseline GDP',
  'unit.ratio': 'ratio',
  'unit.kg': 'kg',
  'unit.count': 'count',
};

export function unitLabel(unitRef: string | null | undefined): string {
  if (!unitRef) return '';
  return UNIT_LABEL[unitRef] ?? unitRef.replace(/^unit\./, '');
}

export function basisLabel(basis: MeasurementBasis | null | undefined): string {
  if (!basis) return '';
  const parts: string[] = [];
  if (basis.price_basis) parts.push(basis.price_basis === 'nominal' ? 'current prices' : basis.price_basis);
  if (basis.price_reference_year) parts.push(`ref. year ${basis.price_reference_year}`);
  if (basis.valuation) parts.push(basis.valuation);
  if (basis.scale_factor && basis.scale_factor !== '1') parts.push(`× ${basis.scale_factor}`);
  return parts.join(', ');
}

/** Plain-text display rendering of a typed value (aria labels, graph labels). Exports use valueNumericString. */
export function valueToText(v: TypedValue | null | undefined): string {
  if (!v) return 'missing (not provided by API)';
  switch (v.kind) {
    case 'missing':
      return `missing (${missingReasonLabel(String(v.reason))})`;
    case 'decimal':
      return `${displayDecimal(String(v.value), String(v.unit_ref)).text} ${unitLabel(String(v.unit_ref))}`.trim();
    case 'float':
      return `${formatNumber(Number(v.value), { maximumSignificantDigits: 15 })} ${unitLabel(String(v.unit_ref))}`.trim();
    case 'category':
      return String(v.code ?? '');
    case 'text':
      return String(v.text ?? 'text artifact');
    default:
      return `${v.kind} value`;
  }
}

/** Raw numeric string for exports; missing stays empty with a separate reason column. */
export function valueNumericString(v: TypedValue | null | undefined): string | null {
  if (!v) return null;
  if (v.kind === 'decimal') return String(v.value);
  if (v.kind === 'float') return String(v.value);
  return null;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return 'not stated';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, 'Z').replace(/:00Z$/, 'Z')}`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return 'not stated';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().slice(0, 10);
}

export function validTimeLabel(vt: TimeExtent | Record<string, unknown> | string | null | undefined): string {
  if (!vt) return 'not stated';
  if (typeof vt === 'string') return vt;
  if (vt.label) return String(vt.label);
  const start = vt.start ? String(vt.start) : '';
  const end = vt.end ? String(vt.end) : '';
  if (start && end) return `[${start}, ${end})`;
  if (start) return `from ${start}`;
  if (end) return `until ${end}`;
  return 'not stated';
}

/** Short opaque id for display; the full id stays in title/aria. */
export function shortId(id: string | null | undefined): string {
  if (!id) return '—';
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

export function humanize(s: string | null | undefined): string {
  if (!s) return '';
  return s.replace(/[._]/g, ' ');
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "point:07-01 (midyear estimate)" -> "1 Jul point (midyear estimate)"; "annual_total" -> "annual total (interval)". */
export function temporalSupportLabel(ts: string | null | undefined): string {
  if (!ts) return 'not stated';
  const m = /^point:(\d{2})-(\d{2})(.*)$/.exec(ts);
  if (m) {
    const month = MONTHS[Number(m[1]) - 1] ?? m[1];
    const day = Number(m[2]);
    return `${day} ${month} point${m[3] ? m[3] : ''}`.trim();
  }
  if (ts === 'annual_total') return 'annual total (interval)';
  return humanize(ts);
}

export function isRecord(x: unknown): x is Record<string, unknown> {
  return !!x && typeof x === 'object' && !Array.isArray(x);
}

export function asArray<T = unknown>(x: unknown): T[] {
  if (Array.isArray(x)) return x as T[];
  if (isRecord(x)) {
    for (const k of ['items', 'rows', 'results', 'data']) {
      if (Array.isArray(x[k])) return x[k] as T[];
    }
  }
  return [];
}

export function str(x: unknown, fallback = ''): string {
  if (x === null || x === undefined) return fallback;
  if (typeof x === 'string') return x;
  if (typeof x === 'number' || typeof x === 'boolean') return String(x);
  return JSON.stringify(x);
}

/** datetime-local value (interpreted as UTC) -> ISO-8601 with Z. */
export function localUtcToIso(v: string): string | undefined {
  if (!v) return undefined;
  const d = new Date(/T\d{2}:\d{2}$/.test(v) ? `${v}:00Z` : `${v}Z`);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

/** Concepts that measure a quantity or net weight (where quantity-estimation flags apply). */
export function isQuantityConcept(concept: string | null | undefined): boolean {
  return !!concept && /(net_weight|quantity|\bqty\b|\.weight)/i.test(concept);
}

const LEGACY_FLAG_NOTE = 'legacy flag (meaning: quantity/net weight estimated)';

/**
 * Short, human-readable notes for provider source flags that change how a value must be read
 * (e.g. UN Comtrade `isReported=false`, WDI `obs_status`). A flag that is absent is not reported
 * as anything.
 *
 * Comtrade estimation (connector 1.2.0): only `provider_value_estimated === true` labels a value
 * as a provider estimate (`null` means undeterminable and says nothing). `provider_quantity_estimated`
 * concerns quantity / net weight and is shown only on quantity/weight concepts. The 1.1.0 key
 * `provider_estimation_flagged` (legacyEstimationFlag != 0) codes estimated quantity/net weight,
 * never the USD value: on old rows it is shown as a legacy flag with that meaning, never as a
 * value estimate. An explicit API `note` is used verbatim unless it is an old note that read the
 * legacy flag as a value estimate.
 */
export function sourceFlagNotes(flags: Record<string, unknown> | null | undefined, opts: { concept?: string | null } = {}): string[] {
  if (!flags || typeof flags !== 'object') return [];
  const out: string[] = [];
  const concept = opts.concept ?? (typeof flags.concept === 'string' ? flags.concept : null);
  const valueEstimated = flags.provider_value_estimated === true;
  const legacy = flags.provider_estimation_flagged === true;
  const lef = flags.legacyEstimationFlag;
  const lefText = lef !== undefined && lef !== null && lef !== '' ? `legacyEstimationFlag ${String(lef)}` : '';
  if (typeof flags.note === 'string' && flags.note.trim()) {
    const note = flags.note.trim();
    const legacyValueClaim = /estimat/i.test(note) && !valueEstimated && (legacy || /legacyEstimationFlag/i.test(note));
    if (!legacyValueClaim) out.push(note);
  }
  if (flags.provider_returned_no_rows === true) out.push('provider returned no rows for this key');
  if (valueEstimated && !out.some((n) => /estimat/i.test(n))) out.push('provider estimate (value marked estimated by the provider)');
  if (isQuantityConcept(concept) && flags.provider_quantity_estimated === true) {
    out.push(`provider estimate of quantity/net weight${lefText ? ` (${lefText})` : ''}`);
  }
  if (legacy && flags.provider_quantity_estimated === undefined && flags.provider_value_estimated === undefined) {
    out.push(`${LEGACY_FLAG_NOTE}${lefText ? `, ${lefText}` : ''}`);
  }
  if (flags.legacy_estimation_code_unrecognised === true) out.push(`unrecognised provider estimation code${lefText ? ` (${lefText})` : ''}`);
  if (flags.isReported === false && !valueEstimated && !out.some((n) => /reported/i.test(n))) {
    out.push('not directly reported (isReported=false)');
  }
  if (typeof flags.obs_status === 'string' && flags.obs_status.trim()) out.push(`provider status: ${flags.obs_status.trim()}`);
  return out;
}

/**
 * Exact comparison of two decimal strings (no float rounding): -1, 0 or 1, or null if either is
 * not a plain decimal. Used to place a point (e.g. the L1 value) relative to a range's ends.
 */
export function compareDecimalStrings(a: string, b: string): -1 | 0 | 1 | null {
  const parse = (s: string) => {
    const m = /^([+-]?)(\d*)(?:\.(\d*))?$/.exec(s.trim().replace(/^−/, '-'));
    if (!m || (m[2] === '' && (m[3] ?? '') === '')) return null;
    const int = (m[2] ?? '').replace(/^0+/, '');
    const frac = (m[3] ?? '').replace(/0+$/, '');
    const zero = int === '' && frac === '';
    return { neg: m[1] === '-' && !zero, int, frac };
  };
  const x = parse(a);
  const y = parse(b);
  if (!x || !y) return null;
  if (x.neg !== y.neg) return x.neg ? -1 : 1;
  const sign = x.neg ? -1 : 1;
  let mag: number;
  if (x.int.length !== y.int.length) mag = x.int.length > y.int.length ? 1 : -1;
  else if (x.int !== y.int) mag = x.int > y.int ? 1 : -1;
  else {
    const n = Math.max(x.frac.length, y.frac.length);
    const fx = x.frac.padEnd(n, '0');
    const fy = y.frac.padEnd(n, '0');
    mag = fx === fy ? 0 : fx > fy ? 1 : -1;
  }
  return (mag * sign) as -1 | 0 | 1;
}

/**
 * Where a point value sits relative to a range: used to say "L1 = upper end under this grid"
 * instead of drawing an interval around L1 (the L2 range over an assumed elasticity grid is
 * often one-sided by construction).
 */
export function pointInRange(point: string, lower: string, upper: string): 'lower_end' | 'upper_end' | 'degenerate' | 'inside' | 'outside' | null {
  const lo = compareDecimalStrings(point, lower);
  const hi = compareDecimalStrings(point, upper);
  if (lo === null || hi === null) return null;
  if (lo === 0 && hi === 0) return 'degenerate';
  if (hi === 0) return 'upper_end';
  if (lo === 0) return 'lower_end';
  if (lo > 0 && hi < 0) return 'inside';
  return 'outside';
}

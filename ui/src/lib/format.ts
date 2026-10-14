/** Formatting helpers. Values travel as decimal strings; we never coerce missing to 0. */
import type { MeasurementBasis, Modality, ContextMode, TimeExtent, TypedValue, MissingReason } from '../api/types';

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

/** Group the integer part of a decimal string without float conversion. */
export function formatDecimalString(value: string, maxFractionDigits = 6): string {
  const trimmed = value.trim();
  const m = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(trimmed);
  if (!m) return trimmed;
  if (m[4] !== undefined) {
    // Exponent form: fall back to Number formatting only for display.
    const n = Number(trimmed);
    return Number.isFinite(n) ? n.toLocaleString('en-US', { maximumSignificantDigits: 15 }) : trimmed;
  }
  let intPart = (m[2] || '0').replace(/^0+(?=\d)/, '');
  let frac = m[3] ?? '';
  if (frac.length > maxFractionDigits) {
    // Keep the carry: 0.9999999 -> "1", 1999.99999995 -> "2,000".
    const r = roundFraction(intPart, frac, maxFractionDigits);
    intPart = r.int.replace(/^0+(?=\d)/, '');
    frac = r.frac;
  }
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  frac = frac.replace(/0+$/, '');
  const isZero = /^0*$/.test(intPart) && !frac;
  const sign = m[1] === '-' && !isZero ? '−' : '';
  return `${sign}${grouped}${frac ? `.${frac}` : ''}`;
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
    if (abs >= s) return `${(n / s).toLocaleString('en-US', { maximumFractionDigits: 2 })} ${label}`;
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

/** Plain-text rendering of a typed value (used in exports and aria labels). */
export function valueToText(v: TypedValue | null | undefined): string {
  if (!v) return 'missing (not provided by API)';
  switch (v.kind) {
    case 'missing':
      return `missing (${missingReasonLabel(String(v.reason))})`;
    case 'decimal':
      return `${formatDecimalString(String(v.value))} ${unitLabel(String(v.unit_ref))}`.trim();
    case 'float':
      return `${Number(v.value).toLocaleString('en-US', { maximumSignificantDigits: 15 })} ${unitLabel(String(v.unit_ref))}`.trim();
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

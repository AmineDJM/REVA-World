/**
 * Pure helpers for the world exposure map, partner arcs and the mirror view.
 *
 * Honesty rules encoded here (tested in worldmap.test.ts):
 * - a country is coloured only from a result row of the selected run; a country the run
 *   could not compute is "hatched" with its typed reason, never coloured and never zero;
 * - arcs are drawn only for reported bilateral flows between two mapped countries; the
 *   partner "World" is never an arc, and composite/aggregate partners (e.g. "Areas, nes")
 *   are listed, never mapped onto a polygon;
 * - mirror rows are what partners report, kept separate from the country's own data.
 */
import type { GeoFeature, Relation, SimResult, SimulationStatus } from '../api/types';
import { isRecord } from './format';

export const EXPOSURE_OUTPUT = 'exposure_percent_baseline_gdp';

// ------------------------------------------------------------------ run country status

export type CountryRunState = 'computed' | 'missing_input' | 'skipped' | 'failed' | 'not_reported';

export interface CountryRunStatus {
  iso3: string;
  state: CountryRunState;
  /** Raw status string from the API (e.g. "ready", "missing_input", "skipped"). */
  apiStatus: string | null;
  /** Typed reasons, one line each (input, year, state). */
  reasons: string[];
}

/** One readable line for a problem item from the API ({input, concept, year, state|reason}). */
export function problemText(p: unknown): string {
  if (typeof p === 'string') return p;
  if (!isRecord(p)) return String(p);
  const what = [p.input, p.concept].filter((x) => typeof x === 'string' && x).join(' · ');
  const year = p.year ?? p.reference_year;
  const state = p.state ?? p.reason ?? p.code ?? p.message ?? p.detail;
  const parts = [what || null, year !== undefined && year !== null ? String(year) : null].filter(Boolean).join(' ');
  const st = typeof state === 'string' ? state.replace(/^missing:/, 'missing: ') : state !== undefined ? JSON.stringify(state) : '';
  return [parts, st].filter(Boolean).join(' — ') || JSON.stringify(p);
}

function stateOf(apiStatus: string | null, hasResult: boolean): CountryRunState {
  if (hasResult) return 'computed';
  switch (apiStatus) {
    case 'missing_input':
    case 'missing':
      return 'missing_input';
    case 'skipped':
    case 'skipped_missing':
      return 'skipped';
    case 'failed':
    case 'error':
    case 'unsupported':
      return 'failed';
    default:
      return 'not_reported';
  }
}

/**
 * Per-country state of a run: from `outcome.applicability.countries` (per-country status and
 * problems), `outcome.applicability.skipped_countries` (skip_missing_countries policy) and the
 * scenario's country list. Countries with a result row are "computed".
 */
export function runCountryStatuses(st: SimulationStatus | null | undefined): Record<string, CountryRunStatus> {
  const out: Record<string, CountryRunStatus> = {};
  if (!st) return out;
  const withResult = new Set<string>();
  for (const r of st.results) {
    const iso = typeof r.applicability?.country === 'string' ? r.applicability.country : null;
    if (iso) withResult.add(iso);
  }
  const app = isRecord(st.outcome?.applicability) ? st.outcome!.applicability : {};
  const countries = isRecord(app.countries) ? app.countries : {};
  for (const [iso, v] of Object.entries(countries)) {
    const apiStatus = isRecord(v) && typeof v.status === 'string' ? v.status : null;
    const problems = isRecord(v) && Array.isArray(v.problems) ? v.problems : [];
    out[iso] = { iso3: iso, apiStatus, state: stateOf(apiStatus, withResult.has(iso)), reasons: problems.map(problemText) };
  }
  const skipped = Array.isArray(app.skipped_countries) ? app.skipped_countries : [];
  for (const s of skipped) {
    const iso = typeof s === 'string' ? s : isRecord(s) && typeof s.country === 'string' ? s.country : null;
    if (!iso) continue;
    const reasons = isRecord(s)
      ? Array.isArray(s.problems)
        ? s.problems.map(problemText)
        : Array.isArray(s.missing)
          ? s.missing.map(problemText)
          : [problemText(s.reason ?? s)]
      : [];
    const prev = out[iso];
    out[iso] = { iso3: iso, apiStatus: prev?.apiStatus ?? 'skipped', state: withResult.has(iso) ? 'computed' : 'skipped', reasons: prev?.reasons.length ? prev.reasons : reasons };
  }
  const specCountries = Array.isArray(st.scenario?.spec?.countries) ? (st.scenario!.spec.countries as unknown[]) : [];
  for (const c of specCountries) {
    if (typeof c !== 'string' || out[c]) continue;
    out[c] = { iso3: c, apiStatus: null, state: withResult.has(c) ? 'computed' : 'not_reported', reasons: [] };
  }
  for (const iso of withResult) {
    if (!out[iso]) out[iso] = { iso3: iso, apiStatus: null, state: 'computed', reasons: [] };
  }
  return out;
}

// ------------------------------------------------------------------ choropleth values

export interface ExposureRow {
  entityId: string;
  label: string;
  iso3: string | null;
  /** Exact decimal string served by the API. */
  value: string;
  unitRef: string;
  result: SimResult;
}

/** The L1 exposure rows of a run (one per country with a decimal result). */
export function exposureRows(results: SimResult[], output = EXPOSURE_OUTPUT, variant = 'L1'): ExposureRow[] {
  const out: ExposureRow[] = [];
  for (const r of results) {
    if (r.output_key !== output || r.variant !== variant || !r.subject) continue;
    if (r.value.kind !== 'decimal') continue;
    out.push({
      entityId: r.subject.id,
      label: r.subject.label,
      iso3: typeof r.applicability?.country === 'string' ? r.applicability.country : null,
      value: r.value.value,
      unitRef: r.value.unit_ref,
      result: r,
    });
  }
  return out.sort((a, b) => Number(b.value) - Number(a.value) || a.label.localeCompare(b.label));
}

// ------------------------------------------------------------------ bilateral flows

export type PartnerClass = 'country' | 'world' | 'composite';

function participant(rel: Relation, role: string) {
  return rel.participants.find((p) => p.role === role) ?? null;
}

/** Comtrade partner code of a bilateral support key like "COMTRADE:251->398". */
export function partnerCode(rel: Relation): string | null {
  const key = isRecord(rel.support?.spatial) ? rel.support.spatial.key : null;
  if (typeof key !== 'string') return null;
  const m = /->(\w+)$/.exec(key);
  return m ? m[1]! : null;
}

export function classifyPartner(rel: Relation, partnerKind: string, partnerLabel: string): PartnerClass {
  if (partnerCode(rel) === '0' || /^world$/i.test(partnerLabel.trim())) return 'world';
  if (partnerKind !== 'entity.country') return 'composite';
  return 'country';
}

export interface FlowRow {
  relationId: string;
  observationId: string | null;
  reporterId: string;
  reporterLabel: string;
  partnerId: string;
  partnerLabel: string;
  partnerKind: string;
  partnerClass: PartnerClass;
  /** Reporter's flow: M = reporter imports from partner, X = reporter exports to partner. */
  flow: string;
  valuation: string | null;
  period: string | null;
  /** Decimal string, or null when the provider row was a typed missing. */
  value: string | null;
  missingReason: string | null;
  origin: string | null;
}

function toFlow(rel: Relation): FlowRow | null {
  const rep = participant(rel, 'role.reporter');
  const par = participant(rel, 'role.partner');
  if (!rep || !par) return null;
  const v = rel.value;
  const flow = typeof rel.component?.flow === 'string' ? rel.component.flow : '?';
  return {
    relationId: rel.id,
    observationId: rel.evidence_observation_id ?? null,
    reporterId: rep.entity_id,
    reporterLabel: rep.entity_label,
    partnerId: par.entity_id,
    partnerLabel: par.entity_label,
    partnerKind: par.entity_kind,
    partnerClass: classifyPartner(rel, par.entity_kind, par.entity_label),
    flow,
    valuation: typeof rel.component?.valuation === 'string' ? rel.component.valuation : null,
    period: rel.support?.period_label ?? null,
    value: v && v.kind === 'decimal' ? v.value : null,
    missingReason: v && v.kind === 'missing' ? String(v.reason) : v ? null : 'not_reported',
    origin: rel.origin ?? null,
  };
}

export interface PartnerFlows {
  /** Partner countries (candidates for arcs; drawn only when both ends have display geometry). */
  countries: FlowRow[];
  /** Composite/aggregate partner areas: listed, never mapped. */
  composites: FlowRow[];
  /** Rows whose partner is "World" (should not exist as relations; never drawn). */
  world: FlowRow[];
}

/** Flows the selected country reports (it is the reporter), for one flow direction. */
export function reportedPartnerFlows(relations: Relation[], entityId: string, flow: string): PartnerFlows {
  const out: PartnerFlows = { countries: [], composites: [], world: [] };
  for (const rel of relations) {
    if (rel.predicate !== 'predicate.trade_flow') continue;
    const f = toFlow(rel);
    if (!f || f.reporterId !== entityId || f.flow !== flow) continue;
    if (f.partnerClass === 'world') out.world.push(f);
    else if (f.partnerClass === 'composite') out.composites.push(f);
    else out.countries.push(f);
  }
  const byValue = (a: FlowRow, b: FlowRow) => Number(b.value ?? -Infinity) - Number(a.value ?? -Infinity) || a.partnerLabel.localeCompare(b.partnerLabel);
  out.countries.sort(byValue);
  out.composites.sort(byValue);
  return out;
}

/**
 * Mirror rows: what other reporters say about trade with `entityId` (it is the partner).
 * A partner's import from the country is the partner's figure, not the country's export:
 * valuation (CIF vs FOB), timing and coverage differ, so the rows are never summed into or
 * substituted for the country's own data.
 */
export function mirrorFlows(relations: Relation[], entityId: string): FlowRow[] {
  const out: FlowRow[] = [];
  for (const rel of relations) {
    if (rel.predicate !== 'predicate.trade_flow') continue;
    const f = toFlow(rel);
    if (!f || f.partnerId !== entityId || f.reporterId === entityId) continue;
    out.push(f);
  }
  return out.sort(
    (a, b) =>
      String(b.period ?? '').localeCompare(String(a.period ?? '')) ||
      Number(b.value ?? -Infinity) - Number(a.value ?? -Infinity) ||
      a.reporterLabel.localeCompare(b.reporterLabel),
  );
}

// ------------------------------------------------------------------ geometry

type Ring = [number, number][];

function polygonsOf(geometry: Record<string, unknown>): Ring[][] {
  const c = geometry.coordinates;
  if (geometry.type === 'Polygon' && Array.isArray(c)) return [c as Ring[]];
  if (geometry.type === 'MultiPolygon' && Array.isArray(c)) return c as Ring[][];
  return [];
}

function ringArea(r: Ring): number {
  let a = 0;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) a += r[j]![0] * r[i]![1] - r[i]![0] * r[j]![1];
  return a / 2;
}

/** Planar centroid of the largest outer ring (good enough for an arc anchor; display only). */
export function featureAnchor(geometry: Record<string, unknown>): [number, number] | null {
  let best: Ring | null = null;
  let bestArea = 0;
  for (const poly of polygonsOf(geometry)) {
    const outer = poly[0];
    if (!outer || outer.length < 3) continue;
    const a = Math.abs(ringArea(outer));
    if (a > bestArea) {
      bestArea = a;
      best = outer;
    }
  }
  if (!best) return null;
  const a = ringArea(best);
  if (a === 0) return best[0] ?? null;
  let cx = 0;
  let cy = 0;
  for (let i = 0, j = best.length - 1; i < best.length; j = i++) {
    const f = best[j]![0] * best[i]![1] - best[i]![0] * best[j]![1];
    cx += (best[j]![0] + best[i]![0]) * f;
    cy += (best[j]![1] + best[i]![1]) * f;
  }
  return [cx / (6 * a), cy / (6 * a)];
}

/** Entity id -> anchor point, from reviewed display features only. */
export function anchorsByEntity(features: GeoFeature[]): Map<string, [number, number]> {
  const out = new Map<string, [number, number]>();
  const areas = new Map<string, number>();
  for (const f of features) {
    const id = f.properties.entity_id;
    if (!id || f.properties.match_status === 'unmatched') continue;
    const anchor = featureAnchor(f.geometry);
    if (!anchor) continue;
    const area = polygonsOf(f.geometry).reduce((s, p) => s + Math.abs(ringArea(p[0] ?? [])), 0);
    if (area > (areas.get(id) ?? -1)) {
      out.set(id, anchor);
      areas.set(id, area);
    }
  }
  return out;
}

const rad = (d: number) => (d * Math.PI) / 180;
const deg = (r: number) => (r * 180) / Math.PI;

/** Great-circle arc from a to b as lon/lat points, longitudes unwrapped to stay continuous. */
export function greatCircle(a: [number, number], b: [number, number], n = 48): [number, number][] {
  const [l1, p1] = [rad(a[0]), rad(a[1])];
  const [l2, p2] = [rad(b[0]), rad(b[1])];
  const d = 2 * Math.asin(Math.sqrt(Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin((l2 - l1) / 2) ** 2));
  if (d === 0 || !Number.isFinite(d)) return [a, b];
  const pts: [number, number][] = [];
  let prevLon: number | null = null;
  for (let i = 0; i <= n; i++) {
    const f = i / n;
    const A = Math.sin((1 - f) * d) / Math.sin(d);
    const B = Math.sin(f * d) / Math.sin(d);
    const x = A * Math.cos(p1) * Math.cos(l1) + B * Math.cos(p2) * Math.cos(l2);
    const y = A * Math.cos(p1) * Math.sin(l1) + B * Math.cos(p2) * Math.sin(l2);
    const z = A * Math.sin(p1) + B * Math.sin(p2);
    let lon = deg(Math.atan2(y, x));
    const lat = deg(Math.atan2(z, Math.sqrt(x * x + y * y)));
    if (prevLon !== null) {
      while (lon - prevLon > 180) lon -= 360;
      while (lon - prevLon < -180) lon += 360;
    }
    prevLon = lon;
    pts.push([lon, lat]);
  }
  return pts;
}

/** Arc width class 1..4 by share of the largest drawn flow (sqrt scale; display only). */
export function arcWidth(value: string | null, max: number): number {
  const v = Number(value);
  if (!Number.isFinite(v) || v <= 0 || max <= 0) return 1;
  return 1 + Math.round(3 * Math.sqrt(v / max));
}

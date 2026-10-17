/**
 * Client of /v1/v8/series/* (src/reva/api/routes/v8_series.py), shared by Monde and Comparer.
 *
 * Values stay exact decimal strings (`v`), never JSON numbers: a filled zero cannot sneak in. A place without a
 * value is a typed `missing` entry with its reason. Responses are validated with zod (tolerant on extra fields,
 * strict on the honesty-critical ones).
 */
import { z } from 'zod';
import { ApiWarning, DecimalString, memo, v3get } from '../../../v3/api/common';
import type { Query } from '../../../api/http';

const BASE = '/v1/v8/series';
const nstr = z.string().nullish().transform((v) => v ?? null);
const warnings = z.array(ApiWarning).nullish().transform((w) => w ?? []);

export const Rights = z.looseObject({
  source: z.string().nullish(),
  licence: z.string(),
  licence_url: nstr,
  attribution: nstr,
  commercial_use: z.string().default('unknown'),
  non_commercial: z.boolean().default(false),
  share_alike: z.boolean().default(false),
  no_derivatives: z.boolean().default(false),
  open_licence: z.boolean().nullish().transform((v) => v ?? null),
  restricted: z.boolean().default(false),
  basis: z.string().default('table_artifact'),
  restriction: nstr,
  required_notice: nstr,
});
export type Rights = z.output<typeof Rights>;

export const Snapshot = z.looseObject({ snapshot_sha256: z.string(), origin: z.string(), knowledge_cutoff: z.string() });

export const Point = z.looseObject({
  p: z.string(),
  ps: z.string(),
  pe: z.string().nullish(),
  basis: nstr,
  v: DecimalString.nullable(),
  t: nstr,
  unit: nstr,
  proj: z.boolean().default(false),
  miss: nstr,
  flags: z.record(z.string(), z.unknown()).default({}),
  geo: nstr,
  iso3: nstr,
  series: nstr,
  raw: nstr,
  table_sha: nstr,
  lic: z.number().int().nullish().transform((v) => v ?? null),
});
export type Point = z.output<typeof Point>;

const Transform = z.looseObject({ ok: z.boolean(), reason: nstr });
export type Transform = z.output<typeof Transform>;

export const SeriesMeta = z.looseObject({
  series: z.string(),
  table: z.string(),
  source: z.string(),
  dataset: z.string(),
  indicator: z.string(),
  measure: z.string(),
  filters: z.array(z.looseObject({ key: z.string(), value: z.string() })).default([]),
  concept: z
    .looseObject({ id: z.string(), labels: z.record(z.string(), z.string()).default({}), definition: z.string().default(''), unit: z.string().default(''), theme: nstr })
    .nullish()
    .transform((v) => v ?? null),
  label: nstr,
  unit: nstr,
  units: z.array(z.looseObject({ unit: z.string(), cells: z.number() })).default([]),
  value_kind: z.string(),
  min_value: z.number().nullish().transform((v) => v ?? null),
  precisions: z.record(z.string(), z.number()).default({}),
  default_precision: nstr,
  countries: z.array(z.string()).default([]),
  levels: z.looseObject({ adm0: z.number(), adm1: z.number() }),
  aggregates: z.array(z.string()).default([]),
  recorded: nstr,
  licences: z.array(Rights).default([]),
  transforms: z.record(z.string(), Transform).default({}),
});
export type SeriesMeta = z.output<typeof SeriesMeta>;

export const PeriodInfo = z.looseObject({ code: z.string(), label: z.string(), obs: z.number(), proj: z.number() });
export type PeriodInfo = z.output<typeof PeriodInfo>;

export const MapData = z.looseObject({
  meta: SeriesMeta,
  precision: z.string(),
  periods: z.array(PeriodInfo),
  period: nstr,
  period_label: nstr,
  level: z.string(),
  projections: z.string(),
  values: z.array(Point),
  missing: z.array(z.looseObject({ geo: z.string(), reason: z.string() })),
  missing_labels: z.record(z.string(), z.string()).default({}),
  aggregates: z.array(Point).default([]),
  licences: z.array(Rights).default([]),
  scale_hint: z.string().default('quantile'),
  counts: z.record(z.string(), z.number()).default({}),
  names: z.record(z.string(), z.string()).default({}),
});
export type MapData = z.output<typeof MapData>;

export const MapResponse = z.looseObject({ data: MapData, snapshot: Snapshot, notes: z.array(z.string()).default([]), warnings, took_ms: z.number().nullish() });
export type MapResponse = z.output<typeof MapResponse>;

export const PlaceResponse = z.looseObject({
  data: z.looseObject({ meta: SeriesMeta, geo: z.string(), name: nstr, precision: z.string(), points: z.array(Point), licences: z.array(Rights).default([]) }),
  snapshot: Snapshot,
  warnings,
});
export type PlaceResponse = z.output<typeof PlaceResponse>;

export const CatalogHit = z.looseObject({
  kind: z.string(),
  id: z.string(),
  label: z.string(),
  sublabel: nstr,
  table: nstr,
  measure: nstr,
  filters: z.array(z.looseObject({ key: z.string(), value: z.string() })).default([]),
  series: nstr,
  concept: nstr,
  unit: nstr,
  theme: nstr,
  in_snapshot: z.boolean().default(true),
});
export type CatalogHit = z.output<typeof CatalogHit>;

export const CatalogResponse = z.looseObject({
  query: z.string(),
  hits: z.array(CatalogHit),
  concepts_total: z.number(),
  themes: z.record(z.string(), z.unknown()).default({}),
  took_ms: z.number().nullish(),
});
export type CatalogResponse = z.output<typeof CatalogResponse>;

export const TableResponse = z.looseObject({
  table: z.string(),
  measures: z.array(z.looseObject({ measure: z.string(), variants: z.number(), label: nstr, default_series: z.string() })),
  measures_total: z.number(),
});
export type TableResponse = z.output<typeof TableResponse>;

export const MeasureResponse = z.looseObject({
  table: z.string(),
  measure: z.string(),
  facets: z.array(z.looseObject({ key: z.string(), values: z.array(z.string()), truncated: z.boolean().default(false) })),
  variants: z.number(),
  resolved: z.looseObject({ series: z.string(), indicator: z.string() }),
});
export type MeasureResponse = z.output<typeof MeasureResponse>;

const PopPoint = z.looseObject({ year: z.number(), v: DecimalString, unit: nstr, series: nstr, method: nstr });
export const MultiResponse = z.looseObject({
  data: z.looseObject({
    series: z.array(
      z.looseObject({
        meta: SeriesMeta,
        precision: z.string(),
        places: z.record(z.string(), z.array(Point)),
        transforms: z.record(z.string(), Transform).default({}),
        vintages_available: z.boolean().default(false),
      }),
    ),
    geos: z.array(z.string()),
    names: z.record(z.string(), z.string()).default({}),
    licences: z.array(Rights).default([]),
    population: z
      .looseObject({
        concept: z.looseObject({ id: z.string(), labels: z.record(z.string(), z.string()).default({}), unit: z.string() }).nullish(),
        places: z.record(z.string(), z.array(PopPoint)).default({}),
        licences: z.array(Rights).default([]),
      })
      .nullish()
      .transform((v) => v ?? null),
  }),
  snapshot: Snapshot,
  warnings,
  took_ms: z.number().nullish(),
});
export type MultiResponse = z.output<typeof MultiResponse>;
export type MultiSeries = MultiResponse['data']['series'][number];

const VPoint = z.looseObject({ p: z.string(), ps: z.string(), v: DecimalString, unit: nstr, proj: z.boolean(), vintage: z.string(), available_at: nstr, dataset: nstr });
export type VPoint = z.output<typeof VPoint>;
export const VintagesResponse = z.looseObject({
  data: z.looseObject({
    available: z.boolean(),
    reason: nstr,
    vintages: z.array(z.looseObject({ vintage: z.string(), available_at: nstr, dataset: nstr, cells: z.number() })).default([]),
    first: z.record(z.string(), z.array(VPoint)).default({}),
    by_vintage: z.record(z.string(), z.record(z.string(), z.array(VPoint))).default({}),
    note: nstr,
  }),
});
export type VintagesResponse = z.output<typeof VintagesResponse>;

export const Place = z.looseObject({ iso3: z.string(), iso2: nstr, label: z.string(), label_fr: nstr, region: nstr });
export type Place = z.output<typeof Place>;
const PlacesResponse = z.looseObject({ data: z.array(Place) });

// ------------------------------------------------------------------ selectors

/** A series selection: either an exact series key, or a table + measure + filters (open filters → default). */
export interface Selector {
  series?: string | null;
  table?: string | null;
  measure?: string | null;
  filters?: Record<string, string>;
}

export function selectorQuery(s: Selector): Query {
  if (s.series) return { series: s.series };
  const f = Object.entries(s.filters ?? {}).map(([k, v]) => `${k}=${v}`);
  return { table: s.table ?? undefined, measure: s.measure ?? undefined, f: f.length ? f : undefined };
}

// ------------------------------------------------------------------ calls (small LRU of resolved responses)

const cache = new Map<string, Promise<unknown>>();
function cached<T>(key: string, fn: () => Promise<T>): Promise<T> {
  const hit = cache.get(key);
  if (hit) return hit as Promise<T>;
  const p = fn();
  cache.set(key, p);
  p.catch(() => cache.delete(key));
  if (cache.size > 200) cache.delete(cache.keys().next().value as string);
  return p;
}
const keyOf = (path: string, q: Query) => `${path}?${JSON.stringify(q)}`;

export function getCatalog(q: string, theme: string | null, signal?: AbortSignal) {
  const query: Query = { q, theme: theme ?? undefined, limit: 30 };
  return v3get(`${BASE}/catalog`, CatalogResponse, query, signal);
}

export function getTable(table: string, q = '', signal?: AbortSignal) {
  return v3get(`${BASE}/table`, TableResponse, { table, q: q || undefined }, signal);
}

export function getMeasure(table: string, measure: string, filters: Record<string, string>, signal?: AbortSignal) {
  const query: Query = { table, measure, f: Object.entries(filters).map(([k, v]) => `${k}=${v}`) };
  return cached(keyOf('measure', query), () => v3get(`${BASE}/measure`, MeasureResponse, query, signal));
}

export interface MapParams {
  period?: string | null;
  precision?: string | null;
  level?: string | null;
  projections?: string | null;
}

export function getMap(sel: Selector, p: MapParams) {
  const query: Query = {
    ...selectorQuery(sel),
    period: p.period ?? undefined,
    precision: p.precision ?? undefined,
    level: p.level && p.level !== 'adm0' ? p.level : undefined,
    projections: p.projections && p.projections !== 'exclude' ? p.projections : undefined,
  };
  return cached(keyOf('map', query), () => v3get(`${BASE}/map`, MapResponse, query));
}

export function getPlace(sel: Selector, geo: string, precision?: string | null) {
  const query: Query = { ...selectorQuery(sel), geo, precision: precision ?? undefined };
  return cached(keyOf('place', query), () => v3get(`${BASE}/place`, PlaceResponse, query));
}

export function getMulti(series: string[], geos: string[], population: boolean) {
  const query: Query = { s: series, g: geos, population: population || undefined };
  return cached(keyOf('multi', query), () => v3get(`${BASE}/multi`, MultiResponse, query));
}

export function getVintages(series: string, geos: string[]) {
  const query: Query = { series, g: geos };
  return cached(keyOf('vint', query), () => v3get(`${BASE}/vintages`, VintagesResponse, query));
}

export const getPlaces = memo(() => v3get(`${BASE}/places`, PlacesResponse).then((r) => r.data));

/** Clears the response cache (tests). */
export function clearSeriesCache() {
  cache.clear();
}

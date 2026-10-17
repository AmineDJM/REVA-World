/**
 * Client of /v1/v8/coverage/* (src/reva/api/routes/v8_coverage.py). Raw bodies (no envelope), validated with zod:
 * tolerant on extra members, strict on what the page draws (counts are integers; an absent cell is absent — the API
 * never sends a zero for "no source").
 */
import { z } from 'zod';
import { request } from '../../../api/http';
import { parseWith } from '../../../v3/api/common';

const Int = z.number().int();
const Year = z.number().int().nullish().transform((v) => v ?? null);
const Str = z.string().nullish().transform((v) => v ?? null);

export const Meta = z.looseObject({
  computed_at: z.string(),
  today: z.string(),
  origin: z.string(),
  atlas_tables: Int,
  atlas_rows: Int,
  kg_entities: Int,
  kg_statements: Int,
  kg_computed: z.boolean(),
  places: Int,
  stale: z
    .looseObject({ tables_registered_since: Int, last_registered_at: Str })
    .nullish()
    .transform((v) => v ?? null),
  note: z.string(),
  refresh_command: z.string(),
});
export type Meta = z.output<typeof Meta>;

export const Summary = z.looseObject({
  tables: Int,
  rows: Int,
  countries: Int,
  latest_year: Year,
  last_loaded_at: Str,
  licences: z.record(z.string(), Int),
  kg_entities: Int,
  catalog: z.looseObject({ concepts: Int, with_loaded_table: Int, without_loaded_table: Int }).nullish().transform((v) => v ?? null),
  pending_loads: Int,
  pending_decisions: Int,
});
export type Summary = z.output<typeof Summary>;

/** One matrix cell: a group of countries (region/subregion) or one country. Absent = nothing stored. */
export const Cell = z.looseObject({
  countries_with_rows: Int.optional(),
  countries_with_kg: Int.optional(),
  rows: Int.optional(),
  tables: Int.optional(),
  y0: Year.optional(),
  y1: Year.optional(),
  y1_median: Year.optional(),
  future_rows: Int.optional(),
  kg: Int.optional(),
});
export type Cell = z.output<typeof Cell>;

export const Column = z.looseObject({
  id: z.string(),
  label: z.string(),
  region: z.string(),
  subregion: z.string().optional(),
  countries: Int,
  placement: z.string().optional(),
  placement_basis: Str.optional(),
});
export type Column = z.output<typeof Column>;

export const Row = z.looseObject({ id: z.string(), label: z.string(), summary: Summary, cells: z.record(z.string(), Cell) });
export type Row = z.output<typeof Row>;

export const Matrix = z.looseObject({ level: z.enum(['region', 'subregion', 'country']), meta: Meta, columns: z.array(Column), rows: z.array(Row) });
export type Matrix = z.output<typeof Matrix>;

export const Licence = z
  .looseObject({
    licence: z.string(),
    licence_url: Str.optional(),
    attribution: Str.optional(),
    commercial_use: z.string().optional(),
    non_commercial: z.boolean().optional(),
    share_alike: z.boolean().optional(),
    no_derivatives: z.boolean().optional(),
    open_licence: z.boolean().nullish(),
    restricted: z.boolean().optional(),
    required_notice: Str.optional(),
  })
  .nullish()
  .transform((v) => v ?? null);

export const Table = z.looseObject({
  key: z.string(),
  source: z.string(),
  dataset: z.string(),
  domain: z.string(),
  domain_basis: z.string(),
  rows: Int,
  indicators: Int.nullish(),
  countries: Int,
  rows_without_country: Int,
  period_min: Str,
  period_max: Str,
  latest_past_year: Year,
  future_rows: Int,
  recorded_at: Str,
  versions: Int,
  last_fetch_at: Str,
  cadence_seconds: Int.nullish(),
  licence: Licence,
  licence_class: z.enum(['open', 'non_commercial', 'restricted', 'unknown']),
  coverage_note: Str,
  overdue: z.boolean().nullish(),
  read_error: Str,
});
export type Table = z.output<typeof Table>;

export const Observed = z
  .looseObject({
    state: z.enum(['done', 'partial', 'not_seen']),
    checks: z.array(
      z.looseObject({
        connector: z.string(),
        min_version: Str,
        versions_acquired: z.array(z.string()),
        last_retrieved_at: Str,
        tables_missing: z.array(z.string()),
        met: z.boolean(),
      }),
    ),
  })
  .nullish()
  .transform((v) => v ?? null);

export const PendingItem = z.looseObject({
  id: z.string(),
  type: z.enum(['load', 'decision']),
  kind: z.string(),
  kind_label: z.string(),
  title: z.string(),
  detail: Str,
  unlocks: z.looseObject({
    domains: z.array(z.string()),
    atlas_sources: z.array(z.string()),
    tables: z.array(z.string()),
    tracks: z.array(z.string()),
    text: z.string(),
  }),
  action: z.string(),
  commands: z.array(z.string()),
  who: z.enum(['owner', 'lead']),
  status: z.enum(['open', 'queued', 'blocked', 'done', 'dropped']),
  disk_mb: z.number().nullish().transform((v) => v ?? null),
  source_docs: z.array(z.string()),
  observed: Observed,
});
export type PendingItem = z.output<typeof PendingItem>;

export const PendingView = z.looseObject({
  type: z.enum(['load', 'decision']),
  updated: z.string(),
  checked_at: Str,
  counts: z.looseObject({ items: Int, open: Int, observed_done: Int }),
  disk_mb_open_loads: z.number(),
  disk_mb_unknown: Int,
  groups: z.array(z.looseObject({ kind: z.string(), label: z.string(), items: z.array(PendingItem) })),
});
export type PendingView = z.output<typeof PendingView>;

const CountryCell = z.looseObject({ rows: Int.optional(), tables: Int.optional(), y0: Year.optional(), y1: Year.optional(), future_rows: Int.optional(), kg: Int.optional() });

export const DomainDetail = z.looseObject({
  id: z.string(),
  label: z.string(),
  summary: Summary,
  tables: z.array(Table),
  countries: z.array(CountryCell.extend({ iso3: z.string(), label: z.string(), region: z.string() })),
  countries_without_data: z.array(z.looseObject({ iso3: z.string(), label: z.string(), region: z.string() })),
  kg_kinds: z.array(z.looseObject({ kind: z.string(), entities: Int, sources: z.record(z.string(), Int) })),
  kg_located_in_note: Str,
  hist: z.record(z.string(), Int),
  hist_before: Int,
  hist_from: Int.nullish(),
  catalog: z
    .looseObject({ concepts: Int, with_loaded_table: Int, without_loaded_table: Int, examples: z.array(z.looseObject({ id: z.string(), label: z.string(), tables: z.array(z.string()) })) })
    .nullish()
    .transform((v) => v ?? null),
  connectors: z.array(
    z.looseObject({ name: z.string(), atlas_source: z.string(), provider: z.string(), licence: z.string(), tables_expected: z.array(z.string()), tables_loaded: z.array(z.string()), acquired: z.boolean(), last_retrieved_at: Str }),
  ),
  pending: z.array(PendingItem),
  meta: Meta,
});
export type DomainDetail = z.output<typeof DomainDetail>;

export const CountryDetail = z.looseObject({
  iso3: z.string(),
  label: z.string(),
  label_en: Str,
  region: z.string(),
  region_label: z.string(),
  subregion: z.string(),
  subregion_label: z.string(),
  placement: z.string(),
  placement_basis: Str,
  domains: z.array(z.looseObject({ id: z.string(), label: z.string(), cell: CountryCell.nullish().transform((v) => v ?? null) })),
  domains_without_data: z.array(z.string()),
  tables: z.array(
    z.looseObject({
      key: z.string(),
      domain: z.string(),
      rows: Int,
      y0: Year,
      y1: Year,
      future_rows: Int,
      licence_class: z.string(),
      licence: Str,
      required_notice: Str,
      recorded_at: Str,
    }),
  ),
  kg_kinds: z.array(z.looseObject({ kind: z.string(), entities: Int })),
  kg_located_in_note: Str,
  meta: Meta,
});
export type CountryDetail = z.output<typeof CountryDetail>;

export const Freshness = z.looseObject({
  meta: Meta,
  hist_from: Int.nullish(),
  domains: z.array(
    z.looseObject({
      id: z.string(),
      label: z.string(),
      hist: z.record(z.string(), Int),
      hist_before: Int,
      tables: z.array(
        z.looseObject({
          key: z.string(),
          rows: Int,
          latest_past_year: Year,
          future_rows: Int,
          recorded_at: Str,
          last_fetch_at: Str,
          cadence_seconds: Int.nullish(),
          overdue: z.boolean().nullish(),
        }),
      ),
    }),
  ),
  never_loaded: z.array(z.looseObject({ name: z.string(), atlas_source: z.string(), provider: z.string(), licence: z.string(), domain: z.string(), tables_expected: z.array(z.string()) })),
});
export type Freshness = z.output<typeof Freshness>;

const BASE = '/v1/v8/coverage';

async function get<S extends z.ZodType>(path: string, schema: S, query?: Record<string, string>, signal?: AbortSignal): Promise<z.output<S>> {
  const res = await request<unknown>('GET', `${BASE}${path}`, { query, signal });
  return parseWith(schema, res.body, `${BASE}${path}`);
}

/** Short-lived memo per key (the matrix is read by the page, the palette provider and the stat strip). */
const cache = new Map<string, { at: number; p: Promise<unknown> }>();
function cached<T>(key: string, fn: () => Promise<T>, ttlMs = 30_000): Promise<T> {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < ttlMs) return hit.p as Promise<T>;
  const p = fn();
  cache.set(key, { at: Date.now(), p });
  p.catch(() => cache.delete(key));
  return p;
}
export function clearCoverageCache(): void {
  cache.clear();
}

export const getMatrix = (level: Matrix['level']) => cached(`m:${level}`, () => get('/matrix', Matrix, { level }));
export const getDomain = (id: string) => cached(`d:${id}`, () => get(`/domains/${encodeURIComponent(id)}`, DomainDetail));
export const getCountry = (iso3: string) => cached(`c:${iso3}`, () => get(`/countries/${encodeURIComponent(iso3)}`, CountryDetail));
export const getFreshness = () => cached('f', () => get('/freshness', Freshness));
export const getPending = () => cached('p', () => get('/pending', PendingView));
export const getDecisions = () => cached('dec', () => get('/decisions', PendingView));

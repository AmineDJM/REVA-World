/**
 * Atlas client (agent ATLASCORE, src/reva/api/routes/atlas.py):
 *   GET /v1/atlas/concepts, /countries/{iso3} (dossier), /series, /map, /coverage.
 *
 * Cells are what a source reports: `value` is an exact decimal string or null (textual or
 * missing). The schema refuses a JSON number for `value`, so a filled zero can never sneak in.
 */
import { z } from 'zod';
import { ApiWarning, DecimalString, JsonRecord, v3get } from './common';

const enc = encodeURIComponent;
const nullishString = z.string().nullish().transform((v) => v ?? null);
const warnings = z.array(ApiWarning).nullish().transform((w) => w ?? []);
const catalogErrors = z.array(z.record(z.string(), z.string())).nullish().transform((v) => v ?? []);

export const AtlasSnapshotRef = z.looseObject({
  snapshot_sha256: z.string(),
  origin: z.string(),
  knowledge_cutoff: z.string(),
  tables: z.number().nullish(),
  rows: z.number().nullish(),
});
export type AtlasSnapshotRef = z.output<typeof AtlasSnapshotRef>;

export const AtlasProvenance = z.looseObject({
  raw_sha256: z.string(),
  selector: JsonRecord,
  table_artifact_sha256: z.string().nullish(),
  dataset_key: nullishString,
  dataset_id: nullishString,
});

export const AtlasCell = z.looseObject({
  iso3: nullishString,
  year: z.number().int().nullish().transform((v) => v ?? null),
  value: DecimalString.nullable(),
  value_text: nullishString,
  unit: z.string(),
  source: z.string(),
  dataset: z.string(),
  indicator: z.string(),
  series: z.string(),
  provider_geo: z.string().nullish(),
  entity_qid: nullishString,
  period_start: z.string(),
  period_end: z.string(),
  flags: JsonRecord.default({}),
  provenance: AtlasProvenance,
  source_rank: z.number().nullish().transform((v) => v ?? null),
  sources_reporting: z.number().nullish().transform((v) => v ?? null),
});
export type AtlasCell = z.output<typeof AtlasCell>;

export const AtlasConcept = z.looseObject({
  id: z.string(),
  labels: z.record(z.string(), z.string()).default({}),
  definition: z.string().default(''),
  unit: z.string(),
  domain: z.string(),
  subdomain: nullishString,
  value_kind: z.string().default('decimal'),
  source_order: z.array(z.string()).default([]),
  provider_declared: z.boolean().default(true),
  review_status: z.string().default('provider_declared'),
  notes: nullishString,
  tags: z.array(z.string()).default([]),
  catalog_file: z.string().nullish(),
});
export type AtlasConcept = z.output<typeof AtlasConcept>;

export const AtlasConceptList = z.looseObject({
  data: z.array(AtlasConcept),
  total: z.number(),
  domains: JsonRecord.default({}),
  catalog_errors: catalogErrors,
  warnings,
});
export type AtlasConceptList = z.output<typeof AtlasConceptList>;

export const AtlasSeriesPoint = z.looseObject({
  year: z.number().int(),
  value: DecimalString.nullable(),
  value_text: nullishString,
  series: z.string(),
  flags: JsonRecord.default({}),
});
export type AtlasSeriesPoint = z.output<typeof AtlasSeriesPoint>;

export const AtlasDossierConcept = z.looseObject({
  concept_id: z.string(),
  labels: z.record(z.string(), z.string()).default({}),
  unit: z.string(),
  subdomain: nullishString,
  latest: AtlasCell,
  years: z.number(),
  year_min: z.number(),
  year_max: z.number(),
  sources_used: z.array(z.string()).default([]),
  series: z.array(AtlasSeriesPoint).nullish().transform((v) => v ?? null),
});
export type AtlasDossierConcept = z.output<typeof AtlasDossierConcept>;

export const AtlasDossierDomain = z.looseObject({
  domain: z.string(),
  concepts: z.array(AtlasDossierConcept),
  without_data: z.array(z.string()).default([]),
});
export type AtlasDossierDomain = z.output<typeof AtlasDossierDomain>;

export const AtlasDossierResponse = z.looseObject({
  data: z.looseObject({
    iso3: z.string(),
    label: nullishString,
    domains: z.array(AtlasDossierDomain),
    concepts_with_data: z.number(),
    concepts_without_data: z.number(),
    non_annual_cells: z.number().nullish(),
  }),
  snapshot: AtlasSnapshotRef,
  notes: z.array(z.string()).default([]),
  catalog_errors: catalogErrors,
  warnings,
});
export type AtlasDossierResponse = z.output<typeof AtlasDossierResponse>;

export const AtlasMapResponse = z.looseObject({
  data: z.looseObject({
    concept: AtlasConcept,
    year: z.number().int(),
    values: z.array(AtlasCell),
    countries_with_value: z.number(),
    countries_without_value: z.array(z.string()).default([]),
    by_source: z.record(z.string(), z.number()).default({}),
  }),
  snapshot: AtlasSnapshotRef,
  notes: z.array(z.string()).default([]),
  warnings,
});
export type AtlasMapResponse = z.output<typeof AtlasMapResponse>;

export const AtlasSeriesResponse = z.looseObject({
  data: z.looseObject({
    concept: AtlasConcept,
    iso3: z.string(),
    best: z.array(AtlasCell),
    per_source: z.array(z.looseObject({ series: z.string(), source: z.string(), cells: z.array(AtlasCell) })).default([]),
    sources_without_data: z.array(z.string()).default([]),
  }),
  snapshot: AtlasSnapshotRef,
  notes: z.array(z.string()).default([]),
  warnings,
});

export const AtlasConceptCoverage = z.looseObject({
  concept_id: z.string(),
  domain: z.string(),
  countries: z.number(),
  cells: z.number(),
  year_min: z.number().nullish().transform((v) => v ?? null),
  year_max: z.number().nullish().transform((v) => v ?? null),
  by_source: z.array(JsonRecord).default([]),
});
export type AtlasConceptCoverage = z.output<typeof AtlasConceptCoverage>;

export const AtlasCoverageResponse = z.looseObject({
  data: z.array(AtlasConceptCoverage),
  by_country: z
    .array(z.looseObject({ iso3: z.string(), years: z.number(), year_min: z.number().nullish(), year_max: z.number().nullish(), best_sources_used: z.array(z.string()).default([]) }))
    .nullish()
    .transform((v) => v ?? null),
  snapshot: AtlasSnapshotRef,
  countries_total: z.number().nullish(),
  catalog_errors: catalogErrors,
  warnings,
});
export type AtlasCoverageResponse = z.output<typeof AtlasCoverageResponse>;

export const getAtlasConcepts = (q: { domain?: string; q?: string; limit?: number } = {}, s?: AbortSignal) =>
  v3get('/v1/atlas/concepts', AtlasConceptList, { limit: 5000, ...q }, s);

export const getAtlasDossier = (iso3: string, q: { domain?: string; series?: boolean } = {}, s?: AbortSignal) =>
  v3get(`/v1/atlas/countries/${enc(iso3.toUpperCase())}`, AtlasDossierResponse, { ...q }, s);

export const getAtlasMap = (concept: string, year: number, s?: AbortSignal) => v3get('/v1/atlas/map', AtlasMapResponse, { concept, year }, s);

export const getAtlasSeries = (concept: string, iso3: string, s?: AbortSignal) => v3get('/v1/atlas/series', AtlasSeriesResponse, { concept, iso3 }, s);

export const getAtlasCoverage = (q: { domain?: string; concept?: string } = {}, s?: AbortSignal) => v3get('/v1/atlas/coverage', AtlasCoverageResponse, { ...q }, s);

export function conceptLabel(c: { id?: string; concept_id?: string; labels?: Record<string, string> } | undefined | null): string {
  if (!c) return '';
  return c.labels?.fr || c.labels?.en || c.id || c.concept_id || '';
}

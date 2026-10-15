/**
 * Knowledge-graph client (agent KGCORE, src/reva/api/routes/kg.py):
 *   GET /v1/kg/entities/{qid}, /neighbours, /timeline, /v1/kg/path, /v1/kg/search.
 */
import { z } from 'zod';
import { envelopeOf, JsonRecord, v3get } from './common';

const enc = encodeURIComponent;
const nullishString = z.string().nullish().transform((v) => v ?? null);

export const KgRef = z.looseObject({ qid: z.string(), label: z.string() });
export type KgRef = z.output<typeof KgRef>;

export const KgTime = z.looseObject({ time: z.string(), precision: z.number().int() });
export type KgTime = z.output<typeof KgTime>;

export const KgPropertyOut = z.looseObject({
  pid: z.string(),
  label_en: nullishString,
  label_fr: nullishString,
  reva_predicate: nullishString,
  causal_assertion: z.boolean().default(false),
});
export type KgPropertyOut = z.output<typeof KgPropertyOut>;

export const KgProvenance = z.looseObject({
  source_key: z.string(),
  statement_id: nullishString,
  statement_key: z.string(),
  source_revision: nullishString,
  raw_sha256: nullishString,
  retrieved_at: z.string(),
  recorded_at: nullishString,
  load_id: z.number().nullish(),
  origin: z.string(),
  rank: z.string(),
  references: z.array(JsonRecord).nullish().transform((r) => r ?? []),
  verification: z.string(),
});
export type KgProvenance = z.output<typeof KgProvenance>;

const statementShape = {
  row_id: z.number(),
  subject: KgRef,
  property: KgPropertyOut,
  object: KgRef.nullable(),
  value: JsonRecord.nullish().transform((v) => v ?? null),
  start: KgTime.nullish().transform((v) => v ?? null),
  end: KgTime.nullish().transform((v) => v ?? null),
  point_in_time: KgTime.nullish().transform((v) => v ?? null),
  valid_from: nullishString,
  valid_to: nullishString,
  qualifiers: JsonRecord.nullish().transform((v) => v ?? {}),
  provenance: KgProvenance,
};

export const KgStatement = z.looseObject({ ...statementShape, direction: z.enum(['out', 'in']).nullish().transform((v) => v ?? null) });
export type KgStatement = z.output<typeof KgStatement>;

export const KgEntityProvenance = z.looseObject({
  source_key: z.string(),
  source_revision: nullishString,
  content_sha256: z.string(),
  retrieved_at: z.string(),
  recorded_at: nullishString,
  load_id: z.number().nullish(),
  raw_sha256s: z.array(z.string()).default([]),
  origin: z.string(),
  verification: z.string(),
});

export const KgEntity = z.looseObject({
  qid: z.string(),
  snapshot: z.number().nullish(),
  kind: nullishString,
  labels: z.record(z.string(), z.string()).default({}),
  descriptions: z.record(z.string(), z.string()).nullish().transform((v) => v ?? {}),
  aliases: z.record(z.string(), z.array(z.string())).nullish().transform((v) => v ?? {}),
  sitelinks: z.number().nullish().transform((v) => v ?? null),
  provenance: KgEntityProvenance.nullish().transform((v) => v ?? null),
  statements_out: z.number().default(0),
  statements_in: z.number().default(0),
  degree_capped_at: z.number().nullish(),
});
export type KgEntity = z.output<typeof KgEntity>;

export const KgNeighbours = z.looseObject({ qid: z.string(), label: z.string(), edges: z.array(KgStatement) });
export type KgNeighbours = z.output<typeof KgNeighbours>;

export const KgTimelineEvent = z.looseObject({
  ...statementShape,
  direction: z.enum(['out', 'in']).nullish().transform((v) => v ?? null),
  sort_date: nullishString,
  dated_by: z.string().nullish(),
});
export type KgTimelineEvent = z.output<typeof KgTimelineEvent>;

export const KgTimeline = z.looseObject({ qid: z.string(), label: z.string(), events: z.array(KgTimelineEvent), truncated: z.boolean().default(false) });
export type KgTimeline = z.output<typeof KgTimeline>;

export const KgSearchHit = z.looseObject({
  qid: z.string(),
  kind: nullishString,
  sitelinks: z.number().nullish(),
  origin: z.string().nullish(),
  match: z.string().nullish(),
  matched: z.array(z.looseObject({ lang: z.string(), label: z.string(), alias: z.boolean().default(false) })).default([]),
  labels: z.record(z.string(), z.string()).default({}),
});
export type KgSearchHit = z.output<typeof KgSearchHit>;

export const KgSearch = z.looseObject({ query: z.string(), mode: z.string().nullish(), results: z.array(KgSearchHit) });

export const KgHop = z.looseObject({
  ...statementShape,
  from: KgRef,
  to: KgRef,
  direction: z.enum(['forward', 'inverse']),
  relation_label_en: z.string(),
  relation_label_fr: nullishString,
  dated: z.boolean().default(false),
  time_status: z.string().nullish(),
  wording: z.string(),
  causal_assertion: JsonRecord.nullish().transform((v) => v ?? null),
});
export type KgHop = z.output<typeof KgHop>;

export const KgPath = z.looseObject({ length: z.number(), hops: z.array(KgHop), wording: z.string().nullish(), origins: z.array(z.string()).default([]) });
export type KgPath = z.output<typeof KgPath>;

export const KgPathResult = z.looseObject({
  from: z.string(),
  to: z.string(),
  from_label: z.string().nullish(),
  to_label: z.string().nullish(),
  found: z.boolean(),
  paths: z.array(KgPath),
  note: z.string().nullish(),
  search: JsonRecord.nullish(),
  not_found_reason: nullishString,
  community_note: nullishString,
});
export type KgPathResult = z.output<typeof KgPathResult>;

export const KgEntityResponse = envelopeOf(KgEntity);
export const KgNeighboursResponse = envelopeOf(KgNeighbours);
export const KgTimelineResponse = envelopeOf(KgTimeline);
export const KgSearchResponse = envelopeOf(KgSearch);
export const KgPathResponse = envelopeOf(KgPathResult);
export type KgNeighboursResponse = z.output<typeof KgNeighboursResponse>;
export type KgPathResponse = z.output<typeof KgPathResponse>;
export type KgTimelineResponse = z.output<typeof KgTimelineResponse>;

export interface NeighboursQuery {
  direction?: 'out' | 'in' | 'both';
  property?: string[];
  kind?: string;
  valid_from?: string;
  valid_to?: string;
  include_undated?: boolean;
  limit?: number;
  cursor?: string;
}

export const getKgEntity = (qid: string, s?: AbortSignal) => v3get(`/v1/kg/entities/${enc(qid)}`, KgEntityResponse, undefined, s);

export const getKgNeighbours = (qid: string, q: NeighboursQuery = {}, s?: AbortSignal) =>
  v3get(`/v1/kg/entities/${enc(qid)}/neighbours`, KgNeighboursResponse, { direction: 'both', limit: 80, ...q }, s);

export const getKgTimeline = (qid: string, q: { include_incoming?: boolean; valid_from?: string; valid_to?: string; limit?: number } = {}, s?: AbortSignal) =>
  v3get(`/v1/kg/entities/${enc(qid)}/timeline`, KgTimelineResponse, { limit: 1000, ...q }, s);

export const searchKg = (q: string, opts: { kind?: string; limit?: number } = {}, s?: AbortSignal) =>
  v3get('/v1/kg/search', KgSearchResponse, { q, lang: ['fr', 'en'], limit: opts.limit ?? 12, kind: opts.kind }, s);

export interface KgPathQuery {
  from: string;
  to: string;
  max_hops?: number;
  valid_from?: string;
  valid_to?: string;
  include_undated?: boolean;
  max_paths?: number;
}

export const getKgPath = (q: KgPathQuery, s?: AbortSignal) => v3get('/v1/kg/path', KgPathResponse, { ...q }, s);

/** Best display label of an entity: French, else English, else any, else the id. */
export function entityLabel(e: { qid: string; labels?: Record<string, string> } | null | undefined, fallback?: string): string {
  if (!e) return fallback ?? '';
  const l = e.labels ?? {};
  return l.fr || l.en || Object.values(l)[0] || fallback || e.qid;
}

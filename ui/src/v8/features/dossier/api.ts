/**
 * Client of /v1/v8/dossier/* (src/reva/api/routes/v8_dossier.py). The document is rendered as served: every value
 * part carries its footnote number; a missing part carries its reason. Validated with zod (tolerant on extra
 * members, strict on what the reader draws).
 */
import { z } from 'zod';
import { buildUrl, request } from '../../../api/http';

const Str = z.string().nullish().transform((v) => v ?? null);

export const ValueKind = z.enum(['observed', 'estimate', 'projection', 'forecast', 'scenario', 'derived', 'claim', 'label']);
export type ValueKind = z.infer<typeof ValueKind>;

export const Part = z.looseObject({
  t: z.enum(['text', 'value', 'missing']),
  text: z.string(),
  exact: Str,
  unit: Str,
  kind: ValueKind.nullish().transform((v) => v ?? null),
  fn: z.number().int().nullish().transform((v) => v ?? null),
  reason: Str,
  href: Str,
});
export type Part = z.infer<typeof Part>;

export const Sentence = z.looseObject({ template_id: z.string(), parts: z.array(Part), text: z.string() });
export type Sentence = z.infer<typeof Sentence>;

const ChartPoint = z.looseObject({ x: z.string(), y: z.number().nullable(), exact: Str, lo: z.number().nullish().transform((v) => v ?? null), hi: z.number().nullish().transform((v) => v ?? null) });
export type ChartPoint = z.infer<typeof ChartPoint>;
const ChartSeries = z.looseObject({ label: z.string(), kind: ValueKind, fn: z.number().int(), points: z.array(ChartPoint) });
export type ChartSeries = z.infer<typeof ChartSeries>;

export const Block = z.discriminatedUnion('type', [
  z.looseObject({ type: z.literal('paragraph'), id: z.string(), sentences: z.array(Sentence) }),
  z.looseObject({
    type: z.literal('table'),
    id: z.string(),
    template_id: z.string(),
    caption: z.string(),
    columns: z.array(z.string()),
    numeric: z.array(z.boolean()),
    rows: z.array(z.array(z.looseObject({ parts: z.array(Part) }))),
    note: Str,
  }),
  z.looseObject({ type: z.literal('list'), id: z.string(), template_id: z.string(), title: Str, items: z.array(Sentence), more: z.number().int() }),
  z.looseObject({
    type: z.literal('chart'),
    id: z.string(),
    template_id: z.string(),
    chart: z.enum(['line', 'bar', 'fan']),
    title: z.string(),
    unit: z.string(),
    x_kind: z.enum(['year', 'month', 'quarter', 'category']),
    series: z.array(ChartSeries),
    note: Str,
  }),
  z.looseObject({ type: z.literal('missing'), id: z.string(), template_id: z.string(), what: z.string(), reason: z.string() }),
]);
export type Block = z.infer<typeof Block>;
export type ChartBlock = Extract<Block, { type: 'chart' }>;

export const Footnote = z.looseObject({
  n: z.number().int(),
  source: z.string(),
  title: z.string(),
  dataset: Str,
  indicator: Str,
  period: Str,
  licence: Str,
  licence_url: Str,
  restricted: z.boolean(),
  non_commercial: z.boolean(),
  required_notice: Str,
  ref: Str,
  url: Str,
  retrieved: Str,
  verification: Str,
  method: Str,
});
export type Footnote = z.infer<typeof Footnote>;

export const DossierDoc = z.looseObject({
  schema: z.string(),
  kind: z.string(),
  subject: z.looseObject({ kind: z.string(), id: z.string(), label: z.string(), sublabel: Str, entity_href: Str }),
  title: z.string(),
  subtitle: z.string(),
  summary: z.array(Sentence),
  sections: z.array(z.looseObject({ id: z.string(), title: z.string(), lede: Str, blocks: z.array(Block) })),
  not_covered: z.array(z.looseObject({ what: z.string(), reason: z.string(), section: Str })),
  footnotes: z.array(Footnote),
  licences: z.array(z.looseObject({ title: z.string(), licence: z.string(), restricted: z.boolean().nullish(), non_commercial: z.boolean().nullish(), url: Str })),
  honesty: z.array(z.string()),
  meta: z.looseObject({
    template: z.string(),
    template_version: z.string(),
    snapshot_id: z.string(),
    atlas_snapshot: Str,
    generated_at: z.string(),
    compute_ms: z.number(),
    cached: z.boolean(),
    sentences: z.number().int(),
    values: z.number().int(),
    missing: z.number().int(),
  }),
});
export type DossierDoc = z.infer<typeof DossierDoc>;
export type Section = DossierDoc['sections'][number];

export const SubjectHit = z.looseObject({ kind: z.string(), id: z.string(), label: z.string(), sublabel: Str, featured: z.boolean() });
export type SubjectHit = z.infer<typeof SubjectHit>;
export const SubjectList = z.looseObject({ query: Str, hits: z.array(SubjectHit), kinds: z.array(z.looseObject({ kind: z.string(), label: z.string() })) });
export type SubjectList = z.infer<typeof SubjectList>;

const BASE = '/v1/v8/dossier';

export function docPath(kind: string, id: string): string {
  return `${BASE}/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`;
}

/** Download / print links of the exports (same document, other formats). */
export function exportUrl(kind: string, id: string, format: 'md' | 'html' | 'json'): string {
  return buildUrl(docPath(kind, id), { format });
}

export async function getDossier(kind: string, id: string, opts: { refresh?: boolean; signal?: AbortSignal } = {}): Promise<DossierDoc> {
  const r = await request<unknown>('GET', docPath(kind, id), { query: opts.refresh ? { refresh: true } : undefined, signal: opts.signal });
  return DossierDoc.parse(r.body);
}

export async function getSubjects(q: string | null, kind: string | null, signal?: AbortSignal): Promise<SubjectList> {
  const r = await request<unknown>('GET', `${BASE}/subjects`, { query: { q: q || undefined, kind: kind || undefined, limit: 20 }, signal });
  return SubjectList.parse(r.body);
}

export { dossierHref, KIND_LABEL } from './links';

export const VALUE_KIND_LABEL: Record<ValueKind, string> = {
  observed: 'observé',
  estimate: 'estimation publiée',
  projection: 'projection',
  forecast: 'prévision scellée',
  scenario: 'scénario non validé',
  derived: 'calcul REVA',
  claim: 'affirmation attribuée',
  label: 'déclaration de la source',
};

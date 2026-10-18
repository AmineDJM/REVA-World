/**
 * Fiche API client: ONE bundle call per page (GET /v1/v8/entity/{kind}/{id}) and the paged relations of one family.
 * Schemas mirror src/reva/api/routes/v8_entity.py; tolerant on extra fields, strict where honesty depends on it:
 * values are exact decimal strings or null (never a JSON number that could hide a filled zero).
 */
import { z } from "zod";
import { DecimalString, envelopeOf, v3get } from "../../../v3/api/common";

const S = z.string();
const Sn = z
  .string()
  .nullish()
  .transform((v) => v ?? null);
const Dn = DecimalString.nullish().transform((v) => v ?? null);

export const Licence = z.looseObject({
  licence: Sn,
  licence_url: Sn,
  attribution: Sn,
  restricted: z.boolean().default(false),
  non_commercial: z.boolean().default(false),
  share_alike: z.boolean().default(false),
  required_notice: Sn,
});
export type Licence = z.output<typeof Licence>;

export const Source = z.looseObject({
  source: S,
  label: S,
  dataset: Sn,
  statement_key: Sn,
  series: Sn,
  property: Sn,
  raw_sha256: Sn,
  selector: z.record(z.string(), z.unknown()).nullish(),
  retrieved_at: Sn,
  period: Sn,
  licence: Licence.nullish().transform((v) => v ?? null),
  verification: Sn,
  origin: Sn,
  href: Sn,
});
export type Source = z.output<typeof Source>;

export const Ref = z.looseObject({
  kind: S,
  id: S,
  label: S,
  loaded: z.boolean().default(true),
});
export type Ref = z.output<typeof Ref>;

export const MissingInfo = z.looseObject({ code: S, reason_fr: S });
export type MissingInfo = z.output<typeof MissingInfo>;

export const Point = z.looseObject({
  t: S,
  x: z.number(),
  v: Dn,
  projection: z.boolean().default(false),
});
export type Point = z.output<typeof Point>;

export const Link = z.looseObject({
  table: Sn,
  params: z
    .record(z.string(), z.string())
    .nullish()
    .transform((v) => v ?? null),
  url: Sn,
  section: Sn,
  family: Sn,
});
export type Link = z.output<typeof Link>;

export const Stat = z.looseObject({
  key: S,
  label_fr: S,
  value: Dn,
  value_text: Sn,
  unit: Sn,
  period: Sn,
  kind: z.enum([
    "observed",
    "projection",
    "estimate",
    "derived",
    "literal",
    "count",
    "relation",
    "judged",
  ]),
  ref: Ref.nullish().transform((v) => v ?? null),
  source: Source.nullish().transform((v) => v ?? null),
  missing: MissingInfo.nullish().transform((v) => v ?? null),
  points: z.array(Point).default([]),
  sub: Sn,
  link: Link.nullish().transform((v) => v ?? null),
});
export type Stat = z.output<typeof Stat>;

export const Ident = z.looseObject({
  scheme: S,
  value: S,
  url: Sn,
  source: Sn,
  property: Sn,
  label_fr: Sn,
  ref: Ref.nullish().transform((v) => v ?? null),
});
export type Ident = z.output<typeof Ident>;

export const Identity = z.looseObject({
  qid: Sn,
  kind: S,
  kg_kind: Sn,
  kind_label_fr: S,
  label: S,
  label_fr: Sn,
  label_en: Sn,
  description: Sn,
  definition: Sn,
  definition_source: Source.nullish().transform((v) => v ?? null),
  aliases: z.array(S).default([]),
  ids: z.array(Ident).default([]),
  anchor: z.boolean(),
  anchor_note: Sn,
  source: Source.nullish().transform((v) => v ?? null),
  sitelinks: z
    .number()
    .nullish()
    .transform((v) => v ?? null),
  snapshot_loaded: z.boolean(),
  community_edited: z.boolean(),
  degree: z.looseObject({
    statements_out: z.number(),
    statements_in: z.number(),
    capped_at: z.number(),
  }),
  iso3: Sn,
});
export type Identity = z.output<typeof Identity>;

export const Fact = z.looseObject({
  property: S,
  label_fr: S,
  value: Dn,
  value_text: S,
  unit: Sn,
  value_lo: Dn,
  value_hi: Dn,
  url: Sn,
  period: Sn,
  count: z.number(),
  points: z.array(Point).default([]),
  long_text: z.boolean(),
  source: Source,
  others: z.array(z.looseObject({ value_text: S, period: Sn })).default([]),
});
export type Fact = z.output<typeof Fact>;

export const RelItem = z.looseObject({
  ref: Ref,
  reading_fr: S,
  property: S,
  property_label: Sn,
  period: Sn,
  share: Dn,
  share_unit: Sn,
  identity: z.boolean(),
  via: Ref.nullish().transform((v) => v ?? null),
  source: Source,
});
export type RelItem = z.output<typeof RelItem>;

export const RelGroup = z.looseObject({
  family: S,
  label_fr: S,
  group: S,
  count: z.number(),
  count_is_lower_bound: z.boolean(),
  count_approx: z.boolean().default(false),
  items: z.array(RelItem),
  more: z.boolean(),
  via: Ref.nullish().transform((v) => v ?? null),
  properties: z
    .array(z.looseObject({ pid: S, label: S, count: z.number() }))
    .default([]),
});
export type RelGroup = z.output<typeof RelGroup>;

export const Series = z.looseObject({
  key: S,
  label_fr: S,
  theme: Sn,
  theme_fr: Sn,
  unit: Sn,
  value: Dn,
  value_text: Sn,
  period: Sn,
  kind: S,
  source: Source.nullish().transform((v) => v ?? null),
  points: z.array(Point).default([]),
  n_points: z.number(),
  first: Sn,
  last: Sn,
  source_breaks: z.number(),
  sources_used: z.array(S).default([]),
  link: Link.nullish().transform((v) => v ?? null),
  out_of_snapshot: z.boolean(),
  missing: MissingInfo.nullish().transform((v) => v ?? null),
  consulted: z.array(z.looseObject({ series: S, status: S })).default([]),
});
export type Series = z.output<typeof Series>;

export const Top = z.looseObject({
  key: S,
  label_fr: S,
  concept: S,
  year: z
    .number()
    .nullish()
    .transform((v) => v ?? null),
  n_countries: z
    .number()
    .nullish()
    .transform((v) => v ?? null),
  unit: Sn,
  items: z.array(z.looseObject({ iso3: S, value: Dn, source: Source })),
});
export type Top = z.output<typeof Top>;

export const Space = z.looseObject({
  chain: z.array(
    z.looseObject({
      ref: Ref,
      level: z
        .number()
        .nullish()
        .transform((v) => v ?? null),
      level_fr: Sn,
      iso3: Sn,
      via: Sn,
      basis: S,
      lat: z
        .number()
        .nullish()
        .transform((v) => v ?? null),
      lon: z
        .number()
        .nullish()
        .transform((v) => v ?? null),
    }),
  ),
  country_iso3: Sn,
  point: z
    .looseObject({ lat: z.number(), lon: z.number(), source_key: Sn })
    .nullish()
    .transform((v) => v ?? null),
  geometry: z
    .looseObject({
      geojson: z.looseObject({ type: S, coordinates: z.unknown() }),
      feature_key: S,
      note_fr: S,
    })
    .nullish()
    .transform((v) => v ?? null),
});
export type Space = z.output<typeof Space>;

export const Event = z.looseObject({
  date: Sn,
  x: z
    .number()
    .nullish()
    .transform((v) => v ?? null),
  label_fr: S,
  property: S,
  ref: Ref.nullish().transform((v) => v ?? null),
  value_text: Sn,
  source: Source,
});
export type Event = z.output<typeof Event>;

export const Claim = z.looseObject({
  claim_key: S,
  cause: Ref,
  effect: Ref,
  relation_fr: Sn,
  wording_fr: Sn,
  stance: Sn,
  polarity: Sn,
  tier: Sn,
  tier_fr: Sn,
  grade: Sn,
  grade_reasons_fr: z.array(S).default([]),
  mechanism: Sn,
  attribution_fr: Sn,
  contested: z.boolean(),
  source: Source,
});
export type Claim = z.output<typeof Claim>;

export const Effect = z.looseObject({
  estimate_key: S,
  role: S,
  cause_fr: S,
  effect_fr: S,
  cause_id: Sn,
  effect_id: Sn,
  wording_fr: Sn,
  measure: Sn,
  unit: Sn,
  point: Dn,
  ci_low: Dn,
  ci_high: Dn,
  uncertainty_status: Sn,
  method: Sn,
  countries: z.array(S).default([]),
  source: Source,
});
export type Effect = z.output<typeof Effect>;

export const ForecastRow = z.looseObject({
  model: S,
  label: S,
  kind: z.enum(["official", "ensemble", "candidate", "baseline"]),
  kind_fr: S,
  description: Sn,
  status: Sn,
  point: Dn,
  probability: Dn,
  lo: Dn,
  hi: Dn,
  level: Sn,
  issued_at: Sn,
  batch_sha256: Sn,
  score: z
    .record(z.string(), z.string().nullable())
    .nullish()
    .transform((v) => v ?? null),
});
export type ForecastRow = z.output<typeof ForecastRow>;

export const Question = z.looseObject({
  question_id: S,
  family: Sn,
  label_fr: Sn,
  text_fr: Sn,
  geo: Sn,
  geo_label_fr: Sn,
  reference_period: Sn,
  type: Sn,
  unit: Sn,
  threshold: Sn,
  status: S,
  outcome: Dn,
  expected_resolution: Sn,
  resolution_source: Sn,
  forecasts: z.array(ForecastRow),
  official_present: z.boolean(),
  judged_fr: S,
});
export type Question = z.output<typeof Question>;

export const MissingItem = z.looseObject({
  section: S,
  what_fr: S,
  reason_code: S,
  reason_fr: S,
});
export type MissingItem = z.output<typeof MissingItem>;

export const SourceSummary = z.looseObject({
  source: S,
  label: S,
  n_values: z.number(),
  licences: z.array(S),
  restricted: z.boolean(),
  non_commercial: z.boolean(),
  verification: Sn,
});
export type SourceSummary = z.output<typeof SourceSummary>;

export const Bundle = z.looseObject({
  canonical: z.looseObject({ kind: S, id: S }),
  resolved_via: S,
  identity: Identity.nullish().transform((v) => v ?? null),
  headline: z.array(Stat),
  facts: z.array(Fact),
  relations: z.array(RelGroup),
  related_forms: z
    .array(
      z.looseObject({
        ref: Ref,
        relation_fr: Sn,
        relations: z.array(RelGroup),
      }),
    )
    .default([]),
  series: z.array(Series),
  tops: z.array(Top).default([]),
  space: Space,
  time: z.array(Event),
  claims: z.looseObject({
    incoming: z.array(Claim),
    outgoing: z.array(Claim),
    n_in: z.number(),
    n_out: z.number(),
    note_fr: S,
  }),
  effects: z.looseObject({
    items: z.array(Effect),
    n: z.number(),
    n_country_scope: z
      .number()
      .nullish()
      .transform((v) => v ?? null),
    note_fr: S,
  }),
  forecasts: z.looseObject({
    questions: z.array(Question),
    n_total: z.number(),
    n_pending: z.number(),
    n_resolved: z.number(),
    families: z.array(S),
    note_fr: S,
  }),
  missing: z.array(MissingItem),
  sources: z.array(SourceSummary),
  notes: z.array(S).default([]),
  meta: z.looseObject({
    timings_ms: z.record(z.string(), z.number()),
    errors: z.array(z.looseObject({ section: S, error: S })).default([]),
    cached: z.boolean(),
    generated_at: Sn,
    atlas_snapshot: Sn,
    atlas_cutoff: Sn,
    warming: z.array(S).default([]),
  }),
});
export type Bundle = z.output<typeof Bundle>;

const BundleEnvelope = envelopeOf(Bundle);
export type BundleResponse = z.output<typeof BundleEnvelope>;

const enc = encodeURIComponent;

export function getBundle(
  kind: string,
  id: string,
  signal?: AbortSignal,
): Promise<BundleResponse> {
  return v3get(
    `/v1/v8/entity/${enc(kind)}/${enc(id)}`,
    BundleEnvelope,
    undefined,
    signal,
  );
}

const RelPage = envelopeOf(
  z.looseObject({ family: S, label_fr: S, items: z.array(RelItem) }),
);
export type RelPage = z.output<typeof RelPage>;

export function getRelations(
  kind: string,
  id: string,
  family: string,
  cursor?: string | null,
  signal?: AbortSignal,
): Promise<RelPage> {
  const q: Record<string, string> = { family, limit: "100" };
  if (cursor) q.cursor = cursor;
  return v3get(
    `/v1/v8/entity/${enc(kind)}/${enc(id)}/relations`,
    RelPage,
    q,
    signal,
  );
}

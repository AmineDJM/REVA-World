/**
 * Universal relation families (V4, agent V4CORE, src/reva/api/routes/kg_families.py):
 *   GET /v1/kg/families, /v1/kg/entities/{qid}/families, /v1/kg/entities/{qid}/places, /v1/kg/families/path.
 * No family is causal; causal claims are read from /v1/causal.
 */
import { z } from "zod";
import { AtlasRights } from "./atlas";
import { envelopeOf, JsonRecord, v3get } from "./common";
import { KgRef, KgStatement } from "./kg";

const enc = encodeURIComponent;
const nullishString = z
  .string()
  .nullish()
  .transform((v) => v ?? null);
const Labels = z.looseObject({ fr: z.string(), en: z.string() });

export const FamilyShare = z.looseObject({
  value: z.string(),
  fraction: nullishString,
  unit: nullishString,
  scale: z.string(),
  qualifier: z.string(),
});
export type FamilyShare = z.output<typeof FamilyShare>;

export const AtlasHint = z.looseObject({
  concept: z.string(),
  iso3: nullishString,
  table: nullishString,
});
export type AtlasHint = z.output<typeof AtlasHint>;

export const FamilyNeighbour = z.looseObject({
  neighbour: KgRef.extend({ kind: nullishString }),
  reading: z.looseObject({ label: Labels, inverse: z.boolean() }),
  subtype: nullishString,
  mapping: z.looseObject({
    origin: z.string(),
    agent: z.string(),
    direction: z.string(),
  }),
  share: FamilyShare.nullish().transform((v) => v ?? null),
  atlas: AtlasHint.nullish().transform((v) => v ?? null),
  rights: AtlasRights.nullish().transform((v) => v ?? null),
  statement: KgStatement,
});
export type FamilyNeighbour = z.output<typeof FamilyNeighbour>;

export const FamilyGroup = z.looseObject({
  family: z.string(),
  label: Labels,
  inverse_label: Labels,
  group: z.string(),
  symmetric: z.boolean().default(false),
  count: z.number(),
  count_is_lower_bound: z.boolean().default(false),
  shown: z.number(),
  truncated: z.boolean().default(false),
  properties: z
    .array(
      z.looseObject({
        pid: z.string(),
        count: z.number(),
        label_fr: nullishString,
        label_en: nullishString,
      }),
    )
    .default([]),
  neighbours: z.array(FamilyNeighbour),
  causal: z.literal(false),
});
export type FamilyGroup = z.output<typeof FamilyGroup>;

export const UnmappedProperty = z.looseObject({
  pid: z.string(),
  direction: z.enum(["out", "in"]),
  count: z.number(),
  label_fr: nullishString,
  label_en: nullishString,
  causal_assertion: z.boolean().default(false),
});
export type UnmappedProperty = z.output<typeof UnmappedProperty>;

export const LiteralStatement = KgStatement.extend({
  rights: AtlasRights.nullish().transform((v) => v ?? null),
});
export type LiteralStatement = z.output<typeof LiteralStatement>;

export const EntityFamilies = z.looseObject({
  qid: z.string(),
  label: z.string(),
  labels: z.record(z.string(), z.string()).default({}),
  kind: nullishString,
  families: z.array(FamilyGroup),
  unmapped: z.array(UnmappedProperty).default([]),
  literals: z.array(LiteralStatement).default([]),
  literals_truncated: z.boolean().default(false),
  scan_capped: z.boolean().default(false),
  registry: JsonRecord.nullish(),
  notes: z.array(z.string()).default([]),
});
export type EntityFamilies = z.output<typeof EntityFamilies>;

export const Coordinates = z.looseObject({
  latitude: z.number(),
  longitude: z.number(),
  statement_key: z.string(),
  source_key: z.string(),
});
export type Coordinates = z.output<typeof Coordinates>;

export const PlaceLevel = z.looseObject({
  entity: KgRef.extend({ kind: nullishString }),
  level: z
    .number()
    .nullish()
    .transform((v) => v ?? null),
  via: nullishString,
  statement_key: nullishString,
  basis: z.enum(["start", "statement", "iso3166_2_code_prefix"]),
  iso3: nullishString,
  coordinates: Coordinates.nullish().transform((v) => v ?? null),
});
export type PlaceLevel = z.output<typeof PlaceLevel>;

export const EntityPlaces = z.looseObject({
  qid: z.string(),
  label: z.string(),
  chain: z.array(PlaceLevel),
  levels: z.record(z.string(), z.string()).default({}),
  country_iso3: nullishString,
  coordinates: Coordinates.nullish().transform((v) => v ?? null),
  note: z.string().default(""),
});
export type EntityPlaces = z.output<typeof EntityPlaces>;

export const EntityFamiliesResponse = envelopeOf(EntityFamilies);
export type EntityFamiliesResponse = z.output<typeof EntityFamiliesResponse>;
export const EntityPlacesResponse = envelopeOf(EntityPlaces);
export type EntityPlacesResponse = z.output<typeof EntityPlacesResponse>;

export const getEntityFamilies = (
  qid: string,
  q: { family?: string[]; per_family?: number } = {},
  s?: AbortSignal,
) =>
  v3get(
    `/v1/kg/entities/${enc(qid)}/families`,
    EntityFamiliesResponse,
    { per_family: 50, ...q },
    s,
  );

export const getEntityPlaces = (qid: string, s?: AbortSignal) =>
  v3get(
    `/v1/kg/entities/${enc(qid)}/places`,
    EntityPlacesResponse,
    undefined,
    s,
  );

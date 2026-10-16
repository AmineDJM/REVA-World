/**
 * Interconnections client (agents LINKS/LINKS2, src/reva/api/routes/links.py, src/reva/links/**):
 *   GET /v1/links/path?from&to&k&max_hops&as_of[&steps&via_any&via_all&non_transit]  (explained paths)
 *   GET /v1/links/{iso3|qid}?year&kind&facet&limit   (top-N links per kind: `by_kind` groups)
 *   GET /v1/links/pair?a&b[&kind&from_year&to_year]  (every link between two countries over time)
 *
 * Schemas are tolerant (unknown members pass through) and keep weights as decimal strings: a link
 * that is absent is "no link", never a zero.
 */
import { z } from 'zod';
import { envelopeOf, v3get } from './common';

const enc = encodeURIComponent;

export const LinkTemporal = z.looseObject({
  kind: z.enum(['interval', 'point', 'snapshot', 'undated']),
  start: z.string().nullish(),
  end: z.string().nullish(),
  point: z.string().nullish(),
  year: z.number().nullish(),
});
export type LinkTemporal = z.output<typeof LinkTemporal>;

export const LinkEvidence = z.looseObject({
  kind: z.enum(['kg_statement', 'atlas_row']),
  source: z.string(),
  statement_id: z.string().nullish(),
  subject: z.string().nullish(),
  pid: z.string().nullish(),
  object: z.unknown().optional(),
  source_revision: z.string().nullish(),
  raw_sha256: z.string().nullish(),
  selector: z.string().nullish(),
  retrieved_at: z.string().nullish(),
  origin: z.string().nullish(),
  cite: z.string().nullish(),
});
export type LinkEvidence = z.output<typeof LinkEvidence>;

const LangText = z.looseObject({ fr: z.string().nullish(), en: z.string().nullish() });

export const LinkHop = z.looseObject({
  from: z.string(),
  to: z.string(),
  kind: z.enum(['statement', 'link']),
  type: z.string(),
  forward: z.boolean(),
  causal: z.boolean().default(false),
  temporal: LinkTemporal,
  evidence: z.array(LinkEvidence),
  sources: z.array(z.string()).default([]),
  alternatives: z.number().default(0),
  text: LangText,
  via: z.array(z.string()).nullish(),
  weight: z.string().nullish(),
  unit: z.string().nullish(),
});
export type LinkHop = z.output<typeof LinkHop>;

export const LinkPath = z.looseObject({
  nodes: z.array(z.string()),
  hops: z.array(LinkHop),
  hop_count: z.number(),
  cost: z.number().nullish(),
  text: z.looseObject({ fr: z.array(z.string()).default([]), en: z.array(z.string()).default([]) }).nullish(),
});
export type LinkPath = z.output<typeof LinkPath>;

export const LinksPathData = z.looseObject({
  query: z.record(z.string(), z.unknown()).default({}),
  found: z.boolean(),
  reason: z.string().nullish(),
  truncated: z.boolean().default(false),
  explored_nodes: z.number().nullish(),
  paths: z.array(LinkPath),
  labels: z.record(z.string(), z.record(z.string(), z.string())).default({}),
  disclaimer: LangText.nullish(),
});
export type LinksPathData = z.output<typeof LinksPathData>;

export const CountryLink = z.looseObject({
  subject: z.string(),
  object: z.string(),
  link_type: z.string(),
  directed: z.boolean().default(false),
  temporal: LinkTemporal,
  via: z.array(z.string()).default([]),
  evidence: z.array(LinkEvidence),
  sources: z.array(z.string()).default([]),
  weight: z.string().nullish(),
  unit: z.string().nullish(),
  basis: z.record(z.string(), z.unknown()).nullish(),
  /** ISO3 of each end when it is a crosswalk country (null otherwise); served by the store-backed views. */
  iso3: z.array(z.string().nullable()).nullish(),
});
export type CountryLink = z.output<typeof CountryLink>;

/** One ranked group of a country's links of one kind (direction, facet, latest period). */
export const LinkGroup = z.looseObject({
  direction: z.string().default('both'),
  facet: z.string().nullish(),
  period: z.string().nullish(),
  total: z.number().nullish(),
  links: z.array(CountryLink).default([]),
});
export type LinkGroup = z.output<typeof LinkGroup>;

const KindMeta = {
  labels: LangText.nullish(),
  inverse_labels: LangText.nullish(),
  honesty: LangText.nullish(),
  directed: z.boolean().nullish(),
  status: z.string().nullish(),
  temporal: z.string().nullish(),
  weight: z.looseObject({ kind: z.string().nullish(), unit: z.string().nullish() }).nullish(),
  sources: z.array(z.string()).nullish(),
};

export const KindGroups = z.looseObject({ ...KindMeta, groups: z.array(LinkGroup).default([]) });
export type KindGroups = z.output<typeof KindGroups>;

export const CountryLinksData = z.looseObject({
  qid: z.string(),
  mode: z.string().nullish(),
  iso3: z.string().nullish(),
  by_kind: z.record(z.string(), KindGroups).nullish(),
  store: z.record(z.string(), z.unknown()).nullish(),
  notes: z.union([LangText, z.array(z.string())]).nullish(),
  query: z.string().nullish(),
  links: z.array(CountryLink),
  labels: z.record(z.string(), z.record(z.string(), z.string())).default({}),
  link_types: z.record(z.string(), z.looseObject({ status: z.string(), derived: z.number().default(0) })).default({}),
  derivation: z.unknown().optional(),
  truncated: z.boolean().default(false),
});
export type CountryLinksData = z.output<typeof CountryLinksData>;

/** A link of the pair view: the country ISO3 of each end (null for a predecessor state), facet, text. */
export const PairLink = CountryLink.extend({
  facet: z.string().nullish(),
  text: LangText.nullish(),
  weight_kind: z.string().nullish(),
});
export type PairLink = z.output<typeof PairLink>;

export const PairKind = z.looseObject({
  ...KindMeta,
  count: z.number(),
  first_year: z.number().nullish(),
  last_year: z.number().nullish(),
  links: z.array(PairLink),
});
export type PairKind = z.output<typeof PairKind>;

export const PredecessorLink = z.looseObject({
  country: z.string(),
  predecessor_of: z.string(),
  predecessor: z.string(),
  link: PairLink,
  succession: z.array(PairLink).default([]),
});
export type PredecessorLink = z.output<typeof PredecessorLink>;

export const PairLinksData = z.looseObject({
  a: z.string(),
  b: z.string(),
  from_year: z.number().nullish(),
  to_year: z.number().nullish(),
  store: z.record(z.string(), z.unknown()).nullish(),
  atlas_snapshot: z.record(z.string(), z.unknown()).nullish(),
  kinds: z.record(z.string(), PairKind),
  kinds_without_links: z.array(z.string()).default([]),
  vote_detail: z.unknown().optional(),
  predecessor_links: z.array(PredecessorLink).default([]),
  labels: z.record(z.string(), z.record(z.string(), z.string())).default({}),
  truncated: z.array(z.string()).default([]),
  notes: z.array(z.string()).default([]),
  honesty: z.unknown().optional(),
});
export type PairLinksData = z.output<typeof PairLinksData>;

export const PairLinksResponse = envelopeOf(PairLinksData);
export type PairLinksResponse = z.output<typeof PairLinksResponse>;

export const LinksPathResponse = envelopeOf(LinksPathData);
export const CountryLinksResponse = envelopeOf(CountryLinksData);
export type LinksPathResponse = z.output<typeof LinksPathResponse>;
export type CountryLinksResponse = z.output<typeof CountryLinksResponse>;

export interface LinksPathQuery {
  from: string;
  to: string;
  k?: number;
  max_hops?: number;
  as_of?: number;
  include_undated?: boolean;
  /** Ordered hop-type steps; each a comma-separated set of kinds ("P1891,P710"). */
  steps?: string[];
  via_any?: string[];
  via_all?: string[];
  /** Properties whose objects may end a path but are never passed through (P37, P2936, P30, P2348). */
  non_transit?: string[];
}

export const getLinksPath = (q: LinksPathQuery, s?: AbortSignal) => v3get('/v1/links/path', LinksPathResponse, { ...q }, s);

export const getCountryLinks = (ref: string, q: { year?: number; limit?: number } = {}, s?: AbortSignal) =>
  v3get(`/v1/links/${enc(ref)}`, CountryLinksResponse, { ...q }, s);

export const getPairLinks = (a: string, b: string, q: { from_year?: number; to_year?: number } = {}, s?: AbortSignal) =>
  v3get('/v1/links/pair', PairLinksResponse, { a, b, ...q }, s);

/** French (else English, else id) label of a node in a links result. */
export function linkNodeLabel(labels: Record<string, Record<string, string>>, id: string): string {
  const l = labels[id] ?? {};
  return l.fr || l.en || id;
}

/** French labels of the derived link types (registry/links/link_types.yaml ids). */
export const LINK_TYPE_LABEL: Record<string, string> = {
  border_wikidata: 'frontière commune (Wikidata)',
  land_border: 'frontière terrestre',
  maritime_neighbour: 'voisin maritime',
  shared_official_language: 'langue officielle commune',
  shared_language_used: 'langue parlée commune',
  shared_language_family: 'même famille de langues',
  colony_of: 'colonie de',
  former_colony_of: 'ancienne colonie de',
  successor_of: 'successeur de',
  co_member: 'membres d’une même organisation',
  alliance_co_member: 'membres d’une même alliance',
  co_signatory: 'cosignataires d’un traité',
  conflict_co_participant: 'participants à un même conflit (camps non précisés)',
  conflict_same_side: 'même camp dans un conflit',
  conflict_opposing_sides: 'camps opposés dans un conflit',
  diplomatic_relation: 'relations diplomatiques',
  shared_head_of_state: 'même chef d’État',
  shared_currency: 'monnaie commune',
  shared_legal_family: 'même famille juridique',
  unga_vote_agreement: 'part de votes identiques à l’AG de l’ONU',
  bilateral_trade: 'exportations de biens déclarées par l’exportateur (FMI)',
  bilateral_trade_imports: 'importations de biens déclarées par l’importateur (FMI)',
  bilateral_trade_baci: 'exportations de biens (BACI, par section SH)',
  migrant_stock: 'stock de migrants (pays de naissance → pays de résidence)',
  land_border_cshapes: 'frontière terrestre datée (CShapes)',
  colonial_rule_cshapes: 'territoire sous l’autorité de (CShapes)',
  peacekeeping_contributor_to_host: 'personnel fourni à une mission de l’ONU dans',
  shared_polity_territory: 'territoires couverts par une même entité politique',
  shared_language_glottolog: 'langue parlée dans les deux pays (Glottolog)',
  co_party: 'parties à un même traité (ONU)',
};

/** French label of a link type: the server's taxonomy label when served, else the local table, else the id. */
export function linkTypeLabel(type: string, labels?: { fr?: string | null; en?: string | null } | null): string {
  return labels?.fr || LINK_TYPE_LABEL[type] || labels?.en || type.replace(/_/g, ' ');
}

export const LINK_STATUS_LABEL: Record<string, string> = {
  derivable: 'dérivé des sources chargées',
  awaiting_source: 'source pas encore livrée (aucun lien ≠ aucune connexion)',
  no_open_source: 'aucune source ouverte connue',
};

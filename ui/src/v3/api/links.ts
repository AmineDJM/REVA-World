/**
 * Interconnections client (agent LINKS, src/reva/api/routes/links.py, src/reva/links/**):
 *   GET /v1/links/path?from&to&k&max_hops&as_of  (k shortest explained paths)
 *   GET /v1/links/{iso3|qid}?year                 (every derived link of a country)
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
});
export type CountryLink = z.output<typeof CountryLink>;

export const CountryLinksData = z.looseObject({
  qid: z.string(),
  query: z.string().nullish(),
  links: z.array(CountryLink),
  labels: z.record(z.string(), z.record(z.string(), z.string())).default({}),
  link_types: z.record(z.string(), z.looseObject({ status: z.string(), derived: z.number().default(0) })).default({}),
  derivation: z.unknown().optional(),
  truncated: z.boolean().default(false),
});
export type CountryLinksData = z.output<typeof CountryLinksData>;

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
}

export const getLinksPath = (q: LinksPathQuery, s?: AbortSignal) => v3get('/v1/links/path', LinksPathResponse, { ...q }, s);

export const getCountryLinks = (ref: string, q: { year?: number } = {}, s?: AbortSignal) => v3get(`/v1/links/${enc(ref)}`, CountryLinksResponse, { ...q }, s);

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
  unga_vote_agreement: 'accord de vote à l’AG de l’ONU',
  bilateral_trade: 'commerce bilatéral',
  migrant_stock: 'stock de migrants',
};

export const LINK_STATUS_LABEL: Record<string, string> = {
  derivable: 'dérivé des sources chargées',
  awaiting_source: 'source pas encore livrée (aucun lien ≠ aucune connexion)',
  no_open_source: 'aucune source ouverte connue',
};

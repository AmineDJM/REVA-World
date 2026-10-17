/**
 * Client of the V8 Liens API (src/reva/api/routes/v8_paths.py, agent V8LIENS):
 *   GET /v1/v8/paths, /v1/v8/neighbourhood, /v1/v8/paths/nodes, /v1/v8/paths/status
 * plus the universal search (/v1/v8/search, V8API) for the pickers when it is deployed.
 *
 * Schemas are tolerant (unknown members pass) but strict on what the screen states: every hop has a
 * sentence, a kind and a licence member (null = unknown, shown as such, never as "open").
 */
import { z } from 'zod';
import { envelopeOf, v3get } from '../../../v3/api/common';

const nstr = z
  .string()
  .nullish()
  .transform((v) => v ?? null);
const Labels = z.looseObject({ fr: z.string(), en: z.string().nullish() });

export const NodeRef = z.looseObject({
  id: z.string(),
  label: z.string(),
  label_en: z.string().nullish(),
  label_loaded: z.boolean().default(true),
  kind: nstr,
  kind_label: nstr,
  virtual: z.boolean().default(false),
  scope_iso3: nstr,
  fiche: z.looseObject({ kind: z.string(), id: z.string() }).nullish().transform((v) => v ?? null),
  degree: z
    .number()
    .nullish()
    .transform((v) => v ?? null),
});
export type NodeRef = z.output<typeof NodeRef>;

export const Licence = z.looseObject({
  licence: z.string(),
  licence_url: nstr,
  attribution: nstr,
  commercial_use: z.string().nullish(),
  non_commercial: z.boolean().nullish(),
  share_alike: z.boolean().nullish(),
  no_derivatives: z.boolean().nullish(),
  open_licence: z.boolean().nullish(),
  restricted: z.boolean().nullish(),
  required_notice: nstr,
});
export type Licence = z.output<typeof Licence>;

export const HopKind = z.enum(['statement', 'causal_claim', 'effect_estimate', 'estimate_scope', 'declared_bridge']);
export type HopKind = z.output<typeof HopKind>;

export const Hop = z.looseObject({
  from: z.string(),
  to: z.string(),
  kind: HopKind,
  direction: z.enum(['forward', 'inverse']),
  cost: z.number(),
  node_penalty: z.number().default(0),
  alternatives: z.number().default(0),
  family: z
    .looseObject({ id: z.string(), label: Labels, inverse: z.boolean().nullish() })
    .nullish()
    .transform((v) => v ?? null),
  property: z
    .looseObject({ pid: z.string(), label_fr: nstr, label_en: nstr })
    .nullish()
    .transform((v) => v ?? null),
  relation: Labels.nullish().transform((v) => v ?? null),
  sentence: Labels,
  source: z
    .looseObject({
      key: z.string(),
      label: z.string(),
      statement_id: nstr,
      revision: nstr,
      citation: nstr,
      retrieved_at: nstr,
    })
    .nullish()
    .transform((v) => v ?? null),
  licence: Licence.nullable(),
  strength: z
    .looseObject({
      level: z.string(),
      label: z.string(),
      grade: nstr,
      tier: nstr,
      verification: nstr,
      reasons: z.array(z.string()).nullish(),
      method: nstr,
      uncertainty: nstr,
    })
    .nullish()
    .transform((v) => v ?? null),
  dated: z.looseObject({ from: nstr, to: nstr }).nullish().transform((v) => v ?? null),
  detail: z.record(z.string(), z.unknown()).nullish().transform((v) => v ?? null),
  origin: z.string().default('live'),
});
export type Hop = z.output<typeof Hop>;

export const PathOut = z.looseObject({
  rank: z.number(),
  cost: z.number(),
  length: z.number(),
  kind: z.enum(['documented', 'mixed', 'causal']),
  nodes: z.array(z.string()),
  hops: z.array(Hop),
  weakest_grade: nstr,
  restricted_licences: z.array(z.string()).default([]),
  unknown_licences: z.number().default(0),
  variants: z
    .looseObject({ count: z.number(), position: z.number().nullish(), examples: z.array(z.string()).default([]) })
    .nullish()
    .transform((v) => v ?? null),
});
export type PathOut = z.output<typeof PathOut>;

export const SearchInfo = z.looseObject({
  max_hops: z.number(),
  k: z.number(),
  families: z.array(z.string()).nullish(),
  include_causal: z.boolean(),
  levels: z.number().default(0),
  entries_scanned: z.number().default(0),
  nodes_reached: z.number().default(0),
  hubs_not_expanded: z.array(NodeRef).default([]),
  hubs_not_expanded_count: z.number().default(0),
  truncated: z.boolean().default(false),
  truncation_reasons: z.array(z.string()).default([]),
  search_ms: z.number().default(0),
});

export const PathsData = z.looseObject({
  from: NodeRef,
  to: NodeRef,
  found: z.boolean(),
  paths: z.array(PathOut),
  nodes: z.record(z.string(), NodeRef),
  search: SearchInfo,
  not_found_reason: nstr,
  note: z.looseObject({ fr: z.string(), en: z.string().nullish() }),
  index: z.record(z.string(), z.unknown()).default({}),
});
export type PathsData = z.output<typeof PathsData>;
const PathsResponse = envelopeOf(PathsData);
export type PathsResponse = z.output<typeof PathsResponse>;

export const EgoData = z.looseObject({
  center: NodeRef,
  edges: z.array(z.looseObject({ hop: Hop, group: z.string() })),
  nodes: z.record(z.string(), NodeRef),
  groups: z.record(z.string(), z.number()),
});
export type EgoData = z.output<typeof EgoData>;
const EgoResponse = envelopeOf(EgoData);

const NodesResponse = envelopeOf(
  z.looseObject({
    query: z.string(),
    hits: z.array(z.looseObject({ node: NodeRef, match: z.string(), indexed: z.boolean() })),
  }),
);
export type NodeHit = { node: NodeRef; match: string; indexed: boolean };

/** /v1/v8/search hit (V8API interface note, docs/dev/v8_api_interface.md); tolerant. */
const V8SearchResponse = envelopeOf(
  z.looseObject({
    hits: z.array(
      z.looseObject({
        id: z.string(),
        kind: z.string(),
        label: z.string(),
        sublabel: z.string().nullish(),
        entity_kind: z.string().nullish(),
        extra: z.record(z.string(), z.unknown()).nullish(),
      }),
    ),
  }),
);

export interface PathQuery {
  from: string;
  to: string;
  families: string[];
  causal: boolean;
  hops: number;
  k?: number;
}

export function fetchPaths(q: PathQuery, signal?: AbortSignal): Promise<PathsResponse> {
  return v3get(
    '/v1/v8/paths',
    PathsResponse,
    {
      from: q.from,
      to: q.to,
      families: q.families.length ? q.families.join(',') : undefined,
      include_causal: q.causal,
      max_hops: q.hops,
      k: q.k ?? 6,
    },
    signal,
  );
}

export function fetchNeighbourhood(id: string, opts: { families?: string[]; causal?: boolean; limit?: number }, signal?: AbortSignal) {
  return v3get(
    '/v1/v8/neighbourhood',
    EgoResponse,
    {
      id,
      families: opts.families?.length ? opts.families.join(',') : undefined,
      include_causal: opts.causal ?? true,
      limit: opts.limit ?? 14,
    },
    signal,
  );
}

export async function searchNodes(q: string, signal?: AbortSignal): Promise<NodeHit[]> {
  const r = await v3get('/v1/v8/paths/nodes', NodesResponse, { q, limit: 12 }, signal);
  return r.data.hits;
}

/** Universal search (V8API) mapped to path nodes; [] when the service is absent or its index is not built. */
export async function searchUniversal(q: string, signal?: AbortSignal): Promise<NodeHit[]> {
  try {
    const r = await v3get('/v1/v8/search', V8SearchResponse, { q, kinds: 'country,region,entity', limit: 8 }, signal);
    return r.data.hits.map((h) => {
      const qid = typeof h.extra?.qid === 'string' ? h.extra.qid : null;
      const id = h.kind === 'country' ? (qid ?? h.id) : h.id;
      return {
        node: {
          id,
          label: h.label,
          label_en: null,
          label_loaded: true,
          kind: h.entity_kind ?? h.kind,
          kind_label: h.sublabel ?? null,
          virtual: false,
          scope_iso3: null,
          fiche: null,
          degree: null,
        },
        match: 'v8search',
        indexed: true,
      };
    });
  } catch {
    return [];
  }
}

export interface FamilyInfo {
  id: string;
  label: string;
  group: string;
}

const FamiliesResponse = envelopeOf(
  z.looseObject({
    families: z.array(z.looseObject({ id: z.string(), label: Labels, group: z.string(), mappings: z.number().default(0) })),
  }),
);

let familiesMemo: Promise<FamilyInfo[]> | null = null;
/** The relation-family registry (GET /v1/kg/families), once per page load. */
export function fetchFamilies(): Promise<FamilyInfo[]> {
  if (!familiesMemo) {
    familiesMemo = v3get('/v1/kg/families', FamiliesResponse).then((r) =>
      r.data.families.filter((f) => f.mappings > 0).map((f) => ({ id: f.id, label: f.label.fr, group: f.group })),
    );
    familiesMemo.catch(() => {
      familiesMemo = null;
    });
  }
  return familiesMemo;
}

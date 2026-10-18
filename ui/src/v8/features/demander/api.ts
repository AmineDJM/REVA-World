/**
 * Client of /v1/v8/ask/* (src/reva/api/routes/v8_ask.py, agent V8ASK).
 *
 * Every number stays the exact decimal string the evidence carries (never a JSON number); schemas are tolerant on
 * extra fields and strict on the honesty-critical members (status, headline kind, citations, sentence kinds).
 */
import { z } from 'zod';
import { JsonRecord, v3get, v3post } from '../../../v3/api/common';

const nstr = z.string().nullish().transform((v) => v ?? null);
const Dec = z.union([z.string(), z.number().transform((n) => String(n))]).nullish().transform((v) => v ?? null);

export const Sentence = z.looseObject({
  id: z.string(),
  text: z.string(),
  kind: z.string().default('fact'),
  citations: z.array(z.string()).default([]),
  citation_refs: z.array(z.string()).default([]),
});
export type Sentence = z.output<typeof Sentence>;

export const Evidence = z.looseObject({
  id: z.string(),
  kind: z.string(),
  citation: JsonRecord.nullish().transform((v) => v ?? {}),
});
export type Evidence = z.output<typeof Evidence>;

export const Headline = z.looseObject({
  kind: z.enum(['number', 'sentence', 'partial', 'unknown']),
  text: z.string(),
  value: Dec,
  unit: nstr,
  period: nstr,
  iso3: nstr,
  concept: nstr,
  concept_label: nstr,
  honesty: nstr,
  status: nstr,
  model: nstr,
  band: z.array(Dec).nullish().transform((v) => v ?? null),
  more: z.number().nullish(),
  count: z.number().nullish(),
  detail: nstr,
  missing: z.array(z.string()).default([]),
  citations: z.array(z.string()).default([]),
});
export type Headline = z.output<typeof Headline>;

export const Source = z.looseObject({
  source: z.string(),
  licence: nstr,
  n: z.number().default(0),
  kinds: z.array(z.string()).default([]),
  non_commercial: z.boolean().default(false),
  restricted: z.boolean().default(false),
  community: z.boolean().default(false),
  tiers: z.array(z.string()).default([]),
  outside_snapshot: z.boolean().default(false),
});
export type Source = z.output<typeof Source>;

export const Target = z.looseObject({
  kind: z.enum(['fiche', 'monde', 'comparer', 'liens', 'decision', 'scores']),
  label: nstr,
  id: nstr,
  entity_kind: nstr,
  table: nstr,
  indicator: nstr,
  series: nstr,
  iso3: nstr,
  period: nstr,
  ref: nstr,
  track: nstr,
  from: nstr,
  to: nstr,
});
export type Target = z.output<typeof Target>;

export const Followup = z.looseObject({
  label: z.string(),
  q: nstr,
  brainstorm: nstr,
  target: Target.nullish().transform((v) => v ?? null),
});
export type Followup = z.output<typeof Followup>;

export const Viz = z.looseObject({ type: z.string() });
export type Viz = z.output<typeof Viz> & Record<string, unknown>;

export const How = z.looseObject({
  planner: nstr,
  prompt_version: nstr,
  restatement: nstr,
  operations: z.array(z.looseObject({ op: z.string(), args: JsonRecord.default({}) })).default([]),
  rejected: z.array(JsonRecord).default([]),
  tables: z.array(z.looseObject({ table: z.string(), n: z.number() })).default([]),
  periods: z.looseObject({ first: z.string(), last: z.string() }).nullish().transform((v) => v ?? null),
  as_of: JsonRecord.default({}),
  evidence_count: z.number().default(0),
  latency_ms: z.number().nullish(),
  warnings: z.array(z.string()).default([]),
});
export type How = z.output<typeof How>;

export const AnswerBundle = z.looseObject({
  question: z.string(),
  language: nstr,
  status: z.enum(['answered', 'partial', 'unknown']),
  headline: Headline,
  missing: z.array(z.string()).default([]),
  viz: Viz.nullish().transform((v) => (v ?? null) as Viz | null),
  how: How,
  sources: z.array(Source).default([]),
  targets: z.array(Target).default([]),
  followups: z.array(Followup).default([]),
  pin: JsonRecord.default({}),
  sentences: z.array(Sentence),
  evidence: z.array(Evidence).default([]),
  answer_text: nstr,
  warnings: z.array(z.string()).default([]),
  latency_ms: z.number().nullish(),
  cached: z.boolean().default(false),
});
export type AnswerBundle = z.output<typeof AnswerBundle>;

const SeriesStatus = z.looseObject({
  concept: z.string(),
  label: nstr,
  unit: nstr,
  status: z.string(),
  last_period: nstr,
  source: nstr,
  n: z.number().nullish(),
  stale: z.boolean().nullish(),
  projections_to: nstr,
  outside_snapshot: z.boolean().nullish(),
});
export type SeriesStatus = z.output<typeof SeriesStatus>;

export const Driver = z.looseObject({
  id: z.string(),
  kind: nstr,
  label: z.string(),
  effects: z.array(z.string()).default([]),
  n: z.number(),
  signs: z.record(z.string(), z.number()).default({}),
  methods: z.record(z.string(), z.number()).default({}),
  references: z.array(z.string()).default([]),
  countries: z.array(z.string()).default([]),
  covers_country: z.boolean().default(false),
  example: z.looseObject({ estimate_key: nstr, wording_fr: nstr }).nullish(),
  data: z.looseObject({
    link: nstr,
    series: z.array(SeriesStatus).default([]),
    candidates: z.array(z.looseObject({ concept: z.string(), label: nstr, curated: z.boolean().default(false) })).default([]),
    queued: z.array(z.looseObject({ file: z.string(), line: z.number(), text: z.string() })).default([]),
  }),
});
export type Driver = z.output<typeof Driver>;

export const BrainstormBundle = z.looseObject({
  target: z.looseObject({ target: z.string(), text: z.string(), iso3: nstr, horizon: z.looseObject({ n: z.number(), unit: z.string() }).nullish() }),
  known: z.boolean(),
  anchors: z.looseObject({
    families: z.array(z.looseObject({ key: z.string(), label: nstr })).default([]),
    concepts: z.array(z.looseObject({ id: z.string(), label: nstr, unit: nstr })).default([]),
    scoreboard: z.array(z.looseObject({ id: z.string(), label: nstr })).default([]),
    effect_variables: z.array(z.looseObject({ id: z.string(), kind: nstr, labels: z.record(z.string(), z.string()).default({}), match: nstr })).default([]),
  }),
  target_series: z.array(SeriesStatus).default([]),
  drivers: z.array(Driver).default([]),
  claims: z.array(JsonRecord).default([]),
  forecasts: z
    .array(
      z.looseObject({
        family: z.string(),
        label: nstr,
        covers: z.boolean(),
        geo: nstr,
        n_questions: z.number(),
        statuses: z.record(z.string(), z.number()).default({}),
        periods: z.looseObject({ first: z.string(), last: z.string() }).nullish(),
        models: z.array(z.looseObject({ model: z.string(), method: nstr })).default([]),
      }),
    )
    .default([]),
  verdicts: z
    .array(
      z.looseObject({
        target: z.string(),
        label: nstr,
        verdicts: z.record(z.string(), z.number()).default({}),
        n_rows: z.number().default(0),
        examples: z.array(JsonRecord).default([]),
      }),
    )
    .default([]),
  next_steps: z.array(z.looseObject({ rank: z.number(), kind: z.string(), text: z.string() })).default([]),
  method: z.string(),
  as_of: z.string(),
  latency_ms: z.number().nullish(),
});
export type BrainstormBundle = z.output<typeof BrainstormBundle>;

export const Examples = z.looseObject({ questions: z.array(z.string()).default([]), brainstorms: z.array(z.string()).default([]) });
export type Examples = z.output<typeof Examples>;

const BASE = '/v1/v8/ask';

export function askAnswer(question: string, signal?: AbortSignal): Promise<AnswerBundle> {
  return v3post(`${BASE}/answer`, AnswerBundle, { question }, signal);
}

export function brainstorm(target: string, signal?: AbortSignal): Promise<BrainstormBundle> {
  return v3get(`${BASE}/brainstorm`, BrainstormBundle, { target }, signal);
}

export function examples(signal?: AbortSignal): Promise<Examples> {
  return v3get(`${BASE}/examples`, Examples, undefined, signal);
}

/**
 * Decision-studio client (agent V8DECISION, src/reva/api/routes/v8_decision.py):
 *   GET  /v1/v8/decision/templates    reference decisions, instruments, schema limits, comparison presets
 *   POST /v1/v8/decision/run          one scenario: engine result + headline, graph, evidence, conflicts, diagnosis
 *   POST /v1/v8/decision/sensitivity  one-at-a-time tornado + Monte-Carlo variance shares
 *   POST /v1/v8/decision/compare      A vs B, paired difference (common random numbers)
 *   POST /v1/v8/decision/brief        Markdown brief with every citation
 *
 * The engine result is validated with the V5 contract (`Prediction`: a label and validation status are required,
 * an unquantified link must not carry a number). Bundle parts are tolerant about extra fields; a response that
 * does not match is a ContractError and nothing is drawn.
 */
import { z } from 'zod';
import { request } from '../../../api/http';
import { ApiWarning, parseWith } from '../../../v3/api/common';
import { DecisionDoc, Prediction } from '../../../v5/decision/api';

const Num = z.number().finite();
const NumN = Num.nullish();

// ------------------------------------------------------------------ templates
export const InstrumentT = z.looseObject({
  id: z.string(),
  label_fr: z.string(),
  magnitude_units: z.array(z.string()).min(1),
  unit_labels_fr: z.record(z.string(), z.string()).optional(),
  direct_changes: z.array(z.looseObject({ geo: z.string(), basis_fr: z.string() })),
  needs: z.looseObject({
    actor_geo: z.boolean(),
    target_geo: z.boolean(),
    countries: z.boolean(),
    occurrence: z.boolean(),
    level: z.boolean(),
  }),
  market: z.boolean().optional(),
});
export type InstrumentT = z.output<typeof InstrumentT>;

export const Schema = z.looseObject({
  id_pattern: z.string(),
  start_pattern: z.string(),
  iso3_pattern: z.string(),
  magnitude_pattern: z.string(),
  label_max: z.number(),
  party_label_max: z.number(),
  horizon_min: z.number(),
  horizon_max: z.number(),
  duration_min: z.number(),
  duration_max: z.number(),
  countries_max: z.number(),
  draws_min: z.number(),
  draws_max: z.number(),
});
export type Schema = z.output<typeof Schema>;

export const Templates = z.looseObject({
  label: z.string().min(1),
  disclaimers_fr: z.array(z.string()),
  not_advice_fr: z.string(),
  references: z.array(
    z.looseObject({ id: z.string(), decision: DecisionDoc, summary_fr: z.string(), instrument_label_fr: z.string(), market: z.boolean().optional() }),
  ),
  instruments: z.array(InstrumentT),
  schema: Schema,
  compare_presets: z.array(z.looseObject({ id: z.string(), label_fr: z.string(), a: DecisionDoc, b_reference_id: z.string(), note_fr: z.string().nullish() })),
  engine: z.looseObject({ state: z.string(), detail: z.string().nullish(), seconds: Num.nullish() }),
});
export type Templates = z.output<typeof Templates>;

// ------------------------------------------------------------------ run bundle
const Value = z
  .looseObject({
    period: z.string().nullable(),
    period_label_fr: z.string(),
    p05: NumN,
    p25: NumN,
    p50: NumN,
    p75: NumN,
    p95: NumN,
    mean: NumN,
    degenerate: z.boolean().nullish(),
  })
  .nullable();
export type Value = z.output<typeof Value>;

export const HeadlineRow = z.looseObject({
  id: z.string(),
  label_fr: z.string(),
  geo: z.string(),
  geo_fr: z.string(),
  unit: z.string(),
  unit_fr: z.string(),
  kind: z.string(),
  frequency: z.string(),
  depth: z.number().nullish(),
  value: Value,
  n_periods: z.number(),
  n_missing: z.number(),
  flags: z.array(z.looseObject({ code: z.string(), label_fr: z.string(), title_fr: z.string().nullish() })),
  sources: z.array(z.looseObject({ estimate_key: z.string(), short: z.string() })),
  models: z.array(z.string()),
  notes_fr: z.array(z.string()),
});
export type HeadlineRow = z.output<typeof HeadlineRow>;

export const GraphNode = z.looseObject({
  id: z.string(),
  kind: z.enum(['decision', 'direct', 'variable', 'plugin', 'stop', 'tie', 'unquantified']),
  label: z.string(),
  geo: z.string(),
  depth: z.number().nullish(),
  value: Value.optional(),
  unit: z.string().nullish(),
  chain_end: z.boolean().optional(),
  blocked: z.boolean().optional(),
  reasons: z.array(z.string()).optional(),
});
export type GraphNode = z.output<typeof GraphNode>;

export const EdgeEffect = z.looseObject({
  beta: NumN,
  beta_sd: NumN,
  beta_ci90: z.tuple([Num, Num]).nullish(),
  uncertainty_basis: z.string().nullish(),
  in_unit: z.string().nullish(),
  out_unit: z.string().nullish(),
  beta_label_fr: z.string(),
  published_fr: z.string().nullish(),
  published_uncertainty_fr: z.string().nullish(),
  has_uncertainty: z.boolean(),
});

export const GraphEdge = z.looseObject({
  id: z.string(),
  from: z.string(),
  to: z.string(),
  kind: z.enum(['assumption', 'estimate', 'plugin', 'model_output', 'missing', 'tie', 'sign_only']),
  label: z.string(),
  estimate_key: z.string().nullish(),
  estimate_keys: z.array(z.string()).optional(),
  source_short: z.string().nullish(),
  method: z.string().nullish(),
  effect: EdgeEffect.optional(),
  applicability: z.looseObject({ status: z.string(), notes: z.array(z.string()).optional(), geo: z.string().optional() }).nullish(),
  applicability_notes_fr: z.array(z.string()).optional(),
  notes_fr: z.array(z.string()).optional(),
  n_alternatives: z.number().optional(),
  restricted: z.boolean().optional(),
  basis_fr: z.string().nullish(),
  assumption_fr: z.string().nullish(),
});
export type GraphEdge = z.output<typeof GraphEdge>;

export const Evidence = z.looseObject({
  estimate_key: z.string(),
  short: z.string(),
  source_kind: z.string().nullish(),
  citation: z.string().nullish(),
  doi: z.string().nullish(),
  url: z.string().nullish(),
  locator: z.string().nullish(),
  quote: z.string().nullish(),
  transcription: z.string().nullish(),
  run: z.looseObject({ run_id: z.string().nullish(), as_of: z.string().nullish(), code_version: z.string().nullish() }).nullish(),
  rights: z.looseObject({ licence: z.string().nullish(), restricted: z.boolean().nullish(), non_commercial: z.boolean().nullish() }).nullish(),
  restricted: z.boolean(),
  non_commercial: z.boolean(),
  as_of: z.union([z.string(), z.number()]).nullish(),
  method: z.string().nullish(),
  identification: z.string().nullish(),
  wording_fr: z.string().nullish(),
  applicability_note: z.string().nullish(),
  source_caveats: z.unknown().optional(),
  curation_review: z.string().nullish(),
  origin: z.string().nullish(),
  point: NumN,
  unit: z.string().nullish(),
  published_size_fr: z.string().nullish(),
  published_uncertainty_fr: z.string(),
  loaded: z.boolean(),
});
export type Evidence = z.output<typeof Evidence>;

export const ConflictRow = z.looseObject({
  estimate_key: z.string(),
  short: z.string(),
  published_fr: z.string().nullish(),
  published_uncertainty_fr: z.string().nullish(),
  point: NumN,
  point_used: NumN,
  unit: z.string().nullish(),
  method: z.string().nullish(),
  chosen: z.boolean(),
  applicability: z.string().nullish(),
  reason_fr: z.string().nullish(),
  effect_label_fr: z.string().nullish(),
  loaded: z.boolean(),
});
export const Conflict = z.looseObject({
  kind: z.enum(['alternatives', 'tie']),
  title_fr: z.string(),
  edges: z.array(z.string()),
  nodes: z.array(z.string().nullable()),
  estimates: z.array(ConflictRow),
  message_fr: z.string().nullish(),
});
export type Conflict = z.output<typeof Conflict>;

const DiagItem = z.looseObject({
  code: z.string(),
  node: z.string().nullish(),
  node_label: z.string().nullish(),
  title_fr: z.string(),
  detail_fr: z.string(),
  missing_fr: z.string().nullish(),
  missing: z.array(z.string()).optional(),
  unblock_fr: z.string().nullish(),
  plugin: z.string().nullish(),
  near: z.array(z.looseObject({ estimate_key: z.string().nullish(), cause: z.string().nullish(), effect: z.string().nullish(), wording_fr: z.string().nullish(), relation: z.string().nullish() })).optional(),
  estimate_keys: z.array(z.string()).optional(),
});
export type DiagItem = z.output<typeof DiagItem>;

export const Diagnosis = z.looseObject({
  has_result: z.boolean(),
  headline_fr: z.string(),
  blocking: z.array(DiagItem),
  ties: z.array(DiagItem),
  plugins: z.array(DiagItem),
  chain_ends: z.array(z.looseObject({ node: z.string().nullish(), label: z.string().nullish(), geo: z.string().nullish() })),
  missing_periods: z.array(z.looseObject({ variable: z.string(), label: z.string(), geo: z.string().nullish(), periods: z.array(z.string()), reason_fr: z.string() })),
  other: z.array(DiagItem),
});
export type Diagnosis = z.output<typeof Diagnosis>;

export const RunBundle = z.looseObject({
  scenario_hash: z.string(),
  cached: z.boolean(),
  compute_ms: Num,
  result: Prediction,
  headline: z.array(HeadlineRow),
  graph: z.looseObject({ nodes: z.array(GraphNode), edges: z.array(GraphEdge), geos: z.array(z.string()) }),
  evidence: z.record(z.string(), Evidence),
  conflicts: z.array(Conflict),
  diagnosis: Diagnosis,
  honesty: z.looseObject({
    label: z.string().min(1),
    validated: z.boolean(),
    status: z.string().nullish(),
    disclaimers_fr: z.array(z.string()),
    market: z.boolean(),
    not_advice_fr: z.string(),
    judge_requirements: z.array(z.string()).optional(),
  }),
  labels: z.record(z.string(), z.string()),
});
export type RunBundle = z.output<typeof RunBundle>;

// ------------------------------------------------------------------ sensitivity
const PerPeriod = z.record(z.string(), Num.nullable());
export const SensVar = z.looseObject({
  id: z.string(),
  label_fr: z.string(),
  geo: z.string(),
  unit: z.string(),
  frequency: z.string(),
  periods: z.array(z.string()),
  headline_period: z.string().nullable(),
  center: PerPeriod,
  bars: z.array(
    z.looseObject({ estimate_key: z.string(), short: z.string(), low: PerPeriod, high: PerPeriod, swing: PerPeriod, mc_r2: PerPeriod }),
  ),
  no_uncertainty: z.array(z.looseObject({ estimate_key: z.string(), short: z.string() })),
});
export type SensVar = z.output<typeof SensVar>;
export const Sensitivity = z.looseObject({
  method: z.literal('one_at_a_time'),
  method_fr: z.string(),
  z: Num,
  center_label_fr: z.string().nullish(),
  variables: z.array(SensVar),
  runs: z.number(),
  compute_ms: Num,
  label: z.string().min(1),
  cached: z.boolean(),
});
export type Sensitivity = z.output<typeof Sensitivity>;

// ------------------------------------------------------------------ compare
const Q3 = z.looseObject({ p05: NumN, p50: NumN, p95: NumN, degenerate: z.boolean().nullish() }).nullable();
export const CompareVar = z.looseObject({
  id: z.string(),
  label_fr: z.string().nullish(),
  geo: z.string().nullish(),
  unit: z.string().nullish(),
  frequency: z.string().nullish(),
  in_a: z.boolean(),
  in_b: z.boolean(),
  periods: z.array(
    z.looseObject({
      period: z.string(),
      a: Q3,
      b: Q3,
      a_missing: z.string().nullish(),
      b_missing: z.string().nullish(),
      diff: z.looseObject({ p05: Num, p25: Num, p50: Num, p75: Num, p95: Num, degenerate: z.boolean() }).nullable(),
      diff_note_fr: z.string().nullish(),
    }),
  ),
});
export type CompareVar = z.output<typeof CompareVar>;
const CompareSide = z.looseObject({
  scenario_hash: z.string(),
  decision: z.looseObject({ id: z.string(), label_fr: z.string() }),
  label: z.string().min(1),
  headline: z.array(HeadlineRow),
  diagnosis: z.looseObject({ has_result: z.boolean(), headline_fr: z.string() }),
});
export const Comparison = z.looseObject({
  paired: z.boolean(),
  paired_fr: z.string(),
  changed_fields: z.array(z.string()),
  variables: z.array(CompareVar),
  a: CompareSide,
  b: CompareSide,
});
export type Comparison = z.output<typeof Comparison>;

export const Brief = z.looseObject({ markdown: z.string().min(1), filename: z.string(), scenario_hash: z.string(), label: z.string(), n_citations: z.number() });
export type Brief = z.output<typeof Brief>;

// ------------------------------------------------------------------ calls
const env = <D extends z.ZodType>(d: D) => z.looseObject({ data: d, warnings: z.array(ApiWarning).nullish() });

export interface ScenarioBody {
  reference_id?: string;
  decision?: Record<string, unknown>;
  draws?: number;
  seed?: number;
}

export async function fetchTemplates(signal?: AbortSignal): Promise<Templates> {
  const res = await request<unknown>('GET', '/v1/v8/decision/templates', { signal });
  return parseWith(env(Templates), res.body, '/v1/v8/decision/templates').data;
}

export async function runScenario(body: ScenarioBody, signal?: AbortSignal): Promise<{ bundle: RunBundle; warnings: ApiWarning[] }> {
  const res = await request<unknown>('POST', '/v1/v8/decision/run', { body, signal });
  const e = parseWith(env(RunBundle), res.body, '/v1/v8/decision/run');
  return { bundle: e.data, warnings: e.warnings ?? [] };
}

export async function fetchSensitivity(body: ScenarioBody, signal?: AbortSignal): Promise<Sensitivity> {
  const res = await request<unknown>('POST', '/v1/v8/decision/sensitivity', { body, signal });
  return parseWith(env(Sensitivity), res.body, '/v1/v8/decision/sensitivity').data;
}

export async function fetchComparison(a: ScenarioBody, b: ScenarioBody, signal?: AbortSignal): Promise<Comparison> {
  const res = await request<unknown>('POST', '/v1/v8/decision/compare', { body: { a, b }, signal });
  return parseWith(env(Comparison), res.body, '/v1/v8/decision/compare').data;
}

export async function fetchBrief(body: ScenarioBody & { url?: string; sensitivity?: boolean }, signal?: AbortSignal): Promise<Brief> {
  const res = await request<unknown>('POST', '/v1/v8/decision/brief', { body, signal });
  return parseWith(env(Brief), res.body, '/v1/v8/decision/brief').data;
}

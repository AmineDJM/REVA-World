/**
 * Decision-engine client (agent PRED-ENGINE, src/reva/api/routes/predict.py):
 *   GET  /v1/predict/decisions   reference decisions + instruments to compose a decision
 *   POST /v1/predict/decision    propagate one decision (reference_id or composed decision), read-only
 *
 * Tolerant about extra fields, strict about honesty: a result must carry its label and validation status; every
 * estimate hop must name its estimate key and source; an unquantified link must not carry a number. Otherwise the
 * response is a ContractError and nothing is drawn.
 */
import { z } from 'zod';
import { request } from '../../api/http';
import { ApiWarning, parseWith } from '../../v3/api/common';

const Num = z.number().finite();

export const Quantiles = z.looseObject({
  p05: Num,
  p25: Num,
  p50: Num,
  p75: Num,
  p95: Num,
  mean: Num,
  degenerate: z.boolean(),
});

export const PeriodValue = z.looseObject({
  period: z.string(),
  missing_reason: z.string().nullable(),
  p05: Num.nullish(),
  p25: Num.nullish(),
  p50: Num.nullish(),
  p75: Num.nullish(),
  p95: Num.nullish(),
  mean: Num.nullish(),
  degenerate: z.boolean().nullish(),
  scenario_level: z.looseObject({ p05: Num, p50: Num, p95: Num, note: z.string().nullish() }).nullish(),
});
export type PeriodValue = z.output<typeof PeriodValue>;

export const VariableResult = z.looseObject({
  id: z.string(),
  variable: z.looseObject({ kind: z.string(), id: z.string(), label_fr: z.string().nullish(), label_en: z.string().nullish() }),
  geo: z.string(),
  unit: z.string(),
  kind: z.enum(['estimate_chain', 'plugin_output']),
  frequency: z.enum(['quarter', 'year', 'undated']),
  depth: z.number().int(),
  path: z.array(z.string()),
  periods: z.array(PeriodValue),
  undated: Quantiles.extend({ kind: z.string().nullish(), kind_label_fr: z.string().nullish() }).nullish(),
  baseline: z.looseObject({ value: z.string().nullish(), unit: z.string().nullish(), year: z.number().nullish(), source: z.string().nullish(), label_fr: z.string().nullish() }).nullish(),
  uncertainty_complete: z.boolean(),
  point_estimates: z.array(z.string()),
  notes: z.array(z.string()),
  model: z.string().nullish(),
});
export type VariableResult = z.output<typeof VariableResult>;

const Source = z.looseObject({
  estimate_key: z.string(),
  source_kind: z.string().nullish(),
  source_key: z.string().nullish(),
  citation: z.string().nullish(),
  doi: z.string().nullish(),
  url: z.string().nullish(),
  locator: z.string().nullish(),
  quote: z.string().nullish(),
  wording_fr: z.string().nullish(),
  applicability_note: z.string().nullish(),
});

const DecisionHop = z.looseObject({ id: z.string(), kind: z.literal('decision'), to: z.string(), direct_change: z.looseObject({ basis_fr: z.string() }) });
const EstimateHop = z.looseObject({
  id: z.string(),
  kind: z.literal('estimate'),
  from: z.string(),
  to: z.string(),
  estimate_key: z.string(),
  source: Source,
  coefficient: z.looseObject({ point: Num, sd: Num.nullable(), uncertainty_basis: z.string().nullable(), in_unit: z.string().nullish(), out_unit: z.string().nullish() }),
  applicability: z.looseObject({ status: z.string(), notes: z.array(z.string()), geo: z.string() }),
  alternatives: z.array(z.looseObject({ estimate_key: z.string(), reason_fr: z.string() })),
  notes: z.array(z.string()),
});
const PluginHop = z.looseObject({
  id: z.string(),
  kind: z.literal('plugin'),
  from: z.string(),
  to: z.array(z.string()),
  model: z.string(),
  label_fr: z.string(),
  assumption_fr: z.string(),
  notes: z.array(z.string()),
});
export const Hop = z.discriminatedUnion('kind', [DecisionHop, EstimateHop, PluginHop]);
export type Hop = z.output<typeof Hop>;
export type EstimateHop = z.output<typeof EstimateHop>;

/** A link without a number: a field named like a quantity is a contract violation. */
export const Unquantified = z
  .looseObject({
    kind: z.enum(['causal_claim', 'sign_only_estimate']),
    wording_fr: z.string().nullish(),
    note_fr: z.string(),
    sign: z.string().nullish(),
    estimate_key: z.string().nullish(),
    claim_key: z.string().nullish(),
    grade: z.string().nullish(),
    source: z.looseObject({ source_key: z.string().nullish(), tier: z.string().nullish() }).nullish(),
  })
  .refine((u) => !('point' in u) && !('p50' in u) && !('value' in u), { message: 'un lien non chiffré ne doit porter aucun nombre' });
export type Unquantified = z.output<typeof Unquantified>;

export const Prediction = z.looseObject({
  engine: z.string(),
  label: z.string().min(1),
  validation: z.looseObject({
    status: z.enum(['unvalidated', 'validated']),
    label: z.string(),
    chain_signature: z.string(),
    reason: z.string().nullish(),
    judge_requirements: z.array(z.string()).nullish(),
  }),
  disclaimers_fr: z.array(z.string()),
  decision: z.looseObject({ id: z.string(), label_fr: z.string() }),
  settings: z.looseObject({ draws: z.number(), seed: z.number(), periods: z.array(z.string()) }),
  direct_changes: z.array(z.looseObject({ basis_fr: z.string(), geo: z.string(), change: z.string(), unit: z.string(), variable: z.looseObject({ id: z.string() }) })),
  variables: z.array(VariableResult),
  hops: z.array(Hop),
  unquantified_links: z.array(Unquantified),
  excluded_links: z.array(z.looseObject({ code: z.string(), estimate_key: z.string(), detail: z.string(), geo: z.string().nullish() })),
  alternative_paths: z.array(z.looseObject({ to: z.string(), estimate_key: z.string(), reason_fr: z.string() })),
  gaps: z.array(z.looseObject({ code: z.string() })),
  effect_layer: z.looseObject({ layer: z.string(), detail: z.string().nullish() }),
  chain: z.array(z.string()),
});
export type Prediction = z.output<typeof Prediction>;

const PredictionEnvelope = z.looseObject({ data: Prediction, warnings: z.array(ApiWarning).nullish() });

export const Instrument = z.looseObject({
  id: z.string(),
  label_fr: z.string(),
  magnitude_units: z.array(z.string()).min(1),
  direct_changes: z.array(z.looseObject({ geo: z.string(), basis_fr: z.string() })),
});
export type Instrument = z.output<typeof Instrument>;

export const DecisionDoc = z.looseObject({
  id: z.string(),
  label_fr: z.string(),
  actor: z.looseObject({ label_fr: z.string(), geo: z.string().nullish(), kg_id: z.string().nullish() }),
  instrument: z.string(),
  target: z.looseObject({ label_fr: z.string(), geo: z.string().nullish(), kg_id: z.string().nullish() }),
  magnitude: z.looseObject({ value: z.string(), unit: z.string() }),
  start: z.string(),
  duration_quarters: z.number().int().nullable(),
  horizon_quarters: z.number().int(),
  countries: z.array(z.string()),
});
export type DecisionDoc = z.output<typeof DecisionDoc>;

const DecisionsEnvelope = z.looseObject({
  data: z.looseObject({ decisions: z.array(DecisionDoc), instruments: z.array(Instrument), label: z.string() }),
});

export async function fetchDecisions(signal?: AbortSignal) {
  const res = await request<unknown>('GET', '/v1/predict/decisions', { signal });
  return parseWith(DecisionsEnvelope, res.body, '/v1/predict/decisions').data;
}

export interface PredictBody {
  reference_id?: string;
  decision?: Record<string, unknown>;
  draws?: number;
  seed?: number;
}

export async function predictDecision(body: PredictBody, signal?: AbortSignal) {
  const res = await request<unknown>('POST', '/v1/predict/decision', { body, signal });
  const env = parseWith(PredictionEnvelope, res.body, '/v1/predict/decision');
  return { prediction: env.data, warnings: env.warnings ?? [] };
}

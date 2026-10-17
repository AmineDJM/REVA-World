/**
 * Client of the prospective forecast registry (src/reva/api/routes/forecasts.py, /v1/forecasts/*).
 * Tolerant zod schemas; frozen forecast values are decimal strings (never JSON numbers that could hide a
 * filled-in value), aggregate scores are numbers or null (null = not applicable, never 0).
 */
import { z } from 'zod';
import { envelopeOf, JsonRecord, v3get } from './common';

const NumOrNull = z.number().nullable();

export const ForecastFamily = z.looseObject({
  key: z.string(),
  label_fr: z.string(),
  label_en: z.string(),
  definition: z.string().nullable(),
  dataset_key: z.string().nullable(),
  frequency: z.enum(['M', 'Q', 'A']),
  unit: z.string(),
  question_types: z.array(z.string()),
  horizons: z.array(z.number()),
  countries: z.array(z.string()),
  status: z.enum(['available', 'awaiting_table', 'awaiting_source']),
  status_reason: z.string().nullable(),
});
export type ForecastFamily = z.output<typeof ForecastFamily>;

export const Batch = z.looseObject({
  batch_sha256: z.string(),
  cycle: z.string().nullable(),
  origin: z.string().nullable(),
  issued_at: z.string().nullable(),
  n_questions: z.number().nullable(),
  by_type: z.record(z.string(), z.number()),
  models: z.array(z.looseObject({ model: z.string().nullable(), n_issued: z.number().nullable(), n_abstained: z.number().nullable() })),
});
export type Batch = z.output<typeof Batch>;

export const QuestionSummary = z.looseObject({
  question_id: z.string(),
  cycle: z.string(),
  family: z.string(),
  label_fr: z.string(),
  geo: z.string(),
  geo_label_fr: z.string(),
  reference_period: z.string(),
  type: z.enum(['quantity', 'interval', 'binary']),
  unit: z.string(),
  threshold: z.string().nullable(),
  text_fr: z.string(),
  expected_resolution: z.string(),
  void_after: z.string(),
  resolution_source: z.string().nullable(),
  status: z.enum(['pending', 'resolved', 'void', 'not_prospective']),
  outcome: z.string().nullable(),
  n_forecasts: z.number(),
  batch_sha256: z.string(),
  issued_at: z.string().nullable(),
});
export type QuestionSummary = z.output<typeof QuestionSummary>;

const QuestionsPage = z.looseObject({ total: z.number(), offset: z.number(), questions: z.array(QuestionSummary) });

export const ForecastRecord = z.looseObject({
  model: z.string(),
  status: z.enum(['issued', 'abstained']),
  reason: z.string().nullish(),
  point: z.string().nullish(),
  interval: z.array(z.string()).nullish(),
  prob_above: z.string().nullish(),
  horizon_periods: z.number().nullish(),
  origin_period: z.string().nullish(),
  predictive: JsonRecord.nullish(),
  score: JsonRecord.nullish(),
  attribution: z.string().nullish(),
});
export type ForecastRecord = z.output<typeof ForecastRecord>;

export const QuestionDetail = z.looseObject({
  question: JsonRecord,
  forecasts: z.array(ForecastRecord),
  outcome: JsonRecord.nullable(),
  status: z.string(),
  seal: z.looseObject({ batch_sha256: z.string(), issued_at: z.string().nullable(), verified: z.boolean(), label: z.string(), data_tables: JsonRecord }),
});
export type QuestionDetail = z.output<typeof QuestionDetail>;

const Relative = z.looseObject({ n_common: z.number(), ratio: NumOrNull, skill: NumOrNull });

export const LeaderboardRow = z.looseObject({
  model: z.string(),
  description: z.string(),
  kind: z.enum(['baseline', 'external_reference', 'reva_ensemble']),
  n_issued: z.number(),
  n_abstained: z.number(),
  n_scored: z.number(),
  mae: NumOrNull,
  crps: NumOrNull,
  brier: NumOrNull,
  coverage: NumOrNull,
  interval_score: NumOrNull,
  mae_vs_persistence: Relative,
  crps_vs_persistence: Relative,
});
export type LeaderboardRow = z.output<typeof LeaderboardRow>;

const Leaderboard = z.looseObject({ label: z.string(), questions: z.record(z.string(), z.number()), models: z.array(LeaderboardRow) });

export const BacktestResult = z.looseObject({
  model: z.string(),
  horizon_periods: z.number(),
  n_scored: z.number(),
  mae: NumOrNull,
  crps: NumOrNull,
  coverage: NumOrNull,
  brier: NumOrNull,
  mae_vs_persistence: Relative,
  crps_vs_persistence: Relative,
});
export const Backtest = z.looseObject({
  backtest_sha256: z.string(),
  label: z.string(),
  vintage_mode: z.string().nullable(),
  run_at: z.string().nullable(),
  years: z.number().nullable(),
  families: z.array(z.looseObject({ family: z.string(), frequency: z.string(), horizons: z.array(z.number()), results: z.array(BacktestResult) })),
});
export type Backtest = z.output<typeof Backtest>;

export const Calibration = z.looseObject({
  model: z.string(),
  n_scored: z.number(),
  pit_histogram: z.looseObject({ bins: z.number(), counts: z.array(z.number()), n: z.number() }),
  interval_coverage: z.looseObject({ nominal: z.number(), empirical: NumOrNull, n: z.number() }),
});

const EnsembleCompetitor = z.looseObject({ model: z.string(), n_common: z.number(), ratio: NumOrNull });
export const EnsembleCell = z.looseObject({
  family: z.string(),
  type: z.string(),
  metric: z.string(),
  n_ensemble_issued: z.number(),
  n_ensemble_scored: z.number(),
  min_pairs: z.number(),
  competitors: z.array(EnsembleCompetitor),
  best_single: EnsembleCompetitor.nullable(),
  test: z.looseObject({ mean_diff: NumOrNull, p_two_sided: NumOrNull, n: z.number() }).nullable(),
  status: z.string(),
  p_holm: NumOrNull,
  holm_family_size: z.number(),
  verdict: z.string(),
});
export type EnsembleCell = z.output<typeof EnsembleCell>;
export const EnsembleBoard = z.looseObject({
  label: z.string(),
  model: z.string(),
  rule: z.string(),
  alpha: z.number(),
  holm_family_size: z.number(),
  n_ensemble_forecasts: z.number(),
  n_ensemble_scored: z.number(),
  weights_by_family: z.record(z.string(), z.record(z.string(), z.number())),
  cells: z.array(EnsembleCell),
});

export const getForecastFamilies = (s?: AbortSignal) => v3get('/v1/forecasts/families', envelopeOf(z.array(ForecastFamily)), undefined, s);
export const getForecastBatches = (s?: AbortSignal) => v3get('/v1/forecasts/batches', envelopeOf(z.array(Batch)), undefined, s);
export const getForecastQuestions = (q: { family?: string; type?: string; status?: string; geo?: string; limit?: number; offset?: number }, s?: AbortSignal) =>
  v3get('/v1/forecasts/questions', envelopeOf(QuestionsPage), q, s);
export const getForecastQuestion = (id: string, s?: AbortSignal) =>
  v3get(`/v1/forecasts/questions/${encodeURIComponent(id)}`, envelopeOf(QuestionDetail), undefined, s);
export const getLeaderboard = (s?: AbortSignal) => v3get('/v1/forecasts/leaderboard', envelopeOf(Leaderboard), undefined, s);
export const getBacktests = (s?: AbortSignal) => v3get('/v1/forecasts/backtests', envelopeOf(z.array(Backtest)), undefined, s);
export const getCalibration = (model: string, s?: AbortSignal) => v3get('/v1/forecasts/calibration', envelopeOf(Calibration), { model }, s);
export const getEnsembleBoard = (s?: AbortSignal) => v3get('/v1/forecasts/ensemble', envelopeOf(EnsembleBoard), undefined, s);

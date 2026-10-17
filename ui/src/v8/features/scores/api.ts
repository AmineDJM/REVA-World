/**
 * Client of the V8 scoreboard (src/reva/api/routes/v8_scores.py, /v1/v8/scores/*). Tolerant zod schemas
 * (unknown fields pass), strict where honesty depends on it: a verdict is one of four words, a ratio is a
 * number or absent (never 0 by default), counts are integers.
 */
import { z } from 'zod';
import { envelopeOf, v3get } from '../../../v3/api/common';

const Int = z.number().int();
const Num = z.number();
export const Verdict = z.enum(['bat', 'ne_bat_pas', 'egalite', 'en_attente']);
export type Verdict = z.output<typeof Verdict>;
const Counts = z.looseObject({ bat: Int, ne_bat_pas: Int, egalite: Int, en_attente: Int });
export type Counts = z.output<typeof Counts>;
const Quote = z.looseObject({ file: z.string(), text: z.string() });

export const Row = z.looseObject({
  id: z.string(),
  track: z.string(),
  window: z.string().nullish(),
  target: z.string(),
  series: z.string().nullish(),
  horizon: z.string().nullish(),
  horizon_label_fr: z.string().nullish(),
  forecaster: z.string().nullish(),
  benchmark: z.string().nullish(),
  benchmark_label_fr: z.string().nullish(),
  benchmark_kind: z.string(),
  candidate: z.string().nullish(),
  candidate_label_fr: z.string().nullish(),
  candidate_kind: z.string(),
  evaluation: z.string(),
  metric: z.string(),
  sample: z.record(z.string(), z.unknown()).nullish(),
  ratio: Num.nullish(),
  ratio_range: z.array(Num).nullish(),
  p_raw: Num.nullish(),
  p_holm: Num.nullish(),
  p_holm_basis: z.string().nullish(),
  holm_family_size: Int.nullish(),
  holm_rejected: z.boolean().nullish(),
  coverage: z.record(z.string(), Num.nullable()).nullish(),
  benchmark_coverage: z.record(z.string(), Num.nullable()).nullish(),
  verdict: Verdict,
  verdict_basis_fr: z.string().nullish(),
  verdict_quote: Quote.nullish(),
  caveats: z.array(z.string()).default([]),
  source: z.looseObject({ file: z.string(), pointer: z.string().nullish(), quote: z.string().nullish(), sha256: z.string().nullish() }),
  report: z.string().nullish(),
  protocol: z.looseObject({ file: z.string(), sha256: z.string().nullish() }).nullish(),
  resolution: z.looseObject({ date: z.string().nullish(), month: z.string().nullish(), not_before: z.string().nullish(), note_fr: z.string().nullish(), quote: Quote.nullish() }).nullish(),
  registry_model: z.string().nullish(),
  sealed: z.boolean().nullish(),
  quarantined: z.boolean().nullish(),
  problems: z.array(z.string()).nullish(),
});
export type Row = z.output<typeof Row>;

export const Cell = z.looseObject({
  key: z.string(),
  status: z.string(),
  target: z.string().nullish(),
  forecaster: z.string().nullish(),
  counts: Counts.nullish(),
  weak_reference: z.record(z.string(), Int).nullish(),
  n_rows: Int.nullish(),
  ratio_min: Num.nullish(),
  ratio_max: Num.nullish(),
  evaluations: z.array(z.string()).nullish(),
  tracks: z.array(z.string()).nullish(),
  wins: z.array(z.string()).nullish(),
  reason_fr: z.string().nullish(),
  quote: Quote.nullish(),
  partial: z.boolean().nullish(),
});
export type Cell = z.output<typeof Cell>;

export const Forecaster = z.looseObject({ id: z.string(), label_fr: z.string(), short: z.string(), org: z.string().nullish(), product: z.string().nullish(), domain: z.string().nullish(), status: z.string().nullish() });
export type Forecaster = z.output<typeof Forecaster>;

const MatrixTarget = z.looseObject({ target: z.string(), label_fr: z.string(), cells: z.record(z.string(), Cell), naive: Cell.nullish(), no_track: z.looseObject({ reason_fr: z.string(), quote: Quote.nullish() }).nullish() });
export type MatrixTarget = z.output<typeof MatrixTarget>;
const MatrixDomain = z.looseObject({ domain: z.string(), label_fr: z.string(), targets: z.array(MatrixTarget) });
export type MatrixDomain = z.output<typeof MatrixDomain>;

export const CalendarItem = z.looseObject({
  id: z.string().nullish(),
  kind: z.string(),
  track: z.string().nullish(),
  date: z.string().nullish(),
  month: z.string().nullish(),
  not_before: z.string().nullish(),
  sort: z.string().nullish(),
  note_fr: z.string().nullish(),
  quote: Quote.nullish(),
  rows: z.array(z.string()).default([]),
  n_rows: Int.default(0),
  forecasters: z.array(z.string()).default([]),
  sealed: z.boolean().nullish(),
});
export type CalendarItem = z.output<typeof CalendarItem>;

export const Track = z.looseObject({
  id: z.string(),
  label_fr: z.string(),
  agent: z.string(),
  version: z.string(),
  report: z.string().nullish(),
  summary_fr: z.string(),
  protocol: z.looseObject({ file: z.string(), sha256: z.string().nullish() }).nullish(),
  counts: Counts.nullish(),
  n_rows: Int.nullish(),
});
export type Track = z.output<typeof Track>;

const Win = z.looseObject({ id: z.string(), track: z.string(), target: z.string(), forecaster: z.string().nullish(), candidate_label_fr: z.string().nullish(), benchmark_label_fr: z.string().nullish(), candidate_kind: z.string(), evaluation: z.string(), metric: z.string(), ratio: Num.nullish(), p_holm: Num.nullish(), horizon_label_fr: z.string().nullish(), caveats: z.array(z.string()).default([]) });
export type Win = z.output<typeof Win>;

export const Vocab = z.looseObject({
  verdicts: z.record(z.string(), z.string()),
  evaluations: z.record(z.string(), z.string()),
  benchmark_kinds: z.record(z.string(), z.string()),
  candidate_kinds: z.record(z.string(), z.string()),
  metrics: z.record(z.string(), z.string()),
  caveats: z.record(z.string(), z.string()),
});
export type Vocab = z.output<typeof Vocab>;

export const ScoresPage = z.looseObject({
  headline: Counts,
  headline_rule_fr: z.string(),
  by_evaluation: z.record(z.string(), Counts),
  weak_reference: Counts,
  naive: Counts,
  n_rows: Int,
  wins: z.array(Win),
  forecasters: z.array(Forecaster),
  matrix: z.array(MatrixDomain),
  tracks: z.array(Track),
  calendar: z.array(CalendarItem),
  vocab: Vocab,
  sources: z.array(z.looseObject({ file: z.string(), sha256: z.string() })).default([]),
  stale_sources: z.array(z.looseObject({ file: z.string() })).default([]),
  problems: z.array(z.string()).default([]),
  quarantined: z.array(z.string()).default([]),
  sealed_models: z.record(z.string(), Int).nullish(),
});
export type ScoresPage = z.output<typeof ScoresPage>;

const Chart = z.looseObject({
  kind: z.string(),
  title_fr: z.string(),
  row: z.string().nullish(),
  x_label_fr: z.string().nullish(),
  points: z.array(z.looseObject({})).nullish(),
  counts: z.array(Int).nullish(),
  coverage80: Num.nullish(),
  n: Int.nullish(),
  model: z.string().nullish(),
  source: z.looseObject({ file: z.string() }).nullish(),
});
export type Chart = z.output<typeof Chart>;

export const TrackDetail = z.looseObject({
  track: z.looseObject({ id: z.string(), label_fr: z.string(), agent: z.string(), version: z.string(), summary_fr: z.string(), report: z.string().nullish(), report_expected: z.string().nullish(), protocol: z.looseObject({ file: z.string(), sha256: z.string().nullish() }).nullish(), files: z.array(z.looseObject({ file: z.string(), sha256: z.string() })).default([]) }),
  counts: Counts,
  rows: z.array(Row),
  holm_families: z.array(z.looseObject({ window: z.string().nullish(), size: Int.nullish(), rows: Int })),
  charts: z.array(Chart),
  calendar: z.array(CalendarItem),
});
export type TrackDetail = z.output<typeof TrackDetail>;

export const CellDetail = z.looseObject({ key: z.string(), cell: Cell, rows: z.array(Row) });
export type CellDetail = z.output<typeof CellDetail>;

const Upcoming = z.looseObject({ date: z.string(), family: z.string(), label_fr: z.string().nullish(), n_questions: Int, officials: z.array(z.string()).default([]), registry: z.string().nullish(), overdue: z.boolean().default(false) });
export type Upcoming = z.output<typeof Upcoming>;
export const LiveState = z.looseObject({
  computed_at: z.string(),
  totals: z.record(z.string(), Int),
  next_resolution: z.string().nullish(),
  upcoming: z.array(Upcoming).default([]),
  upcoming_total: Int.default(0),
  overdue_groups: Int.default(0),
  refreshing: z.boolean().default(false),
  partial: z.boolean().default(false),
  registries: z.array(z.looseObject({ registry: z.string(), batches: Int, questions: Int, forecasts: Int, status: z.record(z.string(), Int).nullish(), families: Int.nullish(), last_sealed_at: z.string().nullish() })).default([]),
  ensemble: z.looseObject({ available: z.boolean(), cells: Int.nullish(), verdicts: z.record(z.string(), Int).nullish(), n_ensemble_forecasts: Int.nullish(), n_ensemble_scored: Int.nullish(), claims_fr: z.array(z.string()).nullish(), reason: z.string().nullish() }).nullish(),
});
export type LiveState = z.output<typeof LiveState>;

export const DocText = z.looseObject({ path: z.string(), sha256: z.string().nullish(), text: z.string(), format: z.enum(['markdown', 'yaml']) });
export type DocText = z.output<typeof DocText>;

export const getScores = (signal?: AbortSignal) => v3get('/v1/v8/scores', envelopeOf(ScoresPage), undefined, signal);
export const getTrack = (id: string, signal?: AbortSignal) => v3get(`/v1/v8/scores/tracks/${encodeURIComponent(id)}`, envelopeOf(TrackDetail), undefined, signal);
export const getCell = (key: string, signal?: AbortSignal) => v3get(`/v1/v8/scores/cells/${encodeURIComponent(key)}`, envelopeOf(CellDetail), undefined, signal);
export const getLive = (signal?: AbortSignal) => v3get('/v1/v8/scores/live', envelopeOf(LiveState), undefined, signal);
export const getDoc = (path: string, signal?: AbortSignal) => v3get('/v1/v8/scores/doc', envelopeOf(DocText), { path }, signal);

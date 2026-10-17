/**
 * Data of the Accueil page, one tolerant client per source (each panel fails alone, never the page):
 *   changes   GET /v1/v8/changes                (V8API, docs/dev/v8_api_interface.md §2)
 *   calendar  GET /v1/v8/scores/live            (V8SCORES: next expected resolutions, registry totals)
 *             GET /v1/v8/scores/calendar?live=false (pre-registered tests and dated events)
 *             fallback: GET /v1/forecasts/questions?status=pending (first page, labelled as such)
 *   decisions GET /v1/v8/coverage/decisions     (V8COUVERTURE; raw body, not the envelope)
 *   coverage  GET /v1/v8/coverage/matrix?level=region (V8COUVERTURE; raw body)
 */
import { z } from 'zod';
import { request } from '../../../api/http';
import { envelopeOf, parseWith, v3get } from '../../../v3/api/common';
import { getForecastQuestions } from '../../../v3/api/forecasts';

const Int = z.number().int();

/**
 * One request shared by the panels that need the same read during one page view (the glance strip and the
 * panels): the promise is reused for `ttlMs`; a failure is not kept. Callers' abort signals do not cancel it.
 */
export function shared<T>(fn: () => Promise<T>, ttlMs = 15_000): (() => Promise<T>) & { reset: () => void } {
  let p: Promise<T> | null = null;
  let at = 0;
  const get = () => {
    if (!p || Date.now() - at > ttlMs) {
      at = Date.now();
      const mine = fn();
      p = mine;
      mine.catch(() => {
        if (p === mine) p = null;
      });
    }
    return p;
  };
  return Object.assign(get, { reset: () => void (p = null) });
}

export const ChangeItem = z.looseObject({
  id: z.string(),
  kind: z.string(),
  at: z.string(),
  title: z.string(),
  summary: z.string().nullish(),
  source: z.string().nullish(),
  severity: z.enum(['info', 'notable', 'action']).catch('info').default('info'),
  counts: z.record(z.string(), z.number()).nullish().transform((v) => v ?? {}),
  refs: z.array(z.looseObject({ type: z.string(), id: z.string() })).default([]),
  links: z.array(z.looseObject({ label: z.string(), href: z.string() })).default([]),
  origin: z.string().nullish(),
  detail: z.record(z.string(), z.unknown()).nullish().transform((v) => v ?? {}),
});
export type ChangeItem = z.output<typeof ChangeItem>;

export const ChangesData = z.looseObject({
  since: z.string().nullish(),
  until: z.string().nullish(),
  items: z.array(ChangeItem),
  counts: z.record(z.string(), Int).nullish().transform((v) => v ?? {}),
  kind_labels: z.record(z.string(), z.string()).nullish(),
  next_cursor: z.string().nullish(),
});
export type ChangesData = Omit<z.output<typeof ChangesData>, 'next_cursor'> & { next_cursor: string | null };

export async function getChanges(q: { since?: string; kinds?: string; limit?: number; cursor?: string }, signal?: AbortSignal): Promise<ChangesData> {
  const r = await v3get('/v1/v8/changes', envelopeOf(ChangesData), { ...q }, signal);
  return { ...r.data, next_cursor: r.data.next_cursor ?? r.next_cursor ?? null };
}

// ------------------------------------------------------------------ calendar

export const Upcoming = z.looseObject({
  date: z.string(),
  family: z.string(),
  label_fr: z.string().nullish(),
  n_questions: Int,
  officials: z.array(z.string()).default([]),
  registry: z.string().nullish(),
  overdue: z.boolean().default(false),
});
export type Upcoming = z.output<typeof Upcoming>;

export const LiveState = z.looseObject({
  computed_at: z.string().nullish(),
  totals: z.looseObject({
    sealed_batches: Int.nullish(),
    questions: Int,
    forecasts: Int.nullish(),
    pending: Int,
    resolved: Int,
    void: Int.nullish(),
  }),
  next_resolution: z.string().nullish(),
  upcoming: z.array(Upcoming).default([]),
  upcoming_total: Int.nullish(),
  overdue_groups: Int.nullish(),
  refreshing: z.boolean().nullish(),
});
export type LiveState = z.output<typeof LiveState>;

export const ScoreEvent = z.looseObject({
  id: z.string(),
  kind: z.string(),
  track: z.string().nullish(),
  date: z.string().nullish(),
  month: z.string().nullish(),
  sort: z.string().nullish(),
  note_fr: z.string().nullish(),
  forecasters: z.array(z.unknown()).default([]),
  n_rows: Int.nullish(),
});
export type ScoreEvent = z.output<typeof ScoreEvent>;

export interface CalendarView {
  source: 'scores' | 'forecasts';
  upcoming: Upcoming[];
  events: ScoreEvent[];
  totals: { questions: number; pending: number; resolved: number } | null;
  nextResolution: string | null;
  computedAt: string | null;
  /** Fallback only: the view covers the first `sample` pending questions out of `total`. */
  sample: { n: number; total: number } | null;
}

export async function fetchCalendar(signal?: AbortSignal): Promise<CalendarView> {
  try {
    const [live, cal] = await Promise.all([
      v3get('/v1/v8/scores/live', envelopeOf(LiveState), undefined, signal),
      v3get('/v1/v8/scores/calendar', envelopeOf(z.looseObject({ scoreboard: z.array(ScoreEvent).default([]) })), { live: false }, signal).catch(() => null),
    ]);
    const l = live.data;
    return {
      source: 'scores',
      upcoming: l.upcoming,
      events: cal?.data.scoreboard ?? [],
      totals: { questions: l.totals.questions, pending: l.totals.pending, resolved: l.totals.resolved },
      nextResolution: l.next_resolution ?? null,
      computedAt: l.computed_at ?? null,
      sample: null,
    };
  } catch (err) {
    if (signal?.aborted) throw err;
    // V8SCORES absent: group the first page of pending registry questions by expected resolution date
    const page = await getForecastQuestions({ status: 'pending', limit: 200 }, signal);
    const by = new Map<string, Upcoming>();
    for (const q of page.data.questions) {
      const k = `${q.expected_resolution}|${q.family}`;
      const u = by.get(k);
      if (u) u.n_questions += 1;
      else by.set(k, { date: q.expected_resolution, family: q.family, label_fr: q.label_fr, n_questions: 1, officials: [], registry: 'forecast', overdue: false });
    }
    const upcoming = [...by.values()].sort((a, b) => a.date.localeCompare(b.date));
    return {
      source: 'forecasts',
      upcoming,
      events: [],
      totals: null,
      nextResolution: upcoming[0]?.date ?? null,
      computedAt: null,
      sample: { n: page.data.questions.length, total: page.data.total },
    };
  }
}

// ------------------------------------------------------------------ owner decisions & coverage (raw bodies)

export const DecisionItem = z.looseObject({
  id: z.string(),
  kind: z.string(),
  kind_label: z.string().nullish(),
  title: z.string(),
  action: z.string().nullish(),
  status: z.string().default('open'),
  who: z.string().nullish(),
  unlocks: z.looseObject({ domains: z.array(z.string()).default([]), text: z.string().nullish() }).nullish(),
  observed: z.looseObject({ state: z.string().nullish() }).nullish(),
});
export type DecisionItem = z.output<typeof DecisionItem>;

export const DecisionsView = z.looseObject({
  updated: z.string().nullish(),
  counts: z.looseObject({ items: Int, open: Int }).nullish(),
  groups: z.array(z.looseObject({ kind: z.string(), label: z.string(), items: z.array(DecisionItem) })),
});
export type DecisionsView = z.output<typeof DecisionsView>;

async function raw<S extends z.ZodType>(path: string, schema: S, query?: Record<string, string>, signal?: AbortSignal): Promise<z.output<S>> {
  const res = await request<unknown>('GET', path, { query, signal });
  // tolerate both a raw body (current V8COUVERTURE routes) and the usual envelope
  const direct = schema.safeParse(res.body);
  if (direct.success) return direct.data;
  const body = res.body as { data?: unknown } | null;
  return parseWith(schema, body && typeof body === 'object' && 'data' in body ? body.data : body, path);
}

export const fetchDecisions = (signal?: AbortSignal) => raw('/v1/v8/coverage/decisions', DecisionsView, undefined, signal);

export const CoverageMeta = z.looseObject({
  computed_at: z.string(),
  atlas_tables: Int,
  atlas_rows: Int,
  kg_entities: Int,
  kg_statements: Int,
  kg_computed: z.boolean().default(true),
  places: Int,
  stale: z.record(z.string(), z.unknown()).nullish(),
});

export const CoverageRow = z.looseObject({
  id: z.string(),
  label: z.string(),
  summary: z.looseObject({
    tables: Int,
    rows: Int,
    countries: Int,
    latest_year: Int.nullish(),
    last_loaded_at: z.string().nullish(),
    pending_loads: Int.default(0),
    pending_decisions: Int.default(0),
  }),
});
export type CoverageRow = z.output<typeof CoverageRow>;

export const CoverageGlance = z.looseObject({ meta: CoverageMeta, rows: z.array(CoverageRow) });
export type CoverageGlance = z.output<typeof CoverageGlance>;

export const fetchCoverage = (signal?: AbortSignal) => raw('/v1/v8/coverage/matrix', CoverageGlance, { level: 'region' }, signal);

export const getCalendar = shared(() => fetchCalendar());
export const getDecisions = shared(() => fetchDecisions());
export const getCoverage = shared(() => fetchCoverage());

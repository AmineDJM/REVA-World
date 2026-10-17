/**
 * Client of the V8 board service (agent V8API, docs/dev/v8_api_interface.md §3, src/reva/api/routes/v8_boards.py):
 *   GET/POST /v1/v8/boards, GET/POST/DELETE /v1/v8/boards/{id}, POST/DELETE …/rules, GET …/alerts.
 * Board documents are validated with the same zod schema as local boards (model.ts) plus the server `version`.
 */
import { z } from 'zod';
import { request } from '../../../api/http';
import { ApiError } from '../../../api/problem';
import { envelopeOf, parseWith, v3get } from '../../../v3/api/common';
import { Board, BoardItem, WatchRule, type RuleKind, type ThresholdOp } from './model';

export const ServerBoard = Board.extend({ version: z.number().int().nullish().transform((v) => v ?? null) });
export type ServerBoard = z.output<typeof ServerBoard>;

export const BoardSummary = z.looseObject({
  id: z.string(),
  title: z.string(),
  n_items: z.number().int(),
  n_rules: z.number().int().default(0),
  updated_at: z.string(),
  version: z.number().int().nullish(),
});
export type BoardSummary = z.output<typeof BoardSummary>;

/** A change-feed item as embedded in an alert (tolerant: only what the alert list renders is required). */
export const AlertChange = z.looseObject({
  id: z.string(),
  kind: z.string(),
  at: z.string(),
  title: z.string(),
  summary: z.string().nullish(),
  source: z.string().nullish(),
  links: z.array(z.looseObject({ label: z.string(), href: z.string() })).default([]),
});

export const Alert = z.looseObject({
  rule_id: z.string(),
  item_uid: z.string(),
  kind: z.string(),
  at: z.string().nullish(),
  message: z.string(),
  change: AlertChange.nullish(),
  observed: z
    .looseObject({ value: z.string().nullish(), period: z.string().nullish(), source: z.string().nullish(), unit: z.string().nullish() })
    .nullish(),
  status: z.enum(['fired', 'not_evaluable']).default('fired'),
  reason: z.string().nullish(),
});
export type Alert = z.output<typeof Alert>;

export const AlertsData = z.looseObject({ since: z.string().nullish(), alerts: z.array(Alert), evaluated_at: z.string().nullish() });
export type AlertsData = z.output<typeof AlertsData>;

const B = '/v1/v8/boards';
const enc = encodeURIComponent;

export async function listServerBoards(signal?: AbortSignal): Promise<BoardSummary[]> {
  const r = await v3get(B, envelopeOf(z.looseObject({ boards: z.array(BoardSummary) })), undefined, signal);
  return r.data.boards;
}

export async function getServerBoard(id: string, signal?: AbortSignal): Promise<ServerBoard> {
  return (await v3get(`${B}/${enc(id)}`, envelopeOf(ServerBoard), undefined, signal)).data;
}

async function post<S extends z.ZodType>(path: string, body: unknown, schema: S): Promise<z.output<S>> {
  const res = await request<unknown>('POST', path, { body });
  return parseWith(schema, res.body, path);
}

export async function createServerBoard(b: { id?: string; title: string; items?: BoardItem[]; rules?: WatchRule[] }): Promise<ServerBoard> {
  return (await post(B, b, envelopeOf(ServerBoard))).data;
}

/** Full update with optimistic concurrency; a stale version is a 409 `version_conflict` (see `conflictBoard`). */
export async function saveServerBoard(b: { id: string; title: string; items: BoardItem[]; rules: WatchRule[]; version: number | null }): Promise<ServerBoard> {
  return (await post(`${B}/${enc(b.id)}`, { title: b.title, items: b.items, rules: b.rules, version: b.version }, envelopeOf(ServerBoard))).data;
}

export async function deleteServerBoard(id: string): Promise<void> {
  await request<unknown>('DELETE', `${B}/${enc(id)}`);
}

export async function addServerRule(id: string, rule: { kind: RuleKind; item_uid: string; op?: ThresholdOp; value?: string }): Promise<ServerBoard> {
  return (await post(`${B}/${enc(id)}/rules`, rule, envelopeOf(ServerBoard))).data;
}

export async function deleteServerRule(id: string, ruleId: string): Promise<ServerBoard> {
  const path = `${B}/${enc(id)}/rules/${enc(ruleId)}`;
  const res = await request<unknown>('DELETE', path);
  return parseWith(envelopeOf(ServerBoard), res.body, path).data;
}

export async function getBoardAlerts(id: string, since?: string, signal?: AbortSignal): Promise<AlertsData> {
  return (await v3get(`${B}/${enc(id)}/alerts`, envelopeOf(AlertsData), since ? { since } : undefined, signal)).data;
}

/** The server's current board carried by a 409 version_conflict, when it parses. */
export function conflictBoard(err: unknown): ServerBoard | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const p = err.problem as Record<string, unknown>;
  for (const cand of [p.detail, p.current, (p.details as Record<string, unknown> | undefined)?.current, p.board]) {
    const r = ServerBoard.safeParse(cand);
    if (r.success) return r.data;
  }
  return null;
}

/**
 * The route is not deployed on this server (unknown route 404 / 405 / 501 / non-JSON): boards fall back to the
 * browser. A 404 on a *board* (absent or not yours) is a different, real answer and is not "absent".
 */
export function serviceAbsent(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  if (err.kind === 'invalid_response' || err.status === 501 || err.status === 405) return true;
  if (err.status !== 404) return false;
  const d = typeof err.problem.detail === 'string' ? err.problem.detail : '';
  return d === '' || d === 'Not Found' || d === 'no such endpoint';
}

/**
 * Tableaux (boards) — pure model: pinned items, boards, watch rules, reorder, share-URL codec.
 *
 * A board is a list of pinned items (entity, series, forecast question, decision scenario, Markdown note)
 * plus watch rules. Boards are persisted by the server (/v1/v8/boards, V8API) when it is available and
 * otherwise kept in this browser (labelled "local"). A board can always be shared as a URL that carries
 * its JSON (read-only render, nothing is fetched to rebuild it except the live values of its items).
 *
 * Nothing here holds a value: items are *references* (ids, concept + country, question id); the values
 * shown on a board are always read live from the API with their source, never stored in the board.
 */
import { z } from 'zod';

// ------------------------------------------------------------------ pinned items

const Str = z.string().min(1).max(400);
const Label = z.string().max(400);

export const EntityPin = z.object({
  kind: z.literal('entity'),
  entityKind: Str, // KG kind or "country", "event", "causal_claim", …
  id: Str,
  label: Label,
  sublabel: Label.optional(),
});
export const SeriesPin = z.object({
  kind: z.literal('series'),
  concept: Str, // atlas concept (or headline concept) id
  geo: Str, // ISO3 (or ADM code)
  label: Label,
  table: Str.optional(), // atlas table when the pin comes from a table view
  unit: Label.optional(),
  headline: z.boolean().optional(), // headline (best-source) concept
});
export const ForecastPin = z.object({
  kind: z.literal('forecast'),
  questionId: Str,
  label: Label,
});
export const DecisionPin = z.object({
  kind: z.literal('decision'),
  ref: Str, // reference decision id or a /v8/decision state ref
  label: Label,
  summary: z.string().max(2000).optional(),
});
export const NotePin = z.object({
  kind: z.literal('note'),
  markdown: z.string().max(20000),
  label: Label.optional(),
});

/** What another feature passes to `pinItem` (see pin.ts). */
export const PinItem = z.discriminatedUnion('kind', [EntityPin, SeriesPin, ForecastPin, DecisionPin, NotePin]);
export type PinItem = z.output<typeof PinItem>;
export type PinKind = PinItem['kind'];

/** An item once on a board: the pin plus a stable uid and the time it was added. */
export const BoardItem = z.intersection(PinItem, z.object({ uid: Str, added_at: z.string() }));
export type BoardItem = z.output<typeof BoardItem>;

// ------------------------------------------------------------------ watch rules

export const RULE_KINDS = ['new_data', 'revision', 'resolution', 'threshold'] as const;
export type RuleKind = (typeof RULE_KINDS)[number];

export const RULE_LABEL_FR: Record<RuleKind, string> = {
  new_data: 'Nouvelle donnée',
  revision: 'Révision',
  resolution: 'Résolution',
  threshold: 'Seuil (valeur observée)',
};

export const THRESHOLD_OPS = ['>', '>=', '<', '<='] as const;
export type ThresholdOp = (typeof THRESHOLD_OPS)[number];

/** Decimal string, never a JSON number (same rule as atlas values). */
export const DecimalStr = z.string().regex(/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/);

export const WatchRule = z.object({
  id: Str,
  kind: z.enum(RULE_KINDS),
  item_uid: Str, // the board item watched
  op: z.enum(THRESHOLD_OPS).optional(),
  value: DecimalStr.optional(),
  created_at: z.string(),
});
export type WatchRule = z.output<typeof WatchRule>;

/** Which rules make sense for which item: thresholds only on observed series values, resolution only on forecasts. */
export function rulesFor(kind: PinKind): RuleKind[] {
  switch (kind) {
    case 'series':
      return ['new_data', 'revision', 'threshold'];
    case 'forecast':
      return ['resolution'];
    case 'entity':
      return ['new_data'];
    default:
      return [];
  }
}

export function ruleProblem(rule: Pick<WatchRule, 'kind' | 'op' | 'value'>, item: BoardItem | undefined): string | null {
  if (!item) return 'élément introuvable sur ce tableau';
  if (!rulesFor(item.kind).includes(rule.kind)) return `règle « ${RULE_LABEL_FR[rule.kind]} » sans objet pour ce type d’élément`;
  if (rule.kind === 'threshold') {
    if (!rule.op) return 'opérateur manquant';
    if (!rule.value || !DecimalStr.safeParse(rule.value.trim()).success) return 'seuil : nombre décimal attendu (ex. 2,5)';
  }
  return null;
}

/** "2,5" (French input) → "2.5"; anything else unchanged. */
export function normaliseDecimalInput(s: string): string {
  return s.trim().replace(/\s/g, '').replace(',', '.');
}

export function describeRule(rule: WatchRule, item: BoardItem | undefined): string {
  const what = item ? itemTitle(item) : 'élément retiré';
  if (rule.kind === 'threshold') return `${what} : valeur observée ${rule.op ?? '?'} ${(rule.value ?? '?').replace('.', ',')}`;
  return `${what} : ${RULE_LABEL_FR[rule.kind].toLowerCase()}`;
}

// ------------------------------------------------------------------ boards

export const Board = z.object({
  id: Str,
  title: z.string().min(1).max(200),
  items: z.array(BoardItem).max(200),
  rules: z.array(WatchRule).max(200).default([]),
  created_at: z.string(),
  updated_at: z.string(),
});
export type Board = z.output<typeof Board>;

/** Where a board lives: on the server, only in this browser, or only in a shared URL (read-only). */
export type BoardOrigin = 'server' | 'local' | 'shared';

export const KIND_LABEL_FR: Record<PinKind, string> = {
  entity: 'Entité',
  series: 'Série',
  forecast: 'Prévision',
  decision: 'Décision',
  note: 'Note',
};

export function itemTitle(it: PinItem): string {
  if (it.kind === 'note') return it.label?.trim() || firstLine(it.markdown) || 'Note';
  return it.label || ('id' in it ? it.id : it.kind === 'series' ? `${it.concept} · ${it.geo}` : it.kind === 'forecast' ? it.questionId : it.ref);
}

function firstLine(md: string): string {
  const l = md.split('\n').find((x) => x.trim());
  return (l ?? '').replace(/^#+\s*/, '').slice(0, 80);
}

/** Identity of a pin (two pins of the same thing are the same item: pinning twice is a no-op). */
export function pinKey(it: PinItem): string {
  switch (it.kind) {
    case 'entity':
      return `entity:${it.entityKind}:${it.id}`;
    case 'series':
      return `series:${it.concept}:${it.geo}:${it.table ?? ''}`;
    case 'forecast':
      return `forecast:${it.questionId}`;
    case 'decision':
      return `decision:${it.ref}`;
    case 'note':
      return `note:${it.markdown}`;
  }
}

export function newId(prefix: string): string {
  const r = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  return `${prefix}_${r.replace(/-/g, '').slice(0, 16)}`;
}

export function emptyBoard(title: string, now = new Date().toISOString()): Board {
  return { id: newId('b'), title: title.trim() || 'Tableau', items: [], rules: [], created_at: now, updated_at: now };
}

/** Add a pin (no duplicate: an existing identical pin is returned unchanged). */
export function addItem(board: Board, pin: PinItem, now = new Date().toISOString()): { board: Board; item: BoardItem; added: boolean } {
  const k = pinKey(pin);
  const existing = board.items.find((i) => pinKey(i) === k);
  if (existing) return { board, item: existing, added: false };
  const item = { ...pin, uid: newId('i'), added_at: now } as BoardItem;
  return { board: { ...board, items: [...board.items, item], updated_at: now }, item, added: true };
}

export function removeItem(board: Board, uid: string, now = new Date().toISOString()): Board {
  return { ...board, items: board.items.filter((i) => i.uid !== uid), rules: board.rules.filter((r) => r.item_uid !== uid), updated_at: now };
}

export function updateItem(board: Board, uid: string, patch: Partial<PinItem>, now = new Date().toISOString()): Board {
  return { ...board, items: board.items.map((i) => (i.uid === uid ? ({ ...i, ...patch, kind: i.kind } as BoardItem) : i)), updated_at: now };
}

/** Move the item at `from` to index `to` (clamped). Pure; used by drag-and-drop and by the keyboard. */
export function moveItem<T>(items: readonly T[], from: number, to: number): T[] {
  const out = items.slice();
  if (from < 0 || from >= out.length) return out;
  const t = Math.max(0, Math.min(out.length - 1, to));
  if (t === from) return out;
  const [x] = out.splice(from, 1);
  out.splice(t, 0, x as T);
  return out;
}

export function reorder(board: Board, from: number, to: number, now = new Date().toISOString()): Board {
  return { ...board, items: moveItem(board.items, from, to), updated_at: now };
}

export function addRule(board: Board, rule: Omit<WatchRule, 'id' | 'created_at'>, now = new Date().toISOString()): Board {
  const r: WatchRule = { ...rule, id: newId('r'), created_at: now };
  return { ...board, rules: [...board.rules, r], updated_at: now };
}

export function removeRule(board: Board, id: string, now = new Date().toISOString()): Board {
  return { ...board, rules: board.rules.filter((r) => r.id !== id), updated_at: now };
}

// ------------------------------------------------------------------ share URL codec

/** Shared boards are read-only renders of the board JSON carried in the URL fragment (never sent to a server). */
export const SHARE_ID = 'partage';
const MAX_SHARE_CHARS = 60000;

function b64urlEncode(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlDecode(s: string): string {
  const b64 = s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (s.length % 4)) % 4);
  const bin = atob(b64);
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
}

/** Compact JSON: rules and timestamps kept, so the shared render is exactly the board. */
export function encodeShare(board: Board): string {
  return b64urlEncode(JSON.stringify({ v: 1, board }));
}

export type ShareDecode = { ok: true; board: Board } | { ok: false; reason: string };

/** Decode an untrusted share fragment; any deviation from the board schema is refused (nothing partial is shown). */
export function decodeShare(fragment: string): ShareDecode {
  const raw = fragment.replace(/^#/, '').replace(/^b=/, '');
  if (!raw) return { ok: false, reason: 'lien de partage vide' };
  if (raw.length > MAX_SHARE_CHARS) return { ok: false, reason: 'lien de partage trop long' };
  let json: unknown;
  try {
    json = JSON.parse(b64urlDecode(raw));
  } catch {
    return { ok: false, reason: 'lien de partage illisible (encodage)' };
  }
  const env = z.object({ v: z.literal(1), board: Board }).safeParse(json);
  if (!env.success) return { ok: false, reason: 'lien de partage non conforme au format des tableaux' };
  return { ok: true, board: env.data.board };
}

export function shareHref(board: Board): string {
  return `/v8/tableaux/${SHARE_ID}#b=${encodeShare(board)}`;
}

/**
 * Pure helpers: search hits / V8 URLs → pins, and the browser-side evaluation of a LOCAL board's watch rules
 * against the change feed. The browser evaluates only what it can decide from the feed alone; everything else
 * is reported "not evaluable here" with the reason (never silently skipped, never guessed).
 */
import type { Alert, AlertChange, SearchHit } from './api';
import type { Board, BoardItem, PinItem, WatchRule } from './model';

/** A search hit as a pin; indicators need a country (`geo`), returned as null without one. */
export function hitToPin(hit: SearchHit, geo?: string): PinItem | null {
  const label = hit.label;
  const sublabel = hit.sublabel ?? undefined;
  switch (hit.kind) {
    case 'question':
      return { kind: 'forecast', questionId: hit.id, label };
    case 'decision':
      return { kind: 'decision', ref: hit.id, label, ...(sublabel ? { summary: sublabel } : {}) };
    case 'indicator': {
      const g = (geo ?? '').trim().toUpperCase();
      if (!/^[A-Z]{3}$/.test(g)) return null;
      const headline = hit.extra?.headline === true || hit.extra?.headline === '1' || /[?&]headline=1/.test(hit.href);
      const unit = typeof hit.extra?.unit === 'string' ? hit.extra.unit : undefined;
      return { kind: 'series', concept: hit.id, geo: g, label: `${label} · ${g}`, ...(headline ? { headline: true } : {}), ...(unit ? { unit } : {}) };
    }
    case 'country':
      return { kind: 'entity', entityKind: 'country', id: hit.id, label, ...(sublabel ? { sublabel } : {}) };
    default:
      return { kind: 'entity', entityKind: hit.entity_kind ?? hit.kind, id: hit.id, label, ...(sublabel ? { sublabel } : {}) };
  }
}

/** A pasted V8 link (fiche, decision, monde with concept+geo) or a bare forecast id as a pin. */
export function urlToPin(text: string): PinItem | null {
  const t = text.trim();
  if (/^fq_[0-9a-f]{24}$/.test(t)) return { kind: 'forecast', questionId: t, label: t };
  let u: URL;
  try {
    u = new URL(t, 'http://x.invalid');
  } catch {
    return null;
  }
  const m = /^\/v8\/fiche\/([^/]+)\/([^/]+)\/?$/.exec(u.pathname);
  if (m) {
    const kind = decodeURIComponent(m[1]!);
    const id = decodeURIComponent(m[2]!);
    if (kind === 'question' && /^fq_/.test(id)) return { kind: 'forecast', questionId: id, label: id };
    return { kind: 'entity', entityKind: kind, id, label: id };
  }
  if (u.pathname === '/v8/decision' && u.searchParams.get('ref')) {
    const ref = u.searchParams.get('ref')!;
    return { kind: 'decision', ref, label: ref };
  }
  if (u.pathname === '/v8/monde') {
    const concept = u.searchParams.get('concept');
    const geo = u.searchParams.get('geo') ?? u.searchParams.get('iso3');
    if (concept && geo && /^[A-Za-z]{3}$/.test(geo))
      return {
        kind: 'series',
        concept,
        geo: geo.toUpperCase(),
        label: `${concept} · ${geo.toUpperCase()}`,
        ...(u.searchParams.get('table') ? { table: u.searchParams.get('table')! } : {}),
        ...(u.searchParams.get('headline') === '1' ? { headline: true } : {}),
      };
  }
  return null;
}

export type LocalAlert = Alert & { local: true };

function touches(ch: AlertChange, item: BoardItem): boolean {
  if (item.kind === 'series') {
    return ch.refs.some((r) => (r.type === 'concept' && r.id === item.concept) || (item.table && r.type === 'table' && (r.id === item.table || r.id === `atlas.${item.table}`)));
  }
  if (item.kind === 'forecast') return ch.refs.some((r) => r.type === 'question' && r.id === item.questionId);
  if (item.kind === 'entity') return ch.refs.some((r) => r.type === 'entity' && r.id === item.id);
  return false;
}

/**
 * Evaluate a local board's rules on change-feed items. Decidable here: `resolution` (question ref), `new_data`
 * when the change names the item's table/concept/entity. Not decidable here (server needed): `revision`
 * (needs the table's version history), `threshold` (needs the latest observed value on the server side), and
 * `new_data` on a series pinned without a table.
 */
export function evaluateLocal(board: Board, changes: readonly AlertChange[]): LocalAlert[] {
  const out: LocalAlert[] = [];
  const byUid = new Map(board.items.map((i) => [i.uid, i] as const));
  const notEval = (r: WatchRule, reason: string): LocalAlert => ({
    rule_id: r.id,
    item_uid: r.item_uid,
    kind: r.kind,
    at: null,
    message: reason,
    change: null,
    observed: null,
    status: 'not_evaluable',
    reason,
    local: true,
  });
  for (const r of board.rules) {
    const it = byUid.get(r.item_uid);
    if (!it) continue;
    if (r.kind === 'revision') {
      out.push(notEval(r, 'Révision : évaluable seulement par le serveur (historique des versions de table).'));
      continue;
    }
    if (r.kind === 'threshold') {
      out.push(notEval(r, 'Seuil : évalué seulement par le serveur sur la dernière valeur observée.'));
      continue;
    }
    const wanted = r.kind === 'resolution' ? ['question_resolved'] : ['ingest', 'kg_batch'];
    const hits = changes.filter((c) => wanted.includes(c.kind) && touches(c, it));
    for (const c of hits)
      out.push({
        rule_id: r.id,
        item_uid: r.item_uid,
        kind: r.kind,
        at: c.at,
        message: c.title,
        change: c,
        observed: null,
        status: 'fired',
        reason: null,
        local: true,
      });
  }
  return out.sort((a, b) => (b.at ?? '').localeCompare(a.at ?? ''));
}

/** Deep links of an answer, built only with the contract's v8Links (plus the Comparer query of V8MONDE). */
import { v8Links } from '../../contract';
import type { Target } from './api';

export const TARGET_LABEL: Record<Target['kind'], string> = {
  fiche: 'Fiche',
  monde: 'Monde',
  comparer: 'Comparer',
  liens: 'Liens',
  decision: 'Décision',
  scores: 'Scores',
};

export function targetHref(t: Target): string | null {
  switch (t.kind) {
    case 'fiche':
      return t.id ? v8Links.entity(t.entity_kind || 'entity', t.id) : null;
    case 'monde': {
      if (!t.table || !t.indicator) return null;
      const p: Record<string, string> = { indicator: t.indicator };
      if (t.iso3) p.sel = t.iso3;
      if (t.period && /^\d{4}$/.test(t.period)) p.period = t.period;
      return v8Links.series(t.table, p);
    }
    case 'comparer':
      // V8MONDE's Comparer accepts ?s=<series key>&g=<ISO3> (no contract helper yet: requested in the report)
      return t.series && t.iso3 ? `/v8/comparer?${new URLSearchParams({ s: t.series, g: t.iso3 }).toString()}` : null;
    case 'liens':
      return t.from && t.to ? v8Links.path(t.from, t.to) : null;
    case 'decision':
      return v8Links.decision(t.ref ?? undefined);
    case 'scores':
      return v8Links.scoreboard(t.track ?? undefined);
    default:
      return null;
  }
}

export function brainstormHref(target: string): string {
  return `/v8/demander?${new URLSearchParams({ mode: 'brainstorm', t: target }).toString()}`;
}

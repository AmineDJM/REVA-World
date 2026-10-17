/**
 * Demander — questions in French or English answered only from REVA's loaded data, with citations (owner V8ASK).
 * Route /v8/demander?q=… (contract v8Links.ask), brainstorm mode /v8/demander?mode=brainstorm&t=….
 * Palette: anything typed after « ? » goes to Demander; « prévoir … » / « brainstorm … » opens the Brainstormer.
 */
import { lazy } from 'react';
import { v8Links, type PaletteHit, type V8Feature } from '../../contract';
import { brainstormHref } from './links';

const QUESTION_START = /^(?:qui|que|quoi|quel(?:le)?s?|combien|comment|pourquoi|o[uù]|quand|est-ce|y a-t-il|what|who|which|how|why|where|when|is|are|does|do|did)\b/i;

/** The question carried by a palette query: « ? … », or a query that reads as a question. */
export function questionOf(query: string): string | null {
  const q = query.trim();
  if (q.startsWith('?')) {
    const rest = q.replace(/^\?+\s*/, '').trim();
    return rest.length >= 2 ? rest : null;
  }
  if (q.length >= 8 && (q.endsWith('?') || QUESTION_START.test(q))) return q;
  return null;
}

/** The brainstorm target of a palette query: « prévoir … », « brainstorm(er) … », « forecast … ». */
export function brainstormOf(query: string): string | null {
  const m = /^\s*(?:brainstorm(?:er)?|pr[ée]voir|forecast|predict)\s*:?\s+(.{2,})$/i.exec(query);
  if (!m) return null;
  return /^(?:pr[ée]voir|forecast|predict)/i.test(query.trim()) ? query.trim() : (m[1] as string).trim();
}

export async function search(query: string): Promise<PaletteHit[]> {
  const out: PaletteHit[] = [];
  const q = questionOf(query);
  if (q) out.push({ id: `demander:q:${q}`, label: `Demander : « ${q} »`, sublabel: 'réponse sourcée, ou ce qui manque', href: v8Links.ask(q), group: 'Demander' });
  const b = brainstormOf(query.replace(/^\?+\s*/, ''));
  if (b) out.push({ id: `demander:b:${b}`, label: `Brainstormer : « ${b} »`, sublabel: 'pilotes, données, prévisions, prochaines étapes', href: brainstormHref(b), group: 'Demander' });
  return out;
}

export const feature: V8Feature = {
  id: 'demander',
  title: 'Demander',
  path: 'demander',
  order: 15,
  icon: '?',
  goKey: 'd',
  element: lazy(() => import('./DemanderPage')),
  commands: [
    {
      id: 'demander.ask',
      label: 'Demander à REVA…',
      keywords: ['?', 'demander', 'question', 'ask', 'pourquoi', 'combien'],
      run: (query) => {
        const q = questionOf(query) ?? query.replace(/^\s*(?:demander|ask|question)\s*:?\s*/i, '').trim();
        return q && !/^(?:demander|ask|question)$/i.test(query.trim()) ? v8Links.ask(q.replace(/^\?+\s*/, '')) : '/v8/demander';
      },
    },
    {
      id: 'demander.brainstorm',
      label: 'Brainstormer une prévision…',
      keywords: ['brainstorm', 'brainstormer', 'prévoir', 'prevoir', 'forecast'],
      run: (query) => {
        const b = brainstormOf(query);
        return b ? brainstormHref(b) : '/v8/demander?mode=brainstorm';
      },
    },
  ],
  search: async (query: string, signal: AbortSignal) => (signal.aborted ? [] : search(query)),
};

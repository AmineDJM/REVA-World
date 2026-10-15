/**
 * Deterministic search for the ⌘K command palette. No LLM, no network at query time, no
 * randomness: the same items and query always give the same ordered result.
 *
 * Score per item = best over its title and keywords:
 *   exact 100 > prefix 80 > word prefix 60 > substring 40 > in-order subsequence (title only) 10.
 * Ties break by kind order, then title, then id (byte order after case folding).
 */
export type PaletteKind = 'screen' | 'country' | 'concept' | 'run' | 'release';

export const KIND_ORDER: PaletteKind[] = ['screen', 'country', 'concept', 'run', 'release'];
export const KIND_LABEL: Record<PaletteKind, string> = {
  screen: 'Screen',
  country: 'Country',
  concept: 'Concept',
  run: 'Run',
  release: 'Release',
};

export interface PaletteItem {
  id: string;
  kind: PaletteKind;
  title: string;
  subtitle?: string;
  keywords?: string[];
  href: string;
}

/** Lower-case, strip diacritics and collapse whitespace (é -> e, "Côte d’Ivoire" -> "cote d'ivoire"). */
export function fold(s: string): string {
  return s
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[‘’]/g, "'")
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

function scoreText(text: string, q: string, allowSubsequence: boolean): number {
  if (!text) return 0;
  if (text === q) return 100;
  if (text.startsWith(q)) return 80;
  const words = text.split(/[\s._\-/:()@,]+/).filter(Boolean);
  if (words.some((w) => w.startsWith(q))) return 60;
  if (text.includes(q)) return 40;
  if (allowSubsequence && q.length >= 2) {
    let i = 0;
    for (const ch of text) if (ch === q[i]) i++;
    if (i === q.length) return 10;
  }
  return 0;
}

export function scoreItem(item: PaletteItem, query: string): number {
  const q = fold(query);
  if (!q) return 1;
  // Every query term must match somewhere; the item scores the mean of its terms' best scores.
  const terms = q.split(' ');
  const fields = [fold(item.title), ...(item.keywords ?? []).map(fold)];
  let total = 0;
  for (const t of terms) {
    let best = 0;
    fields.forEach((f, i) => {
      best = Math.max(best, scoreText(f, t, i === 0));
    });
    if (best === 0) return 0;
    total += best;
  }
  return total / terms.length;
}

function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

export function rankItems(items: PaletteItem[], query: string, limit = 40): PaletteItem[] {
  const scored: { item: PaletteItem; score: number }[] = [];
  for (const item of items) {
    const score = scoreItem(item, query);
    if (score > 0) scored.push({ item, score });
  }
  scored.sort(
    (a, b) =>
      b.score - a.score ||
      KIND_ORDER.indexOf(a.item.kind) - KIND_ORDER.indexOf(b.item.kind) ||
      cmp(fold(a.item.title), fold(b.item.title)) ||
      cmp(a.item.id, b.item.id),
  );
  return scored.slice(0, limit).map((s) => s.item);
}

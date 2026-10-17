/** Accent- and case-insensitive fuzzy matching for the palette (pétrole = petrole). */
export function normalize(s: string): string {
  return s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();
}

const SEP = /[\s\-_/·:.,()'’]/;

/**
 * Score of `query` against `text` (higher is better), or null when it does not match.
 * Substring > all words present > subsequence (with bonuses for word starts and runs).
 */
export function fuzzyScore(query: string, text: string): number | null {
  const q = normalize(query).trim();
  if (!q) return 0;
  const t = normalize(text);
  const idx = t.indexOf(q);
  if (idx >= 0) {
    const wordStart = idx === 0 || SEP.test(t[idx - 1]!);
    return 1000 + (wordStart ? 200 : 0) + (idx === 0 ? 100 : 0) - idx - Math.max(0, t.length - q.length) * 0.5;
  }
  const words = q.split(/\s+/).filter(Boolean);
  if (words.length > 1 && words.every((w) => t.includes(w))) return 700 - t.length * 0.5;
  let from = 0;
  let prev = -2;
  let score = 0;
  for (const ch of q.replace(/\s+/g, '')) {
    const j = t.indexOf(ch, from);
    if (j < 0) return null;
    score += j === prev + 1 ? 8 : 1;
    if (j === 0 || SEP.test(t[j - 1]!)) score += 6;
    prev = j;
    from = j + 1;
  }
  return Math.min(600, score * 3 - t.length * 0.3);
}

/** Best score of a query over several texts (label, keywords…). */
export function bestScore(query: string, texts: readonly (string | undefined)[]): number | null {
  let best: number | null = null;
  for (const t of texts) {
    if (!t) continue;
    const s = fuzzyScore(query, t);
    if (s !== null && (best === null || s > best)) best = s;
  }
  return best;
}

/** [before, match, after] for highlighting a contiguous match in a label (accent-insensitive). */
export function highlight(label: string, query: string): [string, string, string] | null {
  const q = normalize(query).trim();
  if (!q) return null;
  const idx = normalize(label).indexOf(q);
  // NFD can change lengths for some characters; only highlight when lengths line up.
  if (idx < 0 || normalize(label).length !== label.length) return null;
  return [label.slice(0, idx), label.slice(idx, idx + q.length), label.slice(idx + q.length)];
}

/**
 * "comparer FRA DEU inflation" → places [FRA, DEU] + indicator text "inflation".
 * Places: ISO3 codes (any case when the code exists and is not a common word) or country names (French or
 * English, accents and case ignored, up to three words: "royaume-uni", "États-Unis", "costa rica").
 */
export interface PlaceLike {
  iso3: string;
  label: string;
  label_fr?: string | null;
  frName?: string;
}

const KEYWORDS = new Set(['comparer', 'compare', 'comparaison', 'cmp']);
const FILLERS = new Set(['vs', 'versus', 'et', 'and', 'avec', 'contre', 'entre', '&', ',']);
/** Lower-case words that are also ISO3 codes: only taken as places when typed in capitals. */
const WORDS = new Set(['and', 'can', 'per', 'arm', 'bar', 'col', 'dom', 'gab', 'mac', 'mar', 'pan', 'nor', 'sur', 'ton', 'lie', 'pol', 'ben', 'chi']);

export function fold(s: string): string {
  return s
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[’'`]/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

export function parseCompareQuery(text: string, places: PlaceLike[]): { geos: string[]; rest: string } {
  const isos = new Set(places.map((p) => p.iso3));
  const names = new Map<string, string>();
  for (const p of places) for (const n of [p.label, p.label_fr, p.frName]) if (n) names.set(fold(n), p.iso3);
  const raw = text.split(/[\s,;]+/).filter(Boolean);
  const geos: string[] = [];
  const rest: string[] = [];
  let i = 0;
  if (raw[0] && KEYWORDS.has(raw[0].toLowerCase())) i = 1;
  while (i < raw.length) {
    const tok = raw[i]!;
    if (FILLERS.has(tok)) {
      i++;
      continue;
    }
    if (/^[A-Za-z]{3}$/.test(tok) && isos.has(tok.toUpperCase()) && (tok === tok.toUpperCase() || !WORDS.has(tok.toLowerCase()))) {
      if (!geos.includes(tok.toUpperCase())) geos.push(tok.toUpperCase());
      i++;
      continue;
    }
    let matched = 0;
    for (const n of [3, 2, 1]) {
      if (i + n > raw.length) continue;
      const iso = names.get(fold(raw.slice(i, i + n).join(' ')));
      if (iso) {
        if (!geos.includes(iso)) geos.push(iso);
        matched = n;
        break;
      }
    }
    if (matched) {
      i += matched;
      continue;
    }
    rest.push(tok);
    i++;
  }
  return { geos, rest: rest.join(' ') };
}

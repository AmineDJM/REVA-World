import type { PaletteHit } from '../../contract';

export const RECENT_KEY = 'v8.palette.recent';
export const RECENT_MAX = 8;

export function readRecents(): PaletteHit[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    const arr: unknown = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(arr)) return [];
    return arr
      .filter((h): h is PaletteHit => !!h && typeof h === 'object' && typeof (h as PaletteHit).href === 'string' && typeof (h as PaletteHit).label === 'string' && (h as PaletteHit).href.startsWith('/'))
      .slice(0, RECENT_MAX);
  } catch {
    return [];
  }
}

export function pushRecent(hit: PaletteHit): PaletteHit[] {
  const item: PaletteHit = { id: hit.id, label: hit.label, sublabel: hit.sublabel, href: hit.href, group: hit.group };
  const next = [item, ...readRecents().filter((h) => h.href !== hit.href)].slice(0, RECENT_MAX);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* storage unavailable: recents are not kept */
  }
  return next;
}

export function clearRecents(): void {
  try {
    localStorage.removeItem(RECENT_KEY);
  } catch {
    /* ignore */
  }
}

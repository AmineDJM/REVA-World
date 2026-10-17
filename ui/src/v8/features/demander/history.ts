/**
 * Conversation history of « Demander » — a per-viewer convenience only (this browser): the current question is in
 * the URL (?q=…), the list of earlier questions is kept in localStorage. Every access is wrapped: in a private
 * window or with storage blocked the page works without history.
 */
export interface HistoryEntry {
  q: string;
  mode: 'question' | 'brainstorm';
  status?: 'answered' | 'partial' | 'unknown' | null;
  /** One-line summary of the answer at the time it was asked (re-asking re-reads the data). */
  summary?: string | null;
  at: string;
}

const KEY = 'v8.demander.history.v1';
const MAX = 40;

function storage(): Storage | null {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function loadHistory(): HistoryEntry[] {
  const s = storage();
  if (!s) return [];
  try {
    const raw = s.getItem(KEY);
    if (!raw) return [];
    const v = JSON.parse(raw) as unknown;
    if (!Array.isArray(v)) return [];
    return v.filter((e): e is HistoryEntry => !!e && typeof e === 'object' && typeof (e as HistoryEntry).q === 'string').slice(0, MAX);
  } catch {
    return [];
  }
}

export function saveHistory(list: HistoryEntry[]): void {
  const s = storage();
  if (!s) return;
  try {
    s.setItem(KEY, JSON.stringify(list.slice(0, MAX)));
  } catch {
    /* quota or blocked storage: history is a convenience */
  }
}

/** Put an entry first (same question + mode replaces the earlier one). */
export function remember(entry: Omit<HistoryEntry, 'at'>, now = new Date()): HistoryEntry[] {
  const list = loadHistory().filter((e) => !(e.q === entry.q && e.mode === entry.mode));
  const next = [{ ...entry, at: now.toISOString() }, ...list].slice(0, MAX);
  saveHistory(next);
  return next;
}

export function clearHistory(): void {
  const s = storage();
  if (!s) return;
  try {
    s.removeItem(KEY);
  } catch {
    /* ignore */
  }
}

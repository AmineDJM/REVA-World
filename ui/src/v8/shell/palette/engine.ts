/**
 * Palette engine (pure): builds command/navigation items from discovered features + built-in actions,
 * ranks them against the query, merges async provider hits, groups everything for display.
 */
import type { PaletteHit } from '../../contract';
import type { DiscoveredFeature } from '../discovery';
import { bestScore } from './fuzzy';

export interface PaletteItem {
  key: string;
  label: string;
  sublabel?: string;
  /** Target URL; null for an action (theme, rail…). */
  href: string | null;
  group: string;
  action?: () => void;
  /** Keyboard hint shown on the right ("g m"). */
  hint?: string;
  score?: number;
}

export interface PaletteGroup {
  group: string;
  items: PaletteItem[];
}

export interface BuiltinAction {
  id: string;
  label: string;
  keywords?: string[];
  run: () => void;
  hint?: string;
}

export const GROUP_NAV = 'Aller à';
export const GROUP_COMMANDS = 'Commandes';
export const GROUP_RECENT = 'Récents';
export const HITS_PER_GROUP = 8;

/** Navigation items ("Monde", g m) for every linkable feature. */
export function navItems(features: readonly DiscoveredFeature[], query: string): PaletteItem[] {
  const out: PaletteItem[] = [];
  for (const f of features) {
    if (f.navHidden || !f.navTo) continue;
    const s = bestScore(query, [f.title, f.id, f.route]);
    if (s === null || (query.trim() && s < 300)) continue;
    out.push({ key: `nav:${f.id}`, label: f.title, sublabel: f.navTo, href: f.navTo, group: GROUP_NAV, hint: f.goKey ? `g ${f.goKey}` : undefined, score: s });
  }
  return query.trim() ? out.sort((a, b) => (b.score ?? 0) - (a.score ?? 0)) : out;
}

/**
 * Feature commands + built-ins. A command matches by label/keywords, or when the query starts with one of
 * its keywords ("comparer FRA DEU inflation"); its `run(query)` gives the URL — null means "not applicable".
 */
export function commandItems(features: readonly DiscoveredFeature[], builtins: readonly BuiltinAction[], query: string): PaletteItem[] {
  const q = query.trim();
  const first = q.split(/\s+/)[0]?.toLowerCase() ?? '';
  const out: PaletteItem[] = [];
  for (const f of features) {
    for (const c of f.commands ?? []) {
      let s = bestScore(q, [c.label, ...(c.keywords ?? [])]);
      if (q && c.keywords?.some((k) => k.toLowerCase() === first || (first.length >= 3 && k.toLowerCase().startsWith(first)))) s = Math.max(s ?? 0, 1500);
      if (s === null || (q && s < 300)) continue;
      let href: string | null = null;
      try {
        href = c.run(q);
      } catch {
        href = null;
      }
      if (!href) continue;
      out.push({ key: `cmd:${f.id}:${c.id}`, label: c.label, sublabel: f.title, href, group: GROUP_COMMANDS, score: s });
    }
  }
  for (const b of builtins) {
    const s = bestScore(q, [b.label, ...(b.keywords ?? [])]);
    if (s === null || (q && s < 300)) continue;
    out.push({ key: `act:${b.id}`, label: b.label, href: null, action: b.run, group: GROUP_COMMANDS, hint: b.hint, score: s });
  }
  return q ? out.sort((a, b) => (b.score ?? 0) - (a.score ?? 0)) : out;
}

export function hitItems(hits: readonly PaletteHit[]): PaletteItem[] {
  return hits.map((h) => ({ key: `hit:${h.group}:${h.id}:${h.href}`, label: h.label, sublabel: h.sublabel, href: h.href, group: h.group || 'Résultats' }));
}

/**
 * Display groups. Empty query: Récents, Aller à, Commandes. Typed query: best of navigation+commands first
 * (as one "Commandes" block when commands match a keyword), then provider groups in order of arrival,
 * each capped, deduplicated by URL.
 */
export function buildGroups(args: { query: string; recents: readonly PaletteHit[]; nav: PaletteItem[]; commands: PaletteItem[]; hits: readonly PaletteHit[] }): PaletteGroup[] {
  const { query, recents, nav, commands, hits } = args;
  const groups: PaletteGroup[] = [];
  const seen = new Set<string>();
  const add = (group: string, items: PaletteItem[], cap = Infinity) => {
    const kept: PaletteItem[] = [];
    for (const it of items) {
      const id = it.href ?? it.key;
      if (seen.has(id)) continue;
      seen.add(id);
      kept.push(it);
      if (kept.length >= cap) break;
    }
    if (kept.length) groups.push({ group, items: kept });
  };
  if (!query.trim()) {
    add(GROUP_RECENT, recents.map((r) => ({ key: `recent:${r.href}`, label: r.label, sublabel: r.sublabel ?? r.group, href: r.href, group: GROUP_RECENT })));
    add(GROUP_NAV, nav);
    add(GROUP_COMMANDS, commands);
    return groups;
  }
  const strongCommands = commands.filter((c) => (c.score ?? 0) >= 1500);
  if (strongCommands.length) add(GROUP_COMMANDS, strongCommands, 4);
  add(GROUP_NAV, nav, 5);
  const order: string[] = [];
  const byGroup = new Map<string, PaletteItem[]>();
  for (const it of hitItems(hits)) {
    if (!byGroup.has(it.group)) {
      byGroup.set(it.group, []);
      order.push(it.group);
    }
    byGroup.get(it.group)!.push(it);
  }
  for (const g of order) add(g, byGroup.get(g)!, HITS_PER_GROUP);
  add(GROUP_COMMANDS, commands.filter((c) => (c.score ?? 0) < 1500), 5);
  return groups;
}

export function flatten(groups: readonly PaletteGroup[]): PaletteItem[] {
  return groups.flatMap((g) => g.items);
}

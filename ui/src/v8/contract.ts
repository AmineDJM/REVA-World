// REVA V8 console — feature contract (owner: lead; changes only through the lead).
//
// Every V8 feature lives in ui/src/v8/features/<id>/ and exports `feature` from index.ts(x).
// The shell (ui/src/v8/shell/, owner V8SHELL) discovers features with import.meta.glob, so no
// feature agent edits a shared file (App.tsx, router, nav, package.json belong to V8SHELL).
import type { ComponentType, LazyExoticComponent } from 'react';

/** A command shown in the Ctrl/Cmd-K palette. `run` returns the URL to navigate to (or null). */
export interface PaletteCommand {
  id: string;
  label: string; // French UI text
  keywords?: string[];
  run: (query: string) => string | null;
}

/** Async palette provider: given the typed query, return search hits (entities, series, forecasts, …). */
export interface PaletteHit {
  id: string;
  label: string;
  sublabel?: string; // kind, country, period, source
  href: string; // V8 URL
  group: string; // e.g. "Entités", "Séries", "Prévisions", "Décisions"
}
export type PaletteProvider = (query: string, signal: AbortSignal) => Promise<PaletteHit[]>;

export interface V8Feature {
  id: string; // = folder name
  title: string; // French nav label
  path: string; // route path under /v8, e.g. "monde" → /v8/monde; may contain params ("fiche/:kind/:id")
  navPath?: string; // link target in the nav when path has params
  order: number; // nav order (10, 20, …); features without nav set navHidden
  navHidden?: boolean;
  icon?: string; // single emoji or short glyph
  element: LazyExoticComponent<ComponentType> | ComponentType;
  commands?: PaletteCommand[];
  search?: PaletteProvider;
  /** Keyboard shortcut "g <key>" to jump to the feature. */
  goKey?: string;
}

/** Cross-feature deep links (stable URL scheme; every feature must accept these). */
export const v8Links = {
  entity: (kind: string, id: string) => `/v8/fiche/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`,
  series: (table: string, params: Record<string, string> = {}) =>
    `/v8/monde?${new URLSearchParams({ table, ...params }).toString()}`,
  ask: (q: string) => `/v8/demander?${new URLSearchParams({ q }).toString()}`,
  decision: (ref?: string) => (ref ? `/v8/decision?${new URLSearchParams({ ref }).toString()}` : '/v8/decision'),
  scoreboard: (track?: string) => (track ? `/v8/scores?track=${encodeURIComponent(track)}` : '/v8/scores'),
  path: (from: string, to: string) => `/v8/liens?${new URLSearchParams({ from, to }).toString()}`,
  board: (id: string) => `/v8/tableaux/${encodeURIComponent(id)}`,
};

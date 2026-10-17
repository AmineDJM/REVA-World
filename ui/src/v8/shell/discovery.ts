/**
 * Feature discovery (pure): turns the modules found by import.meta.glob('../features/*\/index.{ts,tsx}')
 * into a validated, ordered list. Nothing is hard-coded: the nav lists what exists.
 */
import type { V8Feature } from '../contract';

export interface DiscoveredFeature extends V8Feature {
  /** Folder name (ui/src/v8/features/<folder>/). */
  folder: string;
  /** Route path relative to /v8 ('' = the /v8 index). */
  route: string;
  /** Absolute nav target (/v8/…), or null when the route has params and no navPath. */
  navTo: string | null;
  /** First static path segment ('' for the index) — used for breadcrumbs, prefetch and palette mapping. */
  segment: string;
}

export interface Discovery {
  features: DiscoveredFeature[];
  /** Human-readable problems (invalid module, duplicate id/route/goKey) — shown in the shortcuts sheet. */
  problems: string[];
}

const clean = (p: string) => p.trim().replace(/^\/+/, '').replace(/^v8(\/|$)/, '').replace(/\/+$/, '');

function isFeature(x: unknown): x is V8Feature {
  if (!x || typeof x !== 'object') return false;
  const f = x as Partial<V8Feature>;
  return typeof f.id === 'string' && typeof f.title === 'string' && typeof f.path === 'string' && typeof f.order === 'number' && (typeof f.element === 'function' || (typeof f.element === 'object' && f.element !== null));
}

export function collectFeatures(modules: Record<string, unknown>): Discovery {
  const problems: string[] = [];
  const byFolder = new Map<string, { key: string; feature: V8Feature }>();
  for (const key of Object.keys(modules).sort()) {
    const folder = /features\/([^/]+)\/index\.tsx?$/.exec(key)?.[1] ?? key;
    const mod = modules[key] as { feature?: unknown } | undefined;
    const f = mod?.feature;
    if (!isFeature(f)) {
      problems.push(`${folder} : le module n’exporte pas de « feature » valide (id, title, path, order, element).`);
      continue;
    }
    if (byFolder.has(folder)) {
      problems.push(`${folder} : index.ts et index.tsx présents ; ${byFolder.get(folder)!.key} est utilisé.`);
      continue;
    }
    if (f.id !== folder) problems.push(`${folder} : id « ${f.id} » différent du dossier.`);
    byFolder.set(folder, { key, feature: f });
  }

  const out: DiscoveredFeature[] = [];
  const ids = new Set<string>();
  const routes = new Set<string>();
  const goKeys = new Map<string, string>();
  const sorted = [...byFolder.entries()].sort(([, a], [, b]) => a.feature.order - b.feature.order || a.feature.title.localeCompare(b.feature.title, 'fr'));
  for (const [folder, { feature }] of sorted) {
    if (ids.has(feature.id)) {
      problems.push(`${folder} : id « ${feature.id} » déjà utilisé ; ignoré.`);
      continue;
    }
    const route = clean(feature.path);
    if (routes.has(route)) {
      problems.push(`${folder} : route « /v8/${route} » déjà prise ; ignorée.`);
      continue;
    }
    ids.add(feature.id);
    routes.add(route);
    let goKey = feature.goKey?.trim().toLowerCase() || undefined;
    if (goKey && (goKey.length !== 1 || goKeys.has(goKey))) {
      problems.push(`${folder} : raccourci « g ${goKey} » ${goKey.length !== 1 ? 'invalide (une seule touche)' : `déjà pris par ${goKeys.get(goKey)}`} ; ignoré.`);
      goKey = undefined;
    }
    if (goKey) goKeys.set(goKey, feature.id);
    const target = feature.navPath !== undefined ? clean(feature.navPath) : route.includes(':') || route.includes('*') ? null : route;
    const navTo = target === null ? null : target ? `/v8/${target}` : '/v8';
    out.push({ ...feature, goKey, folder, route, navTo, segment: route.split('/')[0] ?? '' });
  }
  return { features: out, problems };
}

/** The feature whose route owns an absolute /v8 URL (by first segment), if any. */
export function featureForPath(features: readonly DiscoveredFeature[], pathname: string): DiscoveredFeature | null {
  const rest = pathname.replace(/^\/v8\/?/, '').split(/[?#]/)[0] ?? '';
  const seg = decodeURIComponent(rest.split('/')[0] ?? '');
  if (!pathname.startsWith('/v8')) return null;
  return features.find((f) => f.segment === seg && !f.segment.startsWith(':')) ?? null;
}

/** Nav entries: features with a link target, not hidden. */
export function navFeatures(features: readonly DiscoveredFeature[]): DiscoveredFeature[] {
  return features.filter((f) => !f.navHidden && f.navTo !== null);
}

/**
 * Licence display (review V3b LIC-1): every atlas cell carries `rights` (licence, NC/SA/ND, attribution) and every
 * answer lists `licences`; a restricted source is named next to the values it gives, never only in a footnote.
 */
export interface RightsLike {
  licence: string;
  commercial_use?: string;
  non_commercial?: boolean;
  share_alike?: boolean;
  no_derivatives?: boolean;
  open_licence?: boolean | null;
  restricted?: boolean;
  attribution?: string | null;
  licence_url?: string | null;
  required_notice?: string | null;
}

/** French badges of the restrictive terms of one licence (empty for an open, attribution-only licence). */
export function licenceTermsFr(r: RightsLike | null | undefined): string[] {
  if (!r) return [];
  const out: string[] = [];
  if (r.non_commercial || r.commercial_use === 'not_permitted') out.push('licence non commerciale');
  if (r.commercial_use === 'restricted') out.push('accès payant interdit');
  if (r.share_alike) out.push('partage dans les mêmes conditions');
  if (r.no_derivatives) out.push('pas de modification');
  if (r.open_licence === false && !out.length) out.push('licence non ouverte');
  return out;
}

/** Restricted licences first, one entry per (source, licence). */
export function restrictedLicences<T extends RightsLike & { source: string }>(ls: readonly T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const l of ls) {
    const k = `${l.source}|${l.licence}`;
    if (!l.restricted || seen.has(k)) continue;
    seen.add(k);
    out.push(l);
  }
  return out;
}

/** Notices the providers' terms require next to their values (one per source, verbatim; e.g. New York Fed). */
export function requiredNotices<T extends RightsLike & { source: string }>(ls: readonly T[]): { source: string; notice: string }[] {
  const seen = new Map<string, string>();
  for (const l of ls) if (l.required_notice && !seen.has(l.source)) seen.set(l.source, l.required_notice);
  return [...seen.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([source, notice]) => ({ source, notice }));
}

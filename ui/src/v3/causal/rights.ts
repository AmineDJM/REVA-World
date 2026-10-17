/**
 * Licence terms of the sources that assert causal claims (review V3b LIC-1, applied to the causality screen).
 *
 * The causal routes do not carry licences; the source registry does (`GET /v1/sources`, `rights` as recorded for
 * each source). Flags are derived from the recorded terms with the same rule as the atlas
 * (`reva.atlas.snapshot.normalise_rights`): explicit keys first, then the licence text (CC ids, "non-commercial",
 * "cannot charge", "terms of use"). A source whose terms cannot be read is shown as such, never as open.
 */
import { createContext, useEffect, useState } from 'react';
import { request } from '../../api/http';
import type { AtlasLicence } from '../api/atlas';
import { memo } from '../api/common';

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => !!v && typeof v === 'object' && !Array.isArray(v);

const NC_RE = /(\bby[- ]nc\b|non[- ]?commercial|commercial use (?:is )?(?:strictly )?prohibited|prohibits? commercial|no commercial|commercial use not permitted)/i;
const SA_RE = /(\bby(?:[- ]nc)?[- ]sa\b|share[- ]?alike)/i;
const ND_RE = /(\bby(?:[- ]nc)?[- ]nd\b|no[- ]?deriv)/i;
const NOT_OPEN_RE = /(not an open licen[cs]e|no explicit data licen[cs]e|no open data licen[cs]e|no (?:open )?licen[cs]e stated|terms of use|cannot charge|no charging)/i;
const OPEN_RE = /(\bcc[- ]?by\b|\bcc0\b|public domain|open government licen[cs]e|\bodbl\b|etalab)/i;
const CHARGE_RE = /cannot charge|no charging|charge for access|charging for access/i;

/** One source's recorded rights → the atlas `rights` shape (licence + flags + `restricted`). */
export function normaliseRights(source: string, raw: unknown): AtlasLicence | null {
  const r: Rec = typeof raw === 'string' ? { licence: raw } : isRec(raw) ? raw : {};
  if (typeof r.licence !== 'string' || !r.licence) return null;
  const licence = r.licence;
  const text = ['licence', 'restriction', 'commercial_use', 'terms', 'share_alike', 'redistribution'].map((k) => (typeof r[k] === 'string' ? (r[k] as string) : '')).join(' ');
  const cu = typeof r.commercial_use === 'string' ? r.commercial_use.toLowerCase() : '';
  const nc = r.non_commercial === true || cu.includes('prohibit') || cu.includes('not_permitted') || NC_RE.test(text);
  const charge = CHARGE_RE.test(text);
  const sa = r.share_alike === true || SA_RE.test(licence);
  const nd = r.no_derivatives === true || ND_RE.test(licence);
  let open: boolean | null;
  if (typeof r.open_licence === 'boolean') open = r.open_licence && !nc;
  else if (nc || NOT_OPEN_RE.test(text)) open = false;
  else if (OPEN_RE.test(licence)) open = true;
  else open = null;
  return {
    source,
    licence,
    licence_url: typeof r.licence_url === 'string' ? r.licence_url : null,
    attribution: typeof r.attribution === 'string' ? r.attribution : null,
    commercial_use: nc ? 'not_permitted' : charge ? 'restricted' : open ? 'permitted' : 'unknown',
    non_commercial: nc,
    share_alike: sa,
    no_derivatives: nd,
    open_licence: open,
    restricted: nc || sa || nd || charge || open === false,
    basis: 'source_registry',
    restriction: null,
  };
}

export type RightsMap = ReadonlyMap<string, AtlasLicence>;

/** Source key → rights, from the source registry; one request per page load (a failure is not cached). */
export const loadSourceRights = memo(async (): Promise<RightsMap> => {
  const res = await request<unknown>('GET', '/v1/sources');
  const body = res.body;
  const list = isRec(body) && Array.isArray(body.data) ? body.data : Array.isArray(body) ? body : [];
  const m = new Map<string, AtlasLicence>();
  for (const s of list.filter(isRec)) {
    const key = typeof s.source_key === 'string' ? s.source_key : null;
    const r = key ? normaliseRights(key, s.rights) : null;
    if (key && r) m.set(key, r);
  }
  return m;
});

export type RightsState = { status: 'loading' } | { status: 'ok'; map: RightsMap } | { status: 'unavailable' };

/** The registry's rights, or "unavailable" (then the screen says the licences could not be read). */
export function useSourceRights(enabled = true): RightsState {
  const [st, setSt] = useState<RightsState>({ status: 'loading' });
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    loadSourceRights()
      .then((map) => live && setSt({ status: 'ok', map }))
      .catch(() => live && setSt({ status: 'unavailable' }));
    return () => {
      live = false;
    };
  }, [enabled]);
  return st;
}

/** Rights of the sources on screen, provided by the causality screen to every claim line (absent elsewhere). */
export const RightsContext = createContext<RightsMap | null>(null);

/**
 * Citations: every statement, hop, cell and answer sentence names its source.
 *
 * Wikidata links are built only for Wikidata sources and Wikidata ids (Q…/P…); provider-scoped
 * keys (ucdp:conflict:333, fixture:…) never get a Wikidata URL. A revision id links to that exact
 * revision (permalink). Community-edited sources carry the "communautaire, non vérifié" badge.
 */
import type { KgProvenance, KgStatement } from '../api/kg';
import type { LinkEvidence } from '../api/links';
import type { AtlasCell } from '../api/atlas';

export const COMMUNITY_BADGE = 'communautaire, non vérifié';

export interface Citation {
  /** Compact text, e.g. "Wikidata Q155$5C1B… · rév. 2250000000". */
  label: string;
  source: string;
  /** Link to the statement (or item) on its source, when one can be built honestly. */
  href: string | null;
  revision: string | null;
  revisionHref: string | null;
  community: boolean;
  fixture: boolean;
  /** Extra provenance lines (raw sha256, selector, retrieved_at, rank, references). */
  details: string[];
}

export const isWikidataSource = (source: string | null | undefined) => !!source && /^wikidata/i.test(source);
export const isQid = (id: string | null | undefined): id is string => !!id && /^Q[1-9]\d*$/.test(id);
export const isPid = (id: string | null | undefined): id is string => !!id && /^P[1-9]\d*$/.test(id);

export function wikidataItemUrl(qid: string): string | null {
  return isQid(qid) ? `https://www.wikidata.org/wiki/${qid}` : null;
}

export function wikidataPropertyUrl(pid: string): string | null {
  return isPid(pid) ? `https://www.wikidata.org/wiki/Property:${pid}` : null;
}

/** Statement GUIDs look like "Q155$5C1B…" (or "q155-…" in RDF form). */
export function wikidataStatementUrl(statementId: string | null | undefined, subject?: string | null): string | null {
  if (!statementId) return null;
  const m = /^(Q[1-9]\d*)[$-]/i.exec(statementId);
  const item = m ? m[1]!.toUpperCase() : isQid(subject) ? subject : null;
  if (!item) return null;
  return `https://www.wikidata.org/wiki/${item}#${encodeURIComponent(statementId.replace(/^q/, 'Q')).replace(/%24/g, '$')}`;
}

export function wikidataRevisionUrl(rev: string | null | undefined): string | null {
  return rev && /^\d+$/.test(rev) ? `https://www.wikidata.org/w/index.php?oldid=${rev}` : null;
}

function shortSha(s: string | null | undefined) {
  return s ? `${s.slice(0, 12)}…` : '';
}

function sourceName(source: string): string {
  return isWikidataSource(source) ? 'Wikidata' : source;
}

/** Citation of a KG statement from its provenance. */
export function citeKgStatement(st: Pick<KgStatement, 'subject' | 'provenance'>): Citation {
  const p: KgProvenance = st.provenance;
  const wd = isWikidataSource(p.source_key);
  const sid = p.statement_id ?? p.statement_key;
  const details = [
    `rang : ${p.rank === 'preferred' ? 'préféré' : 'normal'}`,
    `récupéré le ${p.retrieved_at.slice(0, 10)}`,
    p.raw_sha256 ? `artefact brut sha256 ${shortSha(p.raw_sha256)}` : '',
    p.references.length ? `${p.references.length} référence(s) propre(s) à la déclaration` : 'aucune référence propre à la déclaration',
  ].filter(Boolean);
  return {
    label: `${sourceName(p.source_key)} ${sid}${p.source_revision ? ` · rév. ${p.source_revision}` : ''}`,
    source: p.source_key,
    href: wd ? wikidataStatementUrl(p.statement_id, st.subject.qid) : null,
    revision: p.source_revision,
    revisionHref: wd ? wikidataRevisionUrl(p.source_revision) : null,
    community: p.verification === 'community_edited_not_verified' || wd,
    fixture: p.origin === 'test_fixture',
    details,
  };
}

/** Citation of a links-layer evidence item (KG statement or atlas row). */
export function citeLinkEvidence(e: LinkEvidence): Citation {
  const wd = isWikidataSource(e.source);
  if (e.kind === 'atlas_row') {
    return {
      label: e.cite ?? `${e.source}:${shortSha(e.raw_sha256)}#${e.selector ?? ''}`,
      source: e.source,
      href: null,
      revision: null,
      revisionHref: null,
      community: false,
      fixture: e.origin === 'test_fixture',
      details: [e.raw_sha256 ? `artefact brut sha256 ${shortSha(e.raw_sha256)}` : '', e.selector ? `sélecteur ${e.selector}` : ''].filter(Boolean),
    };
  }
  return {
    label: `${sourceName(e.source)} ${e.statement_id ?? ''}${e.source_revision ? ` · rév. ${e.source_revision}` : ''}`.trim(),
    source: e.source,
    href: wd ? wikidataStatementUrl(e.statement_id, e.subject) : null,
    revision: e.source_revision ?? null,
    revisionHref: wd ? wikidataRevisionUrl(e.source_revision) : null,
    community: wd,
    fixture: e.origin === 'test_fixture',
    details: [e.retrieved_at ? `récupéré le ${e.retrieved_at.slice(0, 10)}` : '', e.pid ? `propriété ${e.pid}` : ''].filter(Boolean),
  };
}

/** Citation of an atlas cell: source/dataset/indicator, period, raw artifact and selector. */
export function citeAtlasCell(c: AtlasCell): Citation {
  const sel = Object.entries(c.provenance.selector)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(', ');
  return {
    label: `${c.source}/${c.dataset} · ${c.indicator}`,
    source: c.source,
    href: null,
    revision: null,
    revisionHref: null,
    community: false,
    fixture: false,
    details: [
      `période ${c.period_start} → ${c.period_end} (fin exclue)`,
      `artefact brut sha256 ${shortSha(c.provenance.raw_sha256)}`,
      sel ? `sélecteur ${sel}` : '',
      c.provenance.dataset_key ? `table ${c.provenance.dataset_key}` : '',
    ].filter(Boolean),
  };
}

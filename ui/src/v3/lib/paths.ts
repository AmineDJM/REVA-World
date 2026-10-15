/**
 * One view model for explained paths from either engine:
 *   - LINKS  (/v1/links/path): typed hops, k shortest weighted paths, fr/en sentences;
 *   - KGCORE (/v1/kg/path):    raw shortest-statement BFS.
 *
 * Honesty rule (V3 "a path is not a cause"): a hop is worded "relié via « propriété »". Causal
 * wording is produced ONLY when the API marks the hop as a source-asserted cause (LINKS:
 * `causal: true` on a statement hop that has evidence; KG: a non-null `causal_assertion` on a
 * has-cause/has-effect property), and it is always attributed to the asserting source.
 */
import type { KgPathResult } from '../api/kg';
import { linkNodeLabel, LINK_TYPE_LABEL, type LinksPathData, type LinkTemporal } from '../api/links';
import { citeKgStatement, citeLinkEvidence, isWikidataSource, type Citation } from './citations';
import { formatYear, temporalLabel, temporalOf, yearOfPartial } from './time';

export interface HopView {
  from: { id: string; label: string };
  to: { id: string; label: string };
  /** Relation label as served ("langue officielle", "signataire"...). */
  relation: string;
  /** The sentence shown for the hop. Never causal unless `causal` is set. */
  sentence: string;
  causal: { assertedBy: string; statement: string | null } | null;
  dates: string;
  undated: boolean;
  citations: Citation[];
  /** The server's own sentence for this hop (LINKS fr text), shown as a secondary line. */
  serverText: string | null;
  via: string[];
}

export interface PathView {
  hops: HopView[];
  nodes: { id: string; label: string }[];
  cost: number | null;
}

export interface PathsView {
  engine: 'links' | 'kg';
  found: boolean;
  reason: string | null;
  truncated: boolean;
  paths: PathView[];
  disclaimer: string;
}

export const PATH_NOT_CAUSE = 'Un chemin n’est pas une cause : chaque étape dit seulement « relié via » une déclaration sourcée.';

function sourceLabel(source: string): string {
  return isWikidataSource(source) ? 'Wikidata (contribution communautaire, non vérifiée)' : source;
}

export function linkDates(t: LinkTemporal): { text: string; undated: boolean } {
  if (t.kind === 'undated') return { text: 'non daté dans la source', undated: true };
  if (t.kind === 'snapshot') return { text: t.year !== null && t.year !== undefined ? formatYear(t.year) : '', undated: false };
  if (t.kind === 'point') return { text: formatYear(yearOfPartial(t.point)), undated: false };
  const a = yearOfPartial(t.start);
  const b = yearOfPartial(t.end);
  if (a !== null && b !== null) return { text: a === b ? formatYear(a) : `${formatYear(a)} – ${formatYear(b)}`, undated: false };
  if (a !== null) return { text: `depuis ${formatYear(a)}`, undated: false };
  return { text: `jusqu’en ${formatYear(b)}`, undated: false };
}

/** "Brésil — langue officielle → portugais (…) [cite]" -> "langue officielle". */
export function relationFromText(text: string | null | undefined): string | null {
  if (!text) return null;
  const m = /\s—\s(.+?)\s→\s/.exec(text);
  return m ? m[1]!.trim() : null;
}

/** French labels of properties whose hop text is not of the "A — label → B" form (causal ones). */
export const PID_LABEL_FR: Record<string, string> = {
  P828: 'a pour cause',
  P1542: 'a pour effet',
  P1478: 'a pour cause immédiate',
  P1479: 'a pour cause contributive',
  P1536: 'a pour cause sous-jacente',
  P1537: 'a pour effet contributif',
};

export function connectedSentence(relation: string): string {
  return `relié via « ${relation} »`;
}

export function fromLinks(d: LinksPathData): PathsView {
  const L = (id: string) => ({ id, label: linkNodeLabel(d.labels, id) });
  return {
    engine: 'links',
    found: d.found,
    reason: d.reason ?? null,
    truncated: d.truncated,
    disclaimer: d.disclaimer?.fr || PATH_NOT_CAUSE,
    paths: d.paths.map((p) => ({
      cost: p.cost ?? null,
      nodes: p.nodes.map(L),
      hops: p.hops.map((h) => {
        // The server's hop text carries the label in the traversal direction (inverse labels for
        // backward hops, e.g. "ancienne puissance coloniale de"); the type tables are fallbacks.
        const typeLabel = h.kind === 'link' ? LINK_TYPE_LABEL[h.type] : PID_LABEL_FR[h.type];
        const relation =
          relationFromText(h.text.fr) ?? (typeLabel ? (h.forward ? typeLabel : `${typeLabel} (sens inverse)`) : null) ?? relationFromText(h.text.en) ?? h.type;
        const citations = h.evidence.map(citeLinkEvidence);
        const stmtEvidence = h.evidence.find((e) => e.kind === 'kg_statement');
        const isCausal = h.causal === true && h.kind === 'statement' && !!stmtEvidence;
        const dates = linkDates(h.temporal);
        const from = L(h.from);
        const to = L(h.to);
        const causal = isCausal ? { assertedBy: sourceLabel(stmtEvidence!.source), statement: stmtEvidence!.statement_id ?? null } : null;
        return {
          from,
          to,
          relation,
          sentence: causal
            ? `selon ${causal.assertedBy}, ${from.label} — ${relation} — ${to.label} (relation causale affirmée par la source, non établie par REVA)`
            : connectedSentence(relation),
          causal,
          dates: dates.text,
          undated: dates.undated,
          citations,
          serverText: h.text.fr ?? null,
          via: (h.via ?? []).map((v) => L(v).label),
        };
      }),
    })),
  };
}

export function fromKg(d: KgPathResult): PathsView {
  return {
    engine: 'kg',
    found: d.found,
    reason: d.not_found_reason ?? null,
    truncated: !!(d.search && (d.search as { truncated?: boolean }).truncated),
    disclaimer: PATH_NOT_CAUSE,
    paths: d.paths.map((p) => {
      const nodes = p.hops.length ? [p.hops[0]!.from, ...p.hops.map((h) => h.to)] : [];
      return {
        cost: null,
        nodes: nodes.map((n) => ({ id: n.qid, label: n.label })),
        hops: p.hops.map((h) => {
          const relation = h.relation_label_fr || h.relation_label_en || h.property.pid;
          const t = temporalOf(h);
          const isCausal = h.causal_assertion !== null && h.property.causal_assertion === true;
          const asserted = isCausal ? String((h.causal_assertion as Record<string, unknown>).asserted_by ?? h.provenance.source_key) : '';
          const causal = isCausal
            ? { assertedBy: sourceLabel(asserted), statement: String((h.causal_assertion as Record<string, unknown>).statement_key ?? h.provenance.statement_key) }
            : null;
          return {
            from: { id: h.from.qid, label: h.from.label },
            to: { id: h.to.qid, label: h.to.label },
            relation,
            sentence: causal
              ? `selon ${causal.assertedBy}, ${h.subject.label} — ${h.property.label_fr || h.property.label_en || h.property.pid} — ${h.object?.label ?? ''} (relation causale affirmée par la source, non établie par REVA)`
              : connectedSentence(relation),
            causal,
            dates: temporalLabel(t),
            undated: t.kind === 'undated',
            citations: [citeKgStatement(h)],
            serverText: null,
            via: [],
          };
        }),
      };
    }),
  };
}

export function reasonFr(reason: string | null): string {
  switch (reason) {
    case 'no_path_within_max_hops':
      return `aucun chemin trouvé dans la limite d’étapes demandée (cela ne prouve pas l’absence de lien)`;
    case 'unknown_node':
      return 'entité inconnue du graphe';
    case null:
    case undefined:
    case '':
      return 'aucun chemin trouvé (cela ne prouve pas l’absence de lien)';
    default:
      return reason.replace(/_/g, ' ');
  }
}

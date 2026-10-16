/**
 * "Pays ↔ Pays" view model (/v1/links/pair): every link kind between two countries, grouped into
 * themes, each link with its dates and citations.
 *
 * Honesty rules applied here (V3 "Honesty rules"):
 *  - a link is "relié via", never "parce que" (no causal wording is produced);
 *  - a quantity is shown exactly as the source reports it (decimal string, own unit), per direction —
 *    mirror flows (exports reported by A vs imports reported by B) are separate series, never merged;
 *  - vote agreement is a computed "part de votes identiques" with its counts and caveat — never an
 *    alliance, friendship or alignment;
 *  - a kind without links is "aucun lien dans les données consultées", never zero;
 *  - a link with a predecessor state is shown with the succession chain that makes it relevant;
 *  - physical networks (LINKS3) keep their caveats in view: cables are Wikidata's community-edited list (cable
 *    names, earliest service year, and the cited Q55 → Royaume des Pays-Bas hop when it was used), air routes
 *    are OpenFlights' network frozen in June 2014 (ODbL), launch links name the site state per GCAT — never
 *    the launcher's supplier;
 *  - EVMAP: sanctions episodes are coded cases (TIES / EUSANCT, one series per dataset — they overlap and are never
 *    added) and active OFAC programs, sender → target, a threat or imposition, never an effect; COLDAT and COW
 *    entities colonial rule sit next to CShapes in the colonial lane; a tsunami observed in two countries is
 *    "même événement naturel observé", never a cause.
 */
import { linkNodeLabel, linkTypeLabel, type PairKind, type PairLink, type PairLinksData } from '../api/links';
import { citeLinkEvidence, type Citation } from './citations';
import { frDecimal, frValue } from './format';
import { linkDates } from './paths';
import { yearOfPartial } from './time';

export type ThemeId = 'trade' | 'migration' | 'votes' | 'colonial' | 'border' | 'language' | 'treaty' | 'conflict' | 'peacekeeping' | 'sanctions' | 'infrastructure' | 'hazard' | 'diplomatic' | 'history' | 'other';

export const THEMES: { id: ThemeId; label: string; kinds: string[] }[] = [
  { id: 'trade', label: 'Commerce de biens', kinds: ['bilateral_trade', 'bilateral_trade_imports', 'bilateral_trade_baci'] },
  { id: 'migration', label: 'Migrations (stocks de migrants)', kinds: ['migrant_stock'] },
  { id: 'votes', label: 'Votes à l’Assemblée générale de l’ONU', kinds: ['unga_vote_agreement'] },
  { id: 'colonial', label: 'Domination coloniale', kinds: ['colony_of', 'former_colony_of', 'colonial_rule_cshapes', 'colonial_rule_cow', 'colonised_by_coldat'] },
  { id: 'border', label: 'Frontières', kinds: ['land_border', 'land_border_cshapes', 'border_wikidata', 'maritime_neighbour'] },
  { id: 'language', label: 'Langues', kinds: ['shared_official_language', 'shared_language_used', 'shared_language_family', 'shared_language_glottolog'] },
  { id: 'treaty', label: 'Traités et organisations', kinds: ['co_party', 'co_signatory', 'co_member', 'alliance_co_member'] },
  { id: 'conflict', label: 'Conflits armés (UCDP, Wikidata)', kinds: ['conflict_same_side', 'conflict_opposing_sides', 'conflict_co_participant'] },
  { id: 'peacekeeping', label: 'Maintien de la paix (ONU)', kinds: ['peacekeeping_contributor_to_host'] },
  { id: 'sanctions', label: 'Sanctions (épisodes codés TIES / EUSANCT, programmes OFAC)', kinds: ['sanction_episode', 'us_sanctions_program'] },
  { id: 'infrastructure', label: 'Réseaux physiques (câbles sous-marins, vols de 2014, lancements spatiaux)', kinds: ['submarine_cable', 'air_routes_2014', 'launch_service'] },
  { id: 'hazard', label: 'Catastrophes naturelles (même événement observé dans les deux pays)', kinds: ['hazard_shared_event'] },
  { id: 'diplomatic', label: 'Relations diplomatiques', kinds: ['diplomatic_relation', 'shared_head_of_state', 'shared_currency', 'shared_legal_family'] },
  { id: 'history', label: 'Histoire partagée', kinds: ['successor_of', 'shared_polity_territory'] },
];

export function themeOf(kind: string): ThemeId {
  return THEMES.find((t) => t.kinds.includes(kind))?.id ?? 'other';
}

export const VOTE_CAVEAT =
  'Indice calculé par REVA à partir des votes nominaux (Voeten et al.) : part des votes où les deux pays ont voté pareil (oui, non ou abstention) parmi ceux où les deux ont voté. C’est une part de votes identiques au sens strict (une abstention face à un oui ou un non compte 0) — ce n’est pas l’indice « agree » de Voeten et Bailey, qui compte une abstention pour moitié : les valeurs ne sont pas comparables à celles de la littérature. Elle ne mesure ni une amitié ni une convergence d’intérêts ; le choix des votes et le codage par thème l’influencent. La période affichée est celle des votes réellement utilisés (décennie partielle signalée).';

export const ISSUE_LABEL: Record<string, string> = {
  all: 'tous les votes',
  co: 'colonialisme',
  hr: 'droits humains',
  me: 'conflit israélo-palestinien',
  nu: 'armes nucléaires',
  di: 'désarmement',
  ec: 'développement économique',
};

export interface LinkItem {
  key: string;
  kind: string;
  /** "Brésil — ancienne colonie de (via Brésil colonial) → Empire colonial portugais". */
  sentence: string;
  from: string;
  to: string;
  dates: string;
  undated: boolean;
  yearFrom: number | null;
  yearTo: number | null;
  value: string | null;
  citations: Citation[];
  serverText: string | null;
}

export interface SeriesPoint {
  year: number;
  value: string;
  shown: string;
  citations: Citation[];
}

export interface Series {
  key: string;
  kind: string;
  label: string;
  from: string;
  to: string;
  facet: string | null;
  unit: string | null;
  points: SeriesPoint[];
}

export interface VoteRow {
  key: string;
  period: string;
  grain: string;
  issue: string;
  share: string;
  shareShown: string;
  nCommon: number | null;
  nIdentical: number | null;
  citations: Citation[];
}

export interface KindView {
  kind: string;
  label: string;
  theme: ThemeId;
  count: number;
  firstYear: number | null;
  lastYear: number | null;
  directed: boolean;
  series: Series[];
  votes: VoteRow[];
  items: LinkItem[];
  honesty: string | null;
  /** Caveats and licence the server stores on every link of the kind (basis.caveat.fr, basis.licence). */
  notes: string[];
}

export interface PredecessorView {
  key: string;
  country: string;
  predecessorOf: string;
  predecessor: string;
  link: LinkItem;
  succession: LinkItem[];
}

export interface RelationView {
  a: string;
  b: string;
  aLabel: string;
  bLabel: string;
  themes: { id: ThemeId; label: string; kinds: KindView[] }[];
  predecessors: PredecessorView[];
  without: { kind: string; label: string }[];
  truncated: string[];
  span: [number, number] | null;
  storeLabel: string;
}

const ISO_NODE = /^iso3:([A-Z]{3})$/;

function nodeName(labels: PairLinksData['labels'], id: string, names: Record<string, string>): string {
  const iso = ISO_NODE.exec(id)?.[1];
  if (iso && names[iso]) return names[iso]!;
  const l = labels[id];
  if (l && (l.fr || l.en)) return linkNodeLabel(labels, id);
  return iso ?? id;
}

function isSeries(k: PairKind): boolean {
  return k.links.length > 0 && k.links.every((l) => l.temporal.kind === 'snapshot' && l.weight !== null && l.weight !== undefined);
}

function linkItem(l: PairLink, kindLabel: string, labels: PairLinksData['labels'], names: Record<string, string>, directed: boolean, i: number): LinkItem {
  const from = nodeName(labels, l.subject, names);
  const to = nodeName(labels, l.object, names);
  const via = l.via.length ? ` (via ${l.via.map((v) => nodeName(labels, v, names)).join(', ')})` : '';
  const t = l.temporal;
  const dates = censoredDates(l, linkDates(t));
  const phys = physicalDetail(l);
  if (!phys.text) phys.text = datasetDetail(l);
  const yearFrom = phys.since ?? (t.kind === 'snapshot' ? (t.year ?? null) : t.kind === 'point' ? yearOfPartial(t.point) : yearOfPartial(t.start));
  const yearTo = t.kind === 'snapshot' ? (t.year ?? null) : t.kind === 'point' ? yearOfPartial(t.point) : yearOfPartial(t.end);
  return {
    key: `${l.link_type}-${l.subject}-${l.object}-${i}`,
    kind: l.link_type,
    sentence: `${from} — ${kindLabel}${via} ${directed ? '→' : '—'} ${to}${phys.text ? ` : ${phys.text}` : ''}`,
    from,
    to,
    dates: dates.text,
    undated: dates.undated,
    yearFrom,
    yearTo,
    value: l.weight ? `${frValue(l.weight, l.unit)}${l.unit ? ` ${l.unit}` : ''}` : null,
    citations: l.evidence.map(citeLinkEvidence),
    serverText: l.text?.fr ?? null,
  };
}

/**
 * CSH-1 (review V3b): a period that starts on the dataset's first day (CShapes 2.0: 1886-01-01) is left-censored —
 * the situation may be older (Algeria: French rule from 1830). Its start is shown as "≤ 1886", never as a date.
 */
export function censoredDates(l: PairLink, d: { text: string; undated: boolean }): { text: string; undated: boolean } {
  const b = (l.basis ?? {}) as Record<string, unknown>;
  if (b.left_censored !== true || l.temporal.kind !== 'interval') return d;
  const a = yearOfPartial(l.temporal.start);
  const z = yearOfPartial(l.temporal.end);
  const since = typeof b.coverage_start === 'string' ? b.coverage_start : String(a ?? '');
  const span = z !== null ? `≤ ${a} – ${z}` : `depuis ≤ ${a}`;
  return { text: `${span} (début de couverture de la source au ${since} : la situation peut être antérieure)`, undated: d.undated };
}

const PHYSICAL_ITEMS = new Set(['submarine_cable', 'air_routes_2014']);

const rec = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
const int = (v: unknown): number | null => (typeof v === 'number' && Number.isInteger(v) ? v : typeof v === 'string' && /^-?\d+$/.test(v) ? Number(v) : null);

/**
 * Details of a physical-network link read from its basis (LINKS3): the cables Wikidata lists (names, earliest
 * service year — the lane starts there), the cited mapping hop (Q55 → Royaume des Pays-Bas), the 2014 route
 * counts. Nothing is invented: a missing basis member is simply not mentioned.
 */
export function physicalDetail(l: PairLink): { text: string; since: number | null } {
  const b = rec(l.basis);
  const parts: string[] = [];
  let since: number | null = null;
  if (l.link_type === 'submarine_cable') {
    const cables = Array.isArray(rec(b.source_flags).cables) ? (rec(b.source_flags).cables as unknown[]) : [];
    const names = cables.map((c) => String(rec(c).label ?? rec(c).qid ?? '')).filter(Boolean);
    const total = int(rec(b.source_flags).cables_total);
    if (Array.isArray(b.items)) parts.push(`${b.items.length} élément(s) Wikidata distinct(s) (union des listes, jamais additionnées)`);
    else if (names.length) parts.push(`${names.join(', ')}${total !== null && total > names.length ? ` (+${total - names.length})` : ''}`);
    since = int(b.first_service_year);
    if (since !== null) parts.push(`mise en service la plus ancienne : ${since}`);
    const hop = rec(b.mapping_hop);
    if (hop.statement_id) {
      const pl = rec(hop.provider_label);
      parts.push(`rattaché à ${String(hop.iso3 ?? '')} via ${String(pl.fr ?? hop.provider_code ?? '')} — pays (P17) → ${String(hop.target_qid ?? '')} [${String(hop.statement_id)}]`);
    }
  } else if (l.link_type === 'air_routes_2014') {
    const ap = int(b.airport_pairs);
    const al = int(b.airlines);
    if (ap !== null) parts.push(`${ap} paire(s) d’aéroports`);
    if (al !== null) parts.push(`${al} compagnie(s)`);
    parts.push('réseau de juin 2014, données figées');
  }
  return { text: parts.join(' ; '), since };
}

/** Dataset names of a `source=` facet (one link type fed by several datasets: their values are never added). */
export const SOURCE_FACET_LABEL: Record<string, string> = {
  'source=ties': 'jeu de données TIES (1945-2005)',
  'source=eusanct': 'jeu de données EUSANCT (1989-2015)',
};

/** Facet shown next to a series: a dataset name for `source=` facets, else the facet as served. */
export function facetLabel(facet: string | null): string | null {
  if (!facet) return null;
  if (facet.startsWith('source=')) return SOURCE_FACET_LABEL[facet] ?? `jeu de données ${facet.slice(7)}`;
  return facet;
}

/**
 * EVMAP details read from the basis of dataset links (nothing invented: a missing member is not mentioned):
 * the runup records of a tsunami observed in the other country, COLDAT's date aggregation(s), the COW status.
 */
export function datasetDetail(l: PairLink): string {
  const b = rec(l.basis);
  if (l.link_type === 'hazard_shared_event') {
    const parts = ['même événement naturel observé : source dans le premier pays, vagues relevées dans le second'];
    const n = int(b.runup_records);
    if (n !== null) parts.push(`${n} relevé(s) de vagues (NCEI)`);
    const deaths = int(b.runup_deaths_published_sum);
    if (deaths !== null) parts.push(`${deaths} décès publiés pour ces relevés`);
    return parts.join(' ; ');
  }
  if (l.link_type === 'colonised_by_coldat' && Array.isArray(b.aggregations) && b.aggregations.length)
    return `COLDAT, agrégation des dates : ${(b.aggregations as unknown[]).map(String).join(' et ')}`;
  if (l.link_type === 'colonial_rule_cow' && typeof b.status === 'string') return `statut : ${b.status} (liste des entités COW)`;
  return '';
}

/** The caveat (fr) and licence stored on the links of a kind, once each. */
function notesOf(k: PairKind): string[] {
  const out = new Set<string>();
  for (const l of k.links) {
    const b = rec(l.basis);
    const c = rec(b.caveat);
    if (typeof c.fr === 'string' && c.fr) out.add(c.fr);
    if (typeof b.licence === 'string' && b.licence) out.add(`Licence : ${b.licence}`);
  }
  return [...out];
}

function seriesOf(k: PairKind, kind: string, label: string, labels: PairLinksData['labels'], names: Record<string, string>): Series[] {
  const by = new Map<string, Series>();
  for (const l of k.links) {
    const facet = l.facet || null;
    const key = `${l.subject}>${l.object}|${facet ?? ''}`;
    let s = by.get(key);
    if (!s) {
      s = { key: `${kind}-${key}`, kind, label, from: nodeName(labels, l.subject, names), to: nodeName(labels, l.object, names), facet: facetLabel(facet), unit: l.unit ?? null, points: [] };
      by.set(key, s);
    }
    s.points.push({ year: l.temporal.year as number, value: l.weight!, shown: frValue(l.weight!, l.unit), citations: l.evidence.map(citeLinkEvidence) });
  }
  const out = [...by.values()];
  for (const s of out) s.points.sort((x, y) => x.year - y.year);
  // default facet (all goods) first, then the rest; direction a→b before b→a
  return out.sort((x, y) => Number(!!x.facet && x.facet !== 'section=TOTAL') - Number(!!y.facet && y.facet !== 'section=TOTAL') || x.key.localeCompare(y.key));
}

function voteRows(k: PairKind): VoteRow[] {
  return k.links
    .map((l, i) => {
      const b = (l.basis ?? {}) as Record<string, unknown>;
      const issue = String(b.issue ?? (l.facet ?? '').replace(/^issue=/, '') ?? 'all') || 'all';
      const grain = String(b.grain ?? 'decade');
      // VOTE-1/2: the years the value was computed on (server: decade ∩ requested window ∩ data coverage)
      const used = Array.isArray(b.years_used) ? (b.years_used as unknown[]).map((x) => (typeof x === 'number' ? x : null)) : [];
      const a = used[0] ?? yearOfPartial(l.temporal.start);
      const z = used[1] ?? yearOfPartial(l.temporal.end);
      const span = grain === 'year' || a === z ? String(a ?? '') : `${a ?? '…'}–${z ?? '…'}`;
      const period = b.partial_period === true ? `${span} (décennie partielle)` : span;
      const num = (v: unknown) => (typeof v === 'number' ? v : typeof v === 'string' && /^\d+$/.test(v) ? Number(v) : null);
      return {
        key: `vote-${i}`,
        period,
        grain,
        issue,
        share: l.weight ?? '',
        shareShown: l.weight ? frDecimal(l.weight, 4) : '',
        nCommon: num(b.n_common),
        nIdentical: num(b.n_identical),
        citations: l.evidence.map(citeLinkEvidence),
      };
    })
    .sort((x, y) => (x.issue === 'all' ? 0 : 1) - (y.issue === 'all' ? 0 : 1) || x.issue.localeCompare(y.issue) || x.grain.localeCompare(y.grain) || x.period.localeCompare(y.period));
}

export function buildRelation(d: PairLinksData, names: Record<string, string> = {}): RelationView {
  const nm: Record<string, string> = { ...names };
  // the French KG label of the node that holds each country's ISO3 in the served links, else the crosswalk name
  for (const k of Object.values(d.kinds))
    for (const l of k.links) {
      const [ia, ib] = l.iso3 ?? [];
      for (const [iso, node] of [
        [ia, l.subject],
        [ib, l.object],
      ] as const)
        if (iso && !nm[iso] && d.labels[node]?.fr) nm[iso] = d.labels[node]!.fr!;
    }
  for (const iso of [d.a, d.b]) if (!nm[iso]) nm[iso] = d.labels[`iso3:${iso}`]?.fr || d.labels[`iso3:${iso}`]?.en || iso;
  const kinds: KindView[] = Object.entries(d.kinds).map(([kind, k]) => {
    const label = linkTypeLabel(kind, k.labels);
    const directed = !!k.directed;
    const base: KindView = {
      kind,
      label,
      theme: themeOf(kind),
      count: k.count,
      firstYear: k.first_year ?? null,
      lastYear: k.last_year ?? null,
      directed,
      series: [],
      votes: [],
      items: [],
      honesty: k.honesty?.fr ?? null,
      notes: notesOf(k),
    };
    if (kind === 'unga_vote_agreement') return { ...base, votes: voteRows(k) };
    if (PHYSICAL_ITEMS.has(kind)) return { ...base, items: k.links.map((l, i) => linkItem(l, label, d.labels, nm, directed, i)) };
    if (isSeries(k)) return { ...base, series: seriesOf(k, kind, label, d.labels, nm) };
    return { ...base, items: k.links.map((l, i) => linkItem(l, label, d.labels, nm, directed, i)) };
  });
  const themes = [...THEMES, { id: 'other' as ThemeId, label: 'Autres liens', kinds: [] as string[] }]
    .map((t) => ({ id: t.id, label: t.label, kinds: kinds.filter((k) => k.theme === t.id).sort((x, y) => x.label.localeCompare(y.label)) }))
    .filter((t) => t.kinds.length > 0);
  const predecessors = d.predecessor_links.map((p, i) => ({
    key: `pred-${i}`,
    country: nm[p.country] ?? p.country,
    predecessorOf: nm[p.predecessor_of] ?? p.predecessor_of,
    predecessor: nodeName(d.labels, p.predecessor, nm),
    link: linkItem(p.link, linkTypeLabel(p.link.link_type), d.labels, nm, true, i),
    succession: p.succession.map((s, j) => linkItem(s, linkTypeLabel(s.link_type), d.labels, nm, true, j)),
  }));
  const years = kinds.flatMap((k) => [k.firstYear, k.lastYear]).filter((y): y is number => y !== null);
  const store = d.store ?? null;
  return {
    a: d.a,
    b: d.b,
    aLabel: nm[d.a]!,
    bLabel: nm[d.b]!,
    themes,
    predecessors,
    without: d.kinds_without_links.map((kind) => ({ kind, label: linkTypeLabel(kind) })),
    truncated: d.truncated,
    span: years.length ? [Math.min(...years), Math.max(...years)] : null,
    storeLabel: store
      ? `magasin de liens n° ${String(store.materialisation_id ?? store.id ?? '?')}${store.atlas_snapshot_sha256 ? `, instantané ${String(store.atlas_snapshot_sha256).slice(0, 12)}…` : ''}`
      : 'liens dérivés à la demande (aucun magasin de liens matérialisé)',
  };
}

/** Lane marks for the overview timeline: one lane per kind, positions in % of the span. */
export function laneMarks(k: KindView, span: [number, number]): { left: number; width: number; title: string }[] {
  const [lo, hi] = span;
  const w = Math.max(1, hi - lo);
  const pos = (y: number) => ((y - lo) / w) * 100;
  const out: { left: number; width: number; title: string }[] = [];
  const add = (a: number | null, b: number | null, title: string) => {
    if (a === null && b === null) return;
    const x = a ?? lo;
    const z = b ?? hi;
    out.push({ left: pos(x), width: Math.max(0.8, pos(z) - pos(x)), title });
  };
  for (const s of k.series) for (const p of s.points) add(p.year, p.year, `${s.from} → ${s.to} ${p.year}`);
  for (const v of k.votes) {
    const [a, b] = v.period.split('–').map((x) => (/^-?\d+$/.test(x) ? Number(x) : null));
    add(a ?? null, b ?? a ?? null, `${v.period} : ${v.shareShown}`);
  }
  for (const it of k.items) add(it.yearFrom, it.yearTo, `${it.sentence} (${it.dates})`);
  return out;
}

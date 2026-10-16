import { useMemo, useState, type FormEvent } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { getLocations } from '../../api/client';
import { useApi, type ApiState } from '../../lib/useApi';
import { conceptLabel, getAtlasCatalog, getAtlasDossier, type AtlasConcept, type AtlasDossierConcept, type AtlasDossierDomain } from '../api/atlas';
import { getKgTimeline } from '../api/kg';
import { getCountryLinks, LINK_STATUS_LABEL, linkNodeLabel, linkTypeLabel, type CountryLink, type CountryLinksResponse } from '../api/links';
import { CitationChip } from '../components/CitationChip';
import { FixtureBadge, MissingCell, ProviderDeclaredBadge } from '../components/Honesty';
import { Sparkline } from '../components/Sparkline';
import { V3Error, V3Loading } from '../components/States';
import { citeAtlasCell, citeLinkEvidence } from '../lib/citations';
import { iso3OfFeatureKey } from '../lib/choropleth';
import { DOMAIN_ORDER, domainLabel, domainRank } from '../lib/domains';
import { cellText } from '../lib/choropleth';
import { flagLines, frDecimal, frValue } from '../lib/format';
import { linkDates } from '../lib/paths';
import { THEMES, themeOf } from '../lib/relation';
import { buildTimeline } from '../lib/timeline';

export default function DossierPage() {
  const { iso3: raw = 'FRA' } = useParams();
  const iso3 = raw.toUpperCase();
  const navigate = useNavigate();
  const dossier = useApi((s) => getAtlasDossier(iso3, { series: true }, s), [iso3]);
  const concepts = useApi(() => getAtlasCatalog(), []);
  const links = useApi((s) => getCountryLinks(iso3, {}, s), [iso3]);
  const locations = useApi((s) => getLocations(undefined, s), []);
  const [pick, setPick] = useState('');

  const countryNames = useMemo(() => {
    const m = new Map<string, string>();
    if (locations.status === 'ok')
      for (const f of locations.data.data.features) {
        const iso = iso3OfFeatureKey(f.properties.feature_key);
        if (iso && !m.has(iso)) m.set(iso, f.properties.name ?? iso);
      }
    return m;
  }, [locations.status, locations.data]);
  const conceptById = useMemo(() => new Map((concepts.status === 'ok' ? concepts.data.data : []).map((c) => [c.id, c])), [concepts.status, concepts.data]);
  const label = (dossier.status === 'ok' && dossier.data.data.label) || countryNames.get(iso3) || iso3;
  const domains = useMemo(
    () => (dossier.status === 'ok' ? [...dossier.data.data.domains].sort((a, b) => domainRank(a.domain) - domainRank(b.domain) || a.domain.localeCompare(b.domain)) : []),
    [dossier.status, dossier.data],
  );

  const go = (e: FormEvent) => {
    e.preventDefault();
    const v = pick.trim();
    const iso = /^[A-Za-z]{3}$/.test(v) ? v.toUpperCase() : [...countryNames.entries()].find(([, n]) => n.toLowerCase() === v.toLowerCase())?.[0];
    if (iso) navigate(`/v3/pays/${iso}`);
  };

  return (
    <section className="v3-page v3-dossier" aria-labelledby="dossier-title">
      <header className="v3-page-head v3-dossier-head">
        <div>
          <h1 id="dossier-title">
            {label} <span className="muted">{iso3}</span>
          </h1>
          <p className="v3-lede">Dossier pays : chaque domaine, chaque valeur avec sa source et son année ; les cellules absentes sont listées avec les sources consultées.</p>
        </div>
        <form onSubmit={go} className="v3-inline-form" role="search" aria-label="Changer de pays">
          <label>
            Autre pays
            <input list="v3-countries" value={pick} onChange={(e) => setPick(e.target.value)} placeholder="nom ou ISO3" />
          </label>
          <datalist id="v3-countries">
            {[...countryNames.entries()].map(([iso, n]) => (
              <option key={iso} value={n}>
                {iso}
              </option>
            ))}
          </datalist>
          <button type="submit">Ouvrir</button>
          <Link to={`/v3/atlas`}>Carte</Link>
        </form>
      </header>
      {dossier.status === 'ok' && (
        <nav className="v3-toc" aria-label="Sections du dossier">
          <ul>
            {domains.map((d) => (
              <li key={d.domain}>
                <a href={`#dom-${d.domain}`}>
                  {domainLabel(d.domain)} <span className="muted">{d.concepts.length}/{d.concepts.length + d.without_data.length}</span>
                </a>
              </li>
            ))}
            <li>
              <a href="#dom-kg-history">Histoire (frise)</a>
            </li>
            <li>
              <a href="#dom-links">Liens avec d’autres pays</a>
            </li>
          </ul>
        </nav>
      )}
      {dossier.status === 'loading' && <V3Loading what="du dossier" />}
      {dossier.status === 'error' && <V3Error error={dossier.error} what={`Le dossier atlas de ${iso3}`} onRetry={dossier.reload} />}
      {dossier.status === 'ok' && (
        <>
          <p className="v3-meta">
            {dossier.data.data.concepts_with_data} variables renseignées · {dossier.data.data.concepts_without_data} sans donnée · instantané{' '}
            {dossier.data.snapshot.snapshot_sha256.slice(0, 12)}… arrêté au {dossier.data.snapshot.knowledge_cutoff.slice(0, 10)}{' '}
            {dossier.data.snapshot.origin === 'test_fixture' && <FixtureBadge />} <ProviderDeclaredBadge />
          </p>
          <p className="field-hint">
            Chaque cellule est ce qu’une source rapporte ; la « meilleure disponible » suit l’ordre de sources déclaré du concept, sans mélange, imputation
            ni zéro de remplissage.
            {dossier.data.notes.length > 0 && (
              <details>
                <summary>Notes du serveur</summary>
                {dossier.data.notes.map((n) => (
                  <p key={n}>{n}</p>
                ))}
              </details>
            )}
          </p>
          {domains.map((d) => (
            <DomainSection key={d.domain} d={d} conceptById={conceptById} />
          ))}
          {DOMAIN_ORDER.filter((o) => !domains.some((d) => d.domain === o.id || (o.aliases ?? []).includes(d.domain))).map((o) => (
            <section key={o.id} id={`dom-${o.id}`} className="v3-domain v3-domain-empty" aria-labelledby={`h-${o.id}`}>
              <h2 id={`h-${o.id}`}>{o.label}</h2>
              <p className="muted">Aucune variable de ce domaine n’est encore au catalogue de l’atlas : rien à afficher (ce n’est pas une valeur nulle).</p>
            </section>
          ))}
        </>
      )}
      <KgHistory links={links} />
      <CountryLinks links={links} iso3={iso3} />
    </section>
  );
}

function DomainSection({ d, conceptById }: { d: AtlasDossierDomain; conceptById: Map<string, AtlasConcept> }) {
  const bySub = useMemo(() => {
    const m = new Map<string, AtlasDossierConcept[]>();
    for (const c of d.concepts) m.set(c.subdomain ?? '', [...(m.get(c.subdomain ?? '') ?? []), c]);
    return [...m.entries()];
  }, [d]);
  return (
    <section id={`dom-${d.domain}`} className="v3-domain" aria-labelledby={`h-${d.domain}`} data-domain={d.domain}>
      <h2 id={`h-${d.domain}`}>
        {domainLabel(d.domain)} <span className="muted">· {d.concepts.length} renseignée(s), {d.without_data.length} sans donnée</span>
      </h2>
      {d.concepts.length > 0 && (
        <table className="v3-cells">
          <thead>
            <tr>
              <th scope="col">Variable</th>
              <th scope="col">Dernière valeur</th>
              <th scope="col">Année</th>
              <th scope="col">Source</th>
              <th scope="col">Série</th>
            </tr>
          </thead>
          {bySub.map(([sub, list]) => (
            <tbody key={sub || '-'}>
              {sub && (
                <tr className="v3-subdomain">
                  <th colSpan={5} scope="rowgroup">
                    {sub.replace(/_/g, ' ')}
                  </th>
                </tr>
              )}
              {list.map((c) => (
                <ConceptRow key={c.concept_id} c={c} def={conceptById.get(c.concept_id)} />
              ))}
            </tbody>
          ))}
        </table>
      )}
      {d.without_data.length > 0 && (
        <details className="v3-missing-list" open={d.concepts.length === 0}>
          <summary>
            Sans donnée pour ce pays ({d.without_data.length}) — sources consultées listées
          </summary>
          <ul data-testid={`missing-${d.domain}`}>
            {d.without_data.map((id) => {
              const c = conceptById.get(id);
              return (
                <li key={id} data-missing-concept={id}>
                  <strong>{c ? conceptLabel(c) : id}</strong> <MissingCell reason="no_declared_source" />
                  <span className="v3-searched">
                    {' '}
                    sources consultées : {c ? c.source_order.join(', ') || 'aucune source déclarée' : 'catalogue non chargé'}
                  </span>
                </li>
              );
            })}
          </ul>
        </details>
      )}
    </section>
  );
}

function ConceptRow({ c, def }: { c: AtlasDossierConcept; def?: AtlasConcept }) {
  const cell = c.latest;
  const flags = flagLines(cell.flags);
  const hasValue = cell.value !== null || !!cell.value_text;
  return (
    <tr data-concept={c.concept_id}>
      <th scope="row" title={def?.definition}>
        {conceptLabel(c)}
        <span className="v3-unit"> ({c.unit})</span>
      </th>
      <td className="v3-num" title={cell.value ?? cell.value_text ?? ''}>
        {hasValue ? cellText(cell) : <MissingCell reason={typeof cell.flags.missing === 'string' ? cell.flags.missing : null} />}
        {flags.length > 0 && <span className="v3-flags"> {flags.join(' · ')}</span>}
      </td>
      <td>
        {cell.year ?? cell.period_start.slice(0, 4)}
        <span className="muted"> · {c.years} an(s) {c.year_min}–{c.year_max}</span>
      </td>
      <td>
        <CitationChip c={citeAtlasCell(cell)} />
        {c.sources_used.length > 1 && <span className="muted"> · série issue de {c.sources_used.length} sources (par ordre déclaré, jamais mélangées dans une année)</span>}
      </td>
      <td>{c.series && <Sparkline points={c.series} latestSeries={cell.series} />}</td>
    </tr>
  );
}

function KgHistory({ links }: { links: ApiState<CountryLinksResponse> }) {
  const qid = links.status === 'ok' ? links.data.data.qid : null;
  const tl = useApi((s) => getKgTimeline(qid!, { limit: 400 }, s), [qid], !!qid);
  const model = useMemo(() => (tl.status === 'ok' && qid ? buildTimeline(qid, tl.data.data.events) : null), [tl.status, tl.data, qid]);
  return (
    <section id="dom-kg-history" className="v3-domain" aria-labelledby="h-kg-history">
      <h2 id="h-kg-history">Histoire — frise (graphe de connaissances)</h2>
      {links.status === 'loading' && <V3Loading what="de l’identifiant Wikidata du pays" />}
      {links.status === 'error' && <V3Error error={links.error} what="L’identifiant Wikidata du pays (via /v1/links)" />}
      {tl.status === 'loading' && <V3Loading what="de la frise" />}
      {tl.status === 'error' && <V3Error error={tl.error} what="La frise" />}
      {model && (
        <>
          <p>
            {model.items.length} élément(s) daté(s){model.undated.length ? `, ${model.undated.length} non daté(s)` : ''}.{' '}
            <Link to={`/v3/frise?qid=${encodeURIComponent(qid!)}`}>Ouvrir la frise complète</Link> ·{' '}
            <Link to={`/v3/neurones?qid=${encodeURIComponent(qid!)}`}>Explorer en neurones</Link>
          </p>
          <ol className="v3-mini-timeline">
            {model.items.slice(0, 40).map((it) => (
              <li key={it.id}>
                <span className="v3-tl-dates">{it.dates}</span> {it.title} <CitationChip c={it.citation} compact />
              </li>
            ))}
          </ol>
          {model.items.length > 40 && <p className="muted">… {model.items.length - 40} autres dans la frise complète.</p>}
        </>
      )}
    </section>
  );
}

function CountryLinks({ links, iso3 }: { links: ApiState<CountryLinksResponse>; iso3: string }) {
  const kinds = useMemo(() => {
    if (links.status !== 'ok' || !links.data.data.by_kind) return null;
    return Object.entries(links.data.data.by_kind)
      .map(([kind, k]) => ({ kind, k, label: linkTypeLabel(kind, k.labels), total: k.groups.reduce((n, g) => n + (g.total ?? g.links.length), 0) }))
      .sort((a, b) => THEMES.findIndex((t) => t.id === themeOf(a.kind)) - THEMES.findIndex((t) => t.id === themeOf(b.kind)) || a.label.localeCompare(b.label));
  }, [links.status, links.data]);
  const flat = useMemo(() => {
    if (links.status !== 'ok' || links.data.data.by_kind) return [];
    const m = new Map<string, CountryLink[]>();
    for (const l of links.data.data.links) m.set(l.link_type, [...(m.get(l.link_type) ?? []), l]);
    return [...m.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [links.status, links.data]);
  return (
    <section id="dom-links" className="v3-domain" aria-labelledby="h-links">
      <h2 id="h-links">Liens avec d’autres pays et entités</h2>
      {links.status === 'loading' && <V3Loading what="des liens" />}
      {links.status === 'error' && <V3Error error={links.error} what={`Les liens de ${iso3}`} onRetry={links.reload} />}
      {links.status === 'ok' && (
        <>
          <p className="field-hint">
            Un lien dit « relié via » une source, jamais « parce que ». Les quantités sont celles rapportées par la source, dans leur unité ; une part de votes identiques
            n’est pas une mesure d’amitié. {links.data.data.mode === 'on_demand' ? 'Liens dérivés à la demande (aucun magasin de liens matérialisé).' : 'Premiers partenaires par type, sens et période (magasin de liens).'}
          </p>
          {links.data.data.truncated && <p className="v3-notice">Voisinage tronqué par le serveur : d’autres liens peuvent exister.</p>}
          {kinds?.map(({ kind, k, label, total }) => (
            <details key={kind} className="v3-link-group" open={total <= 6} data-testid="link-kind" data-kind={kind}>
              <summary>
                {label} <span className="muted">({total})</span>
              </summary>
              {k.groups.map((g, gi) => (
                <div key={`${g.direction}-${g.facet ?? ''}-${gi}`} className="v3-link-subgroup">
                  <p className="muted">
                    {groupTitle(g.direction, !!k.directed)}
                    {g.period ? ` · ${g.period}` : ''}
                    {g.facet && !/=(all|TOTAL)$/.test(g.facet) ? ` · ${g.facet}` : ''}
                    {g.total !== null && g.total !== undefined && g.total > g.links.length ? ` · ${g.links.length} premiers sur ${g.total}` : ''}
                  </p>
                  <ul>
                    {g.links.map((l, i) => (
                      <LinkLine key={`${l.subject}-${l.object}-${i}`} l={l} self={links.data.data.qid} selfIso3={iso3} labels={links.data.data.labels} />
                    ))}
                  </ul>
                </div>
              ))}
            </details>
          ))}
          {flat.map(([type, list]) => (
            <details key={type} className="v3-link-group" open={list.length <= 6}>
              <summary>
                {linkTypeLabel(type)} <span className="muted">({list.length})</span>
              </summary>
              <ul>
                {list.slice(0, 200).map((l, i) => (
                  <LinkLine key={`${l.subject}-${l.object}-${i}`} l={l} self={links.data.data.qid} selfIso3={iso3} labels={links.data.data.labels} />
                ))}
              </ul>
            </details>
          ))}
          <details className="v3-link-status">
            <summary>État des types de liens</summary>
            <ul>
              {Object.entries(links.data.data.link_types).map(([t, s]) => (
                <li key={t}>
                  {linkTypeLabel(t)} : {s.derived} lien(s) — {LINK_STATUS_LABEL[s.status] ?? s.status}
                </li>
              ))}
            </ul>
          </details>
        </>
      )}
    </section>
  );
}

function groupTitle(direction: string, directed: boolean): string {
  if (!directed || direction === 'both') return 'Partenaires';
  return direction === 'outgoing' ? 'Depuis ce pays vers' : 'Vers ce pays depuis';
}

function LinkLine({ l, self, selfIso3, labels }: { l: CountryLink; self: string; selfIso3: string; labels: Record<string, Record<string, string>> }) {
  const selfIsSubject = l.subject === self || l.iso3?.[0] === selfIso3;
  const other = selfIsSubject ? l.object : l.subject;
  const otherIso = (selfIsSubject ? l.iso3?.[1] : l.iso3?.[0]) ?? (/^iso3:([A-Z]{3})$/.exec(other)?.[1] || null);
  const t = l.temporal;
  return (
    <li>
      {otherIso && otherIso !== selfIso3 ? (
        <Link to={`/v3/relation?a=${selfIso3}&b=${otherIso}`} title="Tous les liens entre les deux pays">
          {linkNodeLabel(labels, other)}
        </Link>
      ) : (
        <Link to={`/v3/chemin?from=${encodeURIComponent(self)}&to=${encodeURIComponent(other)}`}>{linkNodeLabel(labels, other)}</Link>
      )}{' '}
      <span className={t.kind === 'undated' ? 'v3-undated' : 'muted'}>{linkDates(t).text}</span>
      {l.via.length > 0 && <span className="muted"> · via {l.via.map((v) => linkNodeLabel(labels, v)).join(', ')}</span>}
      {l.weight && (
        <span title={l.weight}>
          {' '}
          · {l.link_type === 'unga_vote_agreement' ? `part de votes identiques ${frDecimal(l.weight, 4)}` : `${frValue(l.weight, l.unit)} ${l.unit ?? ''}`}
        </span>
      )}{' '}
      {l.evidence.slice(0, 3).map((e, j) => (
        <CitationChip key={j} c={citeLinkEvidence(e)} compact />
      ))}
      {l.evidence.length > 3 && <span className="muted"> +{l.evidence.length - 3} preuve(s)</span>}
    </li>
  );
}

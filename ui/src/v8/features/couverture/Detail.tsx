import { useMemo, useState } from 'react';
import { Link } from 'react-router';
import { v8Links } from '../../contract';
import { Badge, EmptyState, ErrorState, MissingValue, Panel, Skeleton, SourceChip, Stat, frCompact, frNumber } from '../../shell/ui';
import type { CountryDetail, DomainDetail, Summary, Table } from './api';
import { getCountry, getDomain } from './api';
import { useLoad } from './useLoad';
import { LICENCE_LABEL, NOTE_SHORT, frCadence, frDate, histPoints, licenceBreakdown } from './model';
import { PendingCard } from './Pending';

/** Domain detail: tables (source + licence on hover), countries with / without data, KG kinds, freshness, gaps. */
export function DomainPanel({ id, zone, zoneLabel, onClose, onCountry, onDomain }: { id: string; zone: string; zoneLabel: string; onClose: () => void; onCountry: (iso3: string) => void; onDomain: (id: string) => void }) {
  const st = useLoad(`d:${id}`, () => getDomain(id));
  return (
    <Panel
      className="cv-detail"
      id="cv-domain"
      title={st.data ? st.data.label : 'Domaine'}
      subtitle={zone ? `Filtré sur ${zoneLabel}` : 'Tous les lieux'}
      actions={
        <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={onClose} aria-label="Fermer le détail du domaine (Échap)">
          ✕
        </button>
      }
    >
      {st.status === 'loading' && !st.data && <Skeleton height={420} label="Chargement du domaine…" />}
      {st.status === 'error' && <ErrorState error={st.error} what="le détail du domaine" onRetry={st.reload} />}
      {st.data && <DomainBody d={st.data} zone={zone} onCountry={onCountry} onDomain={onDomain} />}
    </Panel>
  );
}

function SummaryStats({ s, places }: { s: Summary; places: number }) {
  return (
    <div className="cv-stats">
      <Stat label="Tables d’atlas" value={s.tables} size="sm" />
      <Stat label="Lignes stockées" value={s.rows} size="sm" format={(v) => frCompact(Number(v))} />
      <Stat label="Pays avec données" value={s.countries} size="sm" sub={`sur ${places}`} />
      {s.latest_year != null ? (
        <Stat label="Dernière année observée" value={String(s.latest_year)} size="sm" format={(v) => String(v)} />
      ) : (
        <Stat label="Dernière année observée" value={null} size="sm" missing={{ reason: 'aucune période passée stockée' }} />
      )}
      <Stat label="Entités KG" value={s.kg_entities} size="sm" format={(v) => frCompact(Number(v))} />
      {s.catalog && <Stat label="Concepts du catalogue sans table chargée" value={s.catalog.without_loaded_table} size="sm" sub={`sur ${frNumber(s.catalog.concepts)}`} />}
    </div>
  );
}

export function LicenceBar({ s }: { s: Summary }) {
  const parts = licenceBreakdown(s);
  const total = parts.reduce((a, p) => a + p.n, 0);
  if (!total) return null;
  return (
    <div className="cv-licbar" aria-label="Licences des tables">
      <div className="cv-licbar-track" aria-hidden="true">
        {parts.map((p) => (
          <span key={p.cls} className={`cv-lic-${p.cls}`} style={{ flexGrow: p.n }} />
        ))}
      </div>
      <ul className="cv-licbar-legend">
        {parts.map((p) => (
          <li key={p.cls}>
            <span className={`cv-dot cv-lic-${p.cls}`} aria-hidden="true" /> {p.n} {p.label}
          </li>
        ))}
      </ul>
    </div>
  );
}

function DomainBody({ d, zone, onCountry, onDomain }: { d: DomainDetail; zone: string; onCountry: (iso3: string) => void; onDomain: (id: string) => void }) {
  const [allTables, setAllTables] = useState(false);
  const inZone = (c: { region: string; subregion?: string }) => !zone || c.region === zone || c.subregion === zone;
  const countries = d.countries.filter(inZone);
  const without = d.countries_without_data.filter(inZone);
  const tables = allTables ? d.tables : d.tables.slice(0, 12);
  const neverLoaded = d.connectors.filter((c) => !c.acquired && c.tables_loaded.length === 0);
  const places = d.countries.length + d.countries_without_data.length;
  return (
    <div className="cv-detail-body">
      <SummaryStats s={d.summary} places={places} />
      <LicenceBar s={d.summary} />
      <p className="cv-note v8-muted">{NOTE_SHORT}</p>

      <h3 className="cv-h3">Tables ({d.tables.length})</h3>
      {d.tables.length === 0 ? (
        <EmptyState compact title="Aucune table d’atlas dans ce domaine">
          Aucune source chargée ne publie de série rangée ici ; voir les connecteurs déclarés et les chargements en attente ci-dessous.
        </EmptyState>
      ) : (
        <div className="cv-tablebox">
          <table className="cv-list">
            <thead>
              <tr>
                <th scope="col">Table</th>
                <th scope="col" className="num">Lignes</th>
                <th scope="col" className="num">Pays</th>
                <th scope="col">Périodes</th>
                <th scope="col">Chargée</th>
              </tr>
            </thead>
            <tbody>
              {tables.map((t) => (
                <TableRow key={t.key} t={t} />
              ))}
            </tbody>
          </table>
          {d.tables.length > 12 && (
            <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={() => setAllTables((v) => !v)}>
              {allTables ? 'Réduire' : `Voir les ${d.tables.length} tables`}
            </button>
          )}
        </div>
      )}

      <Histogram hist={d.hist} before={d.hist_before} from={d.hist_from ?? 1900} today={d.meta.today} />

      <h3 className="cv-h3">
        Pays sans aucune donnée {zone ? 'dans la zone' : ''} ({without.length})
      </h3>
      {without.length === 0 ? (
        <p className="v8-muted">Chaque pays {zone ? 'de la zone ' : ''}a au moins une ligne ou une entité KG dans ce domaine.</p>
      ) : (
        <ul className="cv-chips" aria-label="Pays sans donnée">
          {without.map((c) => (
            <li key={c.iso3}>
              <button type="button" className="cv-chip is-none" onClick={() => onCountry(c.iso3)} title={`${c.label} : aucune source chargée dans ce domaine (≠ absence du phénomène)`}>
                {c.label}
              </button>
            </li>
          ))}
        </ul>
      )}
      <details className="cv-more">
        <summary>Pays avec données ({countries.length})</summary>
        <ul className="cv-chips">
          {countries.map((c) => (
            <li key={c.iso3}>
              <button type="button" className="cv-chip" onClick={() => onCountry(c.iso3)} title={`${c.label} : ${frNumber(c.rows ?? 0)} lignes, ${c.tables ?? 0} tables${c.y1 ? `, jusqu’à ${c.y1}` : ''}${c.kg ? `, ${c.kg} entités KG` : ''}`}>
                {c.label}
                <span className="v8-num v8-subtle"> {c.y1 ?? (c.kg ? 'KG' : '')}</span>
              </button>
            </li>
          ))}
        </ul>
      </details>

      {d.kg_kinds.length > 0 && (
        <>
          <h3 className="cv-h3">Graphe de connaissances</h3>
          <ul className="cv-kinds">
            {d.kg_kinds.map((k) => (
              <li key={k.kind}>
                <code>{k.kind}</code> <span className="v8-num">{frNumber(k.entities)}</span>
                <span className="v8-subtle"> · {Object.keys(k.sources).slice(0, 3).join(', ')}</span>
              </li>
            ))}
          </ul>
          {d.kg_located_in_note && <p className="cv-note v8-subtle">Rattachement aux pays : {frNote(d.kg_located_in_note)}</p>}
        </>
      )}

      {d.catalog && d.catalog.without_loaded_table > 0 && (
        <details className="cv-more">
          <summary>
            {d.catalog.without_loaded_table} concepts du catalogue sans table chargée (sur {d.catalog.concepts})
          </summary>
          <ul className="cv-plain">
            {d.catalog.examples.map((c) => (
              <li key={c.id}>
                {c.label} <span className="v8-subtle v8-mono">{c.tables.join(', ')}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {neverLoaded.length > 0 && (
        <>
          <h3 className="cv-h3">Connecteurs déclarés, jamais chargés ({neverLoaded.length})</h3>
          <ul className="cv-plain">
            {neverLoaded.map((c) => (
              <li key={c.name}>
                <code>{c.name}</code> — {c.provider} <span className="v8-subtle">· {c.licence.slice(0, 80)}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      {d.pending.length > 0 && (
        <>
          <h3 className="cv-h3">Ce qui débloquerait davantage ({d.pending.length})</h3>
          <div className="cv-cards">
            {d.pending.map((it) => (
              <PendingCard key={it.id} it={it} compact onDomain={onDomain} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function frNote(s: string): string {
  return s.includes('located_in')
    ? 'entités reliées à un pays par une propriété de la famille « est situé dans » (élément Wikidata du pays ou code ISO 3166-2) ; celles placées seulement dans une ville, une région sans code ISO ou par coordonnées ne sont pas comptées.'
    : s;
}

function TableRow({ t }: { t: Table }) {
  return (
    <tr>
      <td>
        <span className="cv-tkey v8-mono">{t.key.replace(/^atlas\./, '')}</span>{' '}
        <SourceChip source={t.source} label={LICENCE_LABEL[t.licence_class] ?? t.licence_class} licence={t.licence ?? null} details={tableDetails(t)} />
        {t.read_error && <Badge kind="missing" title={t.read_error}>illisible</Badge>}
      </td>
      <td className="num v8-num">{frCompact(t.rows)}</td>
      <td className="num v8-num">{t.countries || <span className="v8-subtle" title="Lignes sans code pays : agrégats, régions infranationales, grilles">—</span>}</td>
      <td className="v8-num">
        {t.period_min?.slice(0, 4) ?? '?'}–{t.latest_past_year ?? <MissingValue reason="seulement des périodes futures" compact />}
        {t.future_rows > 0 && (
          <Badge kind="projection" title={`${frNumber(t.future_rows)} lignes datées après aujourd’hui (projections, scénarios ou annonces)`}>
            +futur
          </Badge>
        )}
      </td>
      <td className="v8-num">
        {frDate(t.recorded_at)}
        {t.overdue && (
          <Badge kind="missing" title={`Dernière récupération ${frDate(t.last_fetch_at)} ; cadence déclarée ${frCadence(t.cadence_seconds)}`}>
            à rafraîchir
          </Badge>
        )}
      </td>
    </tr>
  );
}

function tableDetails(t: Table): string[] {
  const out = [`table ${t.key}`, `${frNumber(t.rows)} lignes, ${t.indicators ?? '?'} indicateurs, ${t.countries} pays`];
  if (t.rows_without_country) out.push(`${frNumber(t.rows_without_country)} lignes hors pays (agrégats, régions, grilles)`);
  out.push(`${t.versions} version${t.versions > 1 ? 's' : ''} ; chargée le ${frDate(t.recorded_at)} ; cadence ${frCadence(t.cadence_seconds)}`);
  if (t.domain_basis === 'catalog_theme') out.push('domaine déduit des thèmes du catalogue');
  if (t.coverage_note) out.push(t.coverage_note.slice(0, 240));
  return out;
}

/** Rows per year (period start), from `from`; years without rows are gaps, not zeros. */
export function Histogram({ hist, before, from, today }: { hist: Record<string, number>; before: number; from: number; today: string }) {
  const thisYear = Number(today.slice(0, 4));
  const start = Math.max(from, 1950);
  const pts = useMemo(() => histPoints(hist, start), [hist, start]);
  if (pts.length === 0) return null;
  const end = Math.max(thisYear + 1, pts[pts.length - 1]![0]);
  const max = Math.max(...pts.map((p) => p[1]));
  const W = 600;
  const H = 64;
  const x = (y: number) => ((y - start) / Math.max(1, end - start)) * W;
  const bw = Math.max(1, W / Math.max(1, end - start + 1) - 1);
  const earlier = before + histPoints(hist, -100000, start - 1).reduce((a, p) => a + p[1], 0);
  return (
    <figure className="cv-hist">
      <figcaption>
        Lignes par année de période ({start}–{end})
        {earlier > 0 && <span className="v8-subtle"> · {frCompact(earlier)} lignes avant {start}</span>}
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H + 14}`} preserveAspectRatio="none" role="img" aria-label={`Histogramme des lignes par année, de ${start} à ${end}`}>
        {pts.map(([y, n]) => (
          <rect key={y} x={x(y)} width={bw} y={H - (Math.log10(1 + n) / Math.log10(1 + max)) * H} height={(Math.log10(1 + n) / Math.log10(1 + max)) * H} className={y > thisYear ? 'cv-bar-future' : 'cv-bar'}>
            <title>{`${y} : ${frNumber(n)} lignes${y > thisYear ? ' (après aujourd’hui : projections)' : ''}`}</title>
          </rect>
        ))}
        <line x1={x(thisYear + 1)} x2={x(thisYear + 1)} y1={0} y2={H} className="cv-today" />
        {[start, 1980, 2000, 2020].filter((y) => y >= start && y <= end).map((y) => (
          <text key={y} x={x(y)} y={H + 12} className="cv-axis">
            {y}
          </text>
        ))}
      </svg>
    </figure>
  );
}

/** Country profile: per domain what is stored (hatched when nothing), the tables and KG kinds. */
export function CountryPanel({ iso3, onClose, onDomain }: { iso3: string; onClose: () => void; onDomain: (id: string) => void }) {
  const st = useLoad(`c:${iso3}`, () => getCountry(iso3));
  return (
    <Panel
      className="cv-detail"
      id="cv-country"
      title={st.data ? st.data.label : iso3}
      subtitle={st.data ? `${st.data.region_label} › ${st.data.subregion_label}` : 'Profil de couverture'}
      actions={
        <>
          <Link className="v8-btn v8-btn-sm" to={v8Links.entity('country', iso3)}>
            Fiche
          </Link>
          <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={onClose} aria-label="Fermer le profil du pays (Échap)">
            ✕
          </button>
        </>
      }
    >
      {st.status === 'loading' && !st.data && <Skeleton height={420} label="Chargement du profil…" />}
      {st.status === 'error' && <ErrorState error={st.error} what="le profil du pays" onRetry={st.reload} />}
      {st.data && <CountryBody c={st.data} onDomain={onDomain} />}
    </Panel>
  );
}

function CountryBody({ c, onDomain }: { c: CountryDetail; onDomain: (id: string) => void }) {
  const [all, setAll] = useState(false);
  const max = Math.max(1, ...c.domains.map((d) => d.cell?.rows ?? 0));
  const tables = all ? c.tables : c.tables.slice(0, 15);
  const notices = [...new Map(c.tables.filter((t) => t.required_notice).map((t) => [t.key.split('.')[1], t.required_notice!])).entries()];
  return (
    <div className="cv-detail-body">
      {c.placement === 'reva' && (
        <p className="cv-note">
          <Badge kind="estimate">placement REVA</Badge> {c.placement_basis}
        </p>
      )}
      <div className="cv-stats">
        <Stat label="Domaines avec données" value={c.domains.length - c.domains_without_data.length} size="sm" sub={`sur ${c.domains.length}`} />
        <Stat label="Tables" value={c.tables.length} size="sm" />
        <Stat label="Lignes" value={c.tables.reduce((a, t) => a + t.rows, 0)} size="sm" format={(v) => frCompact(Number(v))} />
        <Stat label="Entités KG rattachées" value={c.kg_kinds.reduce((a, k) => a + k.entities, 0)} size="sm" format={(v) => frCompact(Number(v))} />
      </div>
      <p className="cv-note v8-muted">{NOTE_SHORT}</p>
      <ul className="cv-bars" aria-label="Couverture par domaine">
        {c.domains.map((d) => {
          const cell = d.cell;
          const rows = cell?.rows ?? 0;
          const w = rows > 0 ? Math.max(3, (Math.log10(1 + rows) / Math.log10(1 + max)) * 100) : 0;
          return (
            <li key={d.id}>
              <button type="button" className="cv-barrow" onClick={() => onDomain(d.id)} title={`${d.label} — détail du domaine`}>
                <span className="cv-barlabel">{d.label}</span>
                <span className={`cv-bartrack${rows > 0 ? '' : ' is-none'}`}>
                  {rows > 0 && <span className="cv-barfill" style={{ width: `${w}%` }} />}
                </span>
                <span className="cv-barmeta v8-num">
                  {rows > 0 ? (
                    `${cell?.tables ?? 0} t · ${frCompact(rows)} · ${cell?.y0 ?? '?'}–${cell?.y1 ?? 'futur'}`
                  ) : cell?.kg ? (
                    `KG ${frNumber(cell.kg)}`
                  ) : (
                    <MissingValue reason="aucune source chargée" compact />
                  )}
                  {rows > 0 && cell?.kg ? ` · KG ${frCompact(cell.kg)}` : ''}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      <h3 className="cv-h3">Tables ({c.tables.length})</h3>
      <div className="cv-tablebox">
        <table className="cv-list">
          <thead>
            <tr>
              <th scope="col">Table</th>
              <th scope="col" className="num">Lignes</th>
              <th scope="col">Périodes</th>
              <th scope="col">Licence</th>
            </tr>
          </thead>
          <tbody>
            {tables.map((t) => (
              <tr key={t.key}>
                <td className="v8-mono cv-tkey">{t.key.replace(/^atlas\./, '')}</td>
                <td className="num v8-num">{frCompact(t.rows)}</td>
                <td className="v8-num">
                  {t.y0 ?? '?'}–{t.y1 ?? 'futur'}
                  {t.future_rows > 0 && <span className="v8-subtle"> +{frCompact(t.future_rows)} futur</span>}
                </td>
                <td>
                  <SourceChip source={t.key.split('.')[1] ?? t.key} label={LICENCE_LABEL[t.licence_class] ?? t.licence_class} licence={t.licence ? { licence: t.licence, non_commercial: t.licence_class === 'non_commercial', restricted: t.licence_class !== 'open' && t.licence_class !== 'unknown', required_notice: t.required_notice } : null} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {c.tables.length > 15 && (
          <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={() => setAll((v) => !v)}>
            {all ? 'Réduire' : `Voir les ${c.tables.length} tables`}
          </button>
        )}
      </div>
      {notices.length > 0 && (
        <div className="cv-notices">
          <h4>Avis exigés par les fournisseurs</h4>
          <ul>
            {notices.map(([src, n]) => (
              <li key={src}>
                <code>{src}</code> : <q lang="en">{n}</q>
              </li>
            ))}
          </ul>
        </div>
      )}
      {c.kg_kinds.length > 0 && (
        <>
          <h3 className="cv-h3">Entités KG rattachées au pays</h3>
          <ul className="cv-kinds">
            {c.kg_kinds.slice(0, 24).map((k) => (
              <li key={k.kind}>
                <code>{k.kind}</code> <span className="v8-num">{frNumber(k.entities)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

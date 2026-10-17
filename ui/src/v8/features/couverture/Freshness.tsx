import { useMemo } from 'react';
import { Badge, ErrorState, Panel, Skeleton, frCompact, frNumber } from '../../shell/ui';
import type { Freshness } from './api';
import { getFreshness } from './api';
import { useLoad } from './useLoad';
import { domainLabel, frCadence, frDate, histPoints } from './model';

const AXIS_FROM = 1960;

/**
 * Freshness timeline: per domain, each table is a dot at its latest *observed* year (future-dated rows —
 * projections — are not latest observations), over a light area of rows per year. Overdue tables (last fetch older
 * than the connector's declared cadence) are listed below.
 */
export function FreshnessTab({ onDomain }: { onDomain: (id: string) => void }) {
  const st = useLoad('f', () => getFreshness());
  if (st.status === 'error' && !st.data) return <ErrorState error={st.error} what="la fraîcheur des données" onRetry={st.reload} />;
  if (!st.data) return <Skeleton height={520} label="Chargement de la fraîcheur…" />;
  return <FreshnessBody f={st.data} onDomain={onDomain} />;
}

function FreshnessBody({ f, onDomain }: { f: Freshness; onDomain: (id: string) => void }) {
  const thisYear = Number(f.meta.today.slice(0, 4));
  const end = thisYear + 1;
  const overdue = useMemo(() => f.domains.flatMap((d) => d.tables.filter((t) => t.overdue).map((t) => ({ ...t, domain: d.label }))), [f]);
  const recent = f.domains.reduce((a, d) => a + d.tables.filter((t) => (t.latest_past_year ?? 0) >= thisYear - 1).length, 0);
  const total = f.domains.reduce((a, d) => a + d.tables.length, 0);
  const W = 640;
  const x = (y: number) => ((Math.max(AXIS_FROM, Math.min(end, y)) - AXIS_FROM) / (end - AXIS_FROM)) * W;
  return (
    <div className="cv-fresh">
      <p className="cv-answer">
        <strong className="v8-num">{recent}</strong> tables sur <strong className="v8-num">{total}</strong> ont une observation datée de {thisYear - 1} ou {thisYear} ;{' '}
        <strong className="v8-num">{overdue.length}</strong> n’ont pas été récupérées depuis plus longtemps que la cadence déclarée de leur connecteur. Les lignes datées après aujourd’hui
        (projections, scénarios, annonces) ne comptent jamais comme « dernière observation ».
      </p>
      <Panel title="Dernière année observée, par table" subtitle={`Point = une table (taille ∝ log des lignes) · aire = lignes par année · trait = aujourd’hui · axe ${AXIS_FROM}–${end} (plus ancien : à gauche)`} flush>
        <ul className="cv-fresh-rows">
          {f.domains.map((d) => {
            const pts = histPoints(d.hist, AXIS_FROM, end);
            const max = Math.max(1, ...pts.map((p) => p[1]));
            const area = pts.length ? `M${x(pts[0]![0])},40 ` + pts.map(([y, n]) => `L${x(y)},${40 - (Math.log10(1 + n) / Math.log10(1 + max)) * 34}`).join(' ') + ` L${x(pts[pts.length - 1]![0])},40 Z` : '';
            return (
              <li key={d.id} className="cv-fresh-row">
                <button type="button" className="cv-fresh-label" onClick={() => onDomain(d.id)} title={`${d.label} — détail du domaine`}>
                  {d.label}
                  <span className="v8-subtle v8-num"> {d.tables.length}</span>
                </button>
                <svg className="cv-fresh-svg" viewBox={`0 0 ${W} 44`} preserveAspectRatio="none" role="img" aria-label={`${d.label} : dernières années observées des tables`}>
                  {area && <path d={area} className="cv-fresh-area" />}
                  <line x1={x(thisYear + 0.5)} x2={x(thisYear + 0.5)} y1={2} y2={42} className="cv-today" />
                  {d.tables.map((t) =>
                    t.latest_past_year == null ? null : (
                      <circle key={t.key} cx={x(t.latest_past_year)} cy={22} r={1.5 + Math.log10(1 + t.rows) * 0.9} className={t.overdue ? 'cv-dot-overdue' : 'cv-dot-table'}>
                        <title>{`${t.key} : dernière année observée ${t.latest_past_year} · ${frNumber(t.rows)} lignes · chargée le ${frDate(t.recorded_at)}${t.overdue ? ' · à rafraîchir' : ''}`}</title>
                      </circle>
                    ),
                  )}
                </svg>
              </li>
            );
          })}
        </ul>
        <div className="cv-fresh-axis v8-num" aria-hidden="true">
          {[AXIS_FROM, 1980, 2000, 2020].map((y) => (
            <span key={y} style={{ left: `calc(var(--cv-label-w) + (100% - var(--cv-label-w)) * ${(y - AXIS_FROM) / (end - AXIS_FROM)})` }}>
              {y}
            </span>
          ))}
        </div>
      </Panel>
      <Panel title={`À rafraîchir (${overdue.length})`} subtitle="Dernière récupération plus ancienne que la cadence déclarée par le connecteur" dense>
        {overdue.length === 0 ? (
          <p className="v8-muted">Aucune table en retard sur sa cadence déclarée.</p>
        ) : (
          <div className="cv-tablebox">
            <table className="cv-list">
              <thead>
                <tr>
                  <th scope="col">Table</th>
                  <th scope="col">Domaine</th>
                  <th scope="col">Récupérée</th>
                  <th scope="col">Cadence</th>
                  <th scope="col" className="num">Lignes</th>
                </tr>
              </thead>
              <tbody>
                {overdue.map((t) => (
                  <tr key={t.key}>
                    <td className="v8-mono cv-tkey">{t.key.replace(/^atlas\./, '')}</td>
                    <td>{t.domain}</td>
                    <td className="v8-num">{frDate(t.last_fetch_at)}</td>
                    <td>{frCadence(t.cadence_seconds)}</td>
                    <td className="num v8-num">{frCompact(t.rows)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      <Panel title={`Connecteurs déclarés jamais chargés (${f.never_loaded.length})`} subtitle="Code et tests présents, aucune acquisition live ni table : rien n’est stocké de ces sources" dense>
        <div className="cv-tablebox">
          <table className="cv-list">
            <thead>
              <tr>
                <th scope="col">Connecteur</th>
                <th scope="col">Fournisseur</th>
                <th scope="col">Domaine</th>
                <th scope="col">Licence déclarée</th>
              </tr>
            </thead>
            <tbody>
              {f.never_loaded.map((c) => (
                <tr key={c.name}>
                  <td className="v8-mono">{c.name}</td>
                  <td>{c.provider}</td>
                  <td>{c.domain === 'other' ? <Badge kind="neutral">non classé</Badge> : domainLabel(c.domain)}</td>
                  <td className="v8-subtle cv-clip" title={c.licence}>
                    {c.licence}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

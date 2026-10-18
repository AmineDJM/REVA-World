/**
 * Evidence view chosen by the server from the evidence shape (bundle.viz.type): one series over time → line;
 * cross-section in one unit → ranked bars; sealed forecasts → point + 5–95 % band per model; relations of one
 * entity → mini graph; causal claims → hops; attributed estimates → one row each (never averaged); scenario →
 * the engine's variables and hops. Every mark keeps its evidence id (E1…) for the source popover.
 */
import { Link } from 'react-router';
import { v8Links } from '../../contract';
import { Badge, frDecimal } from '../../shell/ui';
import { TimeChart, type TimePoint } from './TimeChart';

type Rec = Record<string, unknown>;
const s = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : typeof v === 'number' ? String(v) : null);
const arr = (v: unknown): Rec[] => (Array.isArray(v) ? (v.filter((x) => x && typeof x === 'object') as Rec[]) : []);
const num = (v: unknown): number | null => {
  const x = s(v);
  if (x === null) return null;
  const n = Number(x);
  return Number.isFinite(n) ? n : null;
};

export function EvidenceView({ viz, onCite }: { viz: Rec | null; onCite?: (eid: string) => void }) {
  if (!viz) return null;
  switch (viz.type) {
    case 'timeseries':
      return <SeriesView viz={viz} />;
    case 'bars':
      return <BarsView viz={viz} onCite={onCite} />;
    case 'table':
      return <BarsView viz={viz} onCite={onCite} table />;
    case 'forecast':
      return <ForecastView viz={viz} />;
    case 'graph':
      return <GraphView viz={viz} />;
    case 'path':
      return <PathView viz={viz} />;
    case 'estimates':
      return <EstimatesView viz={viz} />;
    case 'scenario':
      return <ScenarioView viz={viz} />;
    default:
      return null;
  }
}

function SeriesView({ viz }: { viz: Rec }) {
  const sr = arr(viz.series)[0];
  if (!sr) return null;
  const pts = arr(sr.points).map((p) => ({ t: s(p.t) ?? '', t_end: s(p.t_end), v: s(p.v), projection: !!p.projection, cited: !!p.cited, source: s(p.source) })) as TimePoint[];
  return (
    <figure className="v8d-fig">
      <figcaption className="v8d-figcap">
        <strong>{s(sr.label) ?? s(sr.concept)}</strong>
        {s(sr.unit) && <span className="v8-subtle"> · {s(sr.unit)}</span>}
        <span className="v8-subtle"> · {pts.length} points · {s(sr.source)}</span>
        {sr.outside_snapshot ? <Badge kind="info" title="Table enregistrée après l’instantané atlas : lue dans sa dernière version enregistrée.">hors instantané</Badge> : null}
      </figcaption>
      <TimeChart points={pts} unit={s(sr.unit)} label={s(sr.label) ?? 'série'} />
      <p className="v8d-fignote v8-subtle">Points cités par la réponse cerclés ; projections en pointillés ; aucun point interpolé.</p>
    </figure>
  );
}

function BarsView({ viz, onCite, table }: { viz: Rec; onCite?: (eid: string) => void; table?: boolean }) {
  const rows = arr(viz.rows);
  const vals = rows.map((r) => num(r.value)).filter((v): v is number => v !== null);
  const max = Math.max(...vals.map((v) => Math.abs(v)), 0);
  const signed = vals.some((v) => v < 0);
  return (
    <figure className="v8d-fig">
      <figcaption className="v8d-figcap">
        {table ? <span>{s(viz.note) ?? 'Valeurs citées'}</span> : <span>Classement des valeurs citées{s(viz.unit) ? ` (${s(viz.unit)})` : ''}</span>}
      </figcaption>
      <ol className="v8d-bars">
        {rows.map((r, i) => {
          const v = num(r.value);
          const w = !table && !signed && v !== null && max > 0 ? Math.max(1.5, (Math.abs(v) / max) * 100) : 0;
          const eid = s(r.eid);
          return (
            <li key={`${eid ?? i}`} className="v8d-bar">
              <span className="v8d-bar-label" title={s(r.label) ?? ''}>
                {s(r.label)}
                {s(r.iso3) && <span className="v8-subtle"> · {s(r.iso3)}</span>}
              </span>
              <span className="v8d-bar-track" aria-hidden="true">
                {w > 0 && <span className={`v8d-bar-fill${r.projection ? ' is-proj' : ''}`} style={{ width: `${w}%` }} />}
              </span>
              <span className="v8d-bar-value v8-num">
                {s(r.value) ? frDecimal(s(r.value) as string, 2) : 'manquant'}
                {table && s(r.unit) ? ` ${s(r.unit)}` : ''}
                <span className="v8-subtle"> {s(r.period)}</span>
                {eid && (
                  <button type="button" className="v8d-eid" onClick={() => onCite?.(eid)} title="Voir la preuve">
                    {eid}
                  </button>
                )}
              </span>
            </li>
          );
        })}
      </ol>
      {arr(viz.others).length > 0 && (
        <ul className="v8d-others">
          {arr(viz.others).map((r, i) => (
            <li key={`${s(r.eid) ?? i}`} className="v8-subtle">
              Autre unité, hors du classement : {s(r.label)} — <span className="v8-num">{s(r.value) ? frDecimal(s(r.value) as string, 2) : 'manquant'}</span> {s(r.unit)}{' '}
              {s(r.period)} {s(r.eid) && <span className="v8d-eid-static">{s(r.eid)}</span>}
            </li>
          ))}
        </ul>
      )}
    </figure>
  );
}

const STATUS_FR: Record<string, string> = {
  pending: 'en attente de résolution',
  resolved: 'résolue',
  void: 'annulée',
  issued: 'émise',
  abstained: 'abstention',
};

function ForecastView({ viz }: { viz: Rec }) {
  return (
    <div className="v8d-fc">
      {arr(viz.questions).map((q) => {
        const models = arr(q.models);
        const nums = models.flatMap((m) => [num(m.lo), num(m.hi), num(m.point)]).filter((v): v is number => v !== null);
        const out = q.outcome && typeof q.outcome === 'object' ? num((q.outcome as Rec).value) : null;
        if (out !== null) nums.push(out);
        const lo = Math.min(...nums);
        const hi = Math.max(...nums);
        const pos = (v: number) => (hi > lo ? ((v - lo) / (hi - lo)) * 100 : 50);
        return (
          <figure key={s(q.eid) ?? s(q.question_id) ?? ''} className="v8d-fig">
            <figcaption className="v8d-figcap">
              <strong>{s(q.family)}</strong> · {s(q.geo)} · {s(q.period)}
              {s(q.unit) && <span className="v8-subtle"> · {s(q.unit)}</span>}
              <Badge kind="judged" title="Prévision scellée avant l’issue ; jugée à la résolution contre la référence officielle.">
                {STATUS_FR[s(q.status) ?? ''] ?? s(q.status) ?? 'prévision scellée'}
              </Badge>
            </figcaption>
            <ul className="v8d-fc-rows">
              {models.map((m) => {
                const p = num(m.point);
                const l = num(m.lo);
                const h = num(m.hi);
                return (
                  <li key={s(m.model) ?? ''} className="v8d-fc-row">
                    <span className="v8d-fc-model" title={s(m.method) ?? ''}>
                      {(s(m.model) ?? '').replace(/^forecast\./, '')}
                    </span>
                    <span className="v8d-fc-track" aria-hidden="true">
                      {l !== null && h !== null && <span className="v8d-fc-band" style={{ left: `${pos(l)}%`, width: `${Math.max(0.5, pos(h) - pos(l))}%` }} />}
                      {p !== null && <span className="v8d-fc-dot" style={{ left: `${pos(p)}%` }} />}
                      {out !== null && <span className="v8d-fc-out" style={{ left: `${pos(out)}%` }} title="valeur publiée" />}
                    </span>
                    <span className="v8d-fc-val v8-num">
                      {p !== null ? (
                        <>
                          {frDecimal(s(m.point) as string, 2)}
                          {l !== null && h !== null && (
                            <span className="v8-subtle">
                              {' '}
                              [{frDecimal(s(m.lo) as string, 2)} ; {frDecimal(s(m.hi) as string, 2)}]
                            </span>
                          )}
                        </>
                      ) : (
                        <span className="v8-subtle">{STATUS_FR[s(m.status) ?? ''] ?? s(m.status)}{s(m.reason) ? ` : ${s(m.reason)}` : ''}</span>
                      )}
                    </span>
                  </li>
                );
              })}
            </ul>
            <p className="v8d-fignote v8-subtle">Point et intervalle 5–95 % de chaque modèle, tels que scellés ; méthode au survol. Pas un conseil.</p>
          </figure>
        );
      })}
    </div>
  );
}

function GraphView({ viz }: { viz: Rec }) {
  const center = (viz.center ?? {}) as Rec;
  const edges = arr(viz.edges);
  const nodes = new Map(arr(viz.nodes).map((n) => [s(n.id) ?? '', s(n.label) ?? s(n.id) ?? '']));
  const others = edges.map((e) => (s(e.from) === s(center.id) ? s(e.to) : s(e.from)) ?? '');
  const W = 560;
  const H = 260;
  const cx = W / 2;
  const cy = H / 2;
  const R = Math.min(W, H) / 2 - 30;
  return (
    <figure className="v8d-fig">
      <figcaption className="v8d-figcap">
        Relations de <strong>{s(center.label)}</strong> ({edges.length}
        {num(viz.total) !== null && num(viz.total)! > edges.length ? ` sur ${s(viz.total)}` : ''}) — {s(viz.note)}
      </figcaption>
      <svg className="v8d-graph" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Graphe des relations de ${s(center.label)}`}>
        {others.map((o, i) => {
          const a = (i / Math.max(1, others.length)) * Math.PI * 2 - Math.PI / 2;
          const x = cx + Math.cos(a) * R;
          const y = cy + Math.sin(a) * R * 0.82;
          return (
            <g key={`${o}-${i}`}>
              <line x1={cx} y1={cy} x2={x} y2={y} className="v8d-g-edge" />
              <circle cx={x} cy={y} r={4} className="v8d-g-node" />
              <text x={x} y={y + (y < cy ? -8 : 14)} textAnchor="middle" className="v8d-g-label">
                {(nodes.get(o) ?? o).slice(0, 26)}
              </text>
            </g>
          );
        })}
        <circle cx={cx} cy={cy} r={7} className="v8d-g-center" />
        <text x={cx} y={cy - 12} textAnchor="middle" className="v8d-g-clabel">
          {(s(center.label) ?? '').slice(0, 30)}
        </text>
      </svg>
      <ul className="v8d-edges">
        {edges.map((e, i) => (
          <li key={`${s(e.eid)}-${i}`}>
            <span className="v8-subtle">{s(e.label)}</span> {nodes.get((s(e.from) === s(center.id) ? s(e.to) : s(e.from)) ?? '')}{' '}
            <span className="v8d-eid-static">{s(e.eid)}</span>
          </li>
        ))}
      </ul>
    </figure>
  );
}

function PathView({ viz }: { viz: Rec }) {
  return (
    <figure className="v8d-fig">
      <figcaption className="v8d-figcap">{s(viz.note)}</figcaption>
      <ol className="v8d-hops">
        {arr(viz.hops).map((h, i) => {
          const from = (h.from ?? {}) as Rec;
          const to = (h.to ?? {}) as Rec;
          return (
            <li key={`${s(h.eid)}-${i}`} className="v8d-hop">
              <span className="v8d-hop-node">{s(from.label)}</span>
              <span className="v8d-hop-arrow" aria-hidden="true">
                →
              </span>
              <span className="v8d-hop-node">{s(to.label)}</span>
              <span className="v8d-hop-meta v8-subtle">
                {s(h.stance) === 'denies' ? 'conteste · ' : ''}selon {s(h.source)} · {s(h.tier)}
                {s(h.grade) ? ` · preuve ${s(h.grade)}` : ''} <span className="v8d-eid-static">{s(h.eid)}</span>
              </span>
            </li>
          );
        })}
      </ol>
    </figure>
  );
}

function EstimatesView({ viz }: { viz: Rec }) {
  return (
    <figure className="v8d-fig">
      <figcaption className="v8d-figcap">{s(viz.note)}</figcaption>
      <div className="v8d-scroll">
        <table className="v8d-table">
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">Cause → effet</th>
              <th scope="col">Estimation</th>
              <th scope="col">Méthode</th>
            </tr>
          </thead>
          <tbody>
            {arr(viz.rows).map((r) => (
              <tr key={s(r.eid) ?? ''} className={r.related ? 'is-related' : undefined}>
                <td>
                  {s(r.reference)}
                  {r.related ? <Badge kind="info" title="Estimation voisine : autre cause ou autre effet ; elle ne répond pas à la question.">voisine</Badge> : null}
                </td>
                <td>
                  {s(r.cause)} → {s(r.effect)}
                </td>
                <td className="v8-num">
                  {s(r.point) ? frDecimal(s(r.point) as string, 3) : 'non publiée'} <span className="v8-subtle">{s(r.unit)}</span>
                </td>
                <td className="v8-subtle">{s(r.method)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}

function ScenarioView({ viz }: { viz: Rec }) {
  const rows = Array.isArray(viz.rows) ? (viz.rows as unknown[]).filter((x): x is string => typeof x === 'string') : [];
  return (
    <figure className="v8d-fig v8d-scenario">
      <figcaption className="v8d-figcap">
        <Badge kind="scenario" /> {s(viz.label)} · décision de référence <code>{s(viz.decision)}</code>
      </figcaption>
      <ul className="v8d-sc-rows">
        {rows.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      {s(viz.decision) && (
        <p className="v8d-fignote">
          <Link to={v8Links.decision((s(viz.decision) as string).replace(/\.ask$/, ''))}>Ouvrir dans le studio de décision →</Link>
        </p>
      )}
    </figure>
  );
}

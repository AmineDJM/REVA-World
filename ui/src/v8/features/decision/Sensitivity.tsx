import { EmptyState, MissingValue } from '../../shell/ui';
import type { Sensitivity as Sens } from './api';
import { num, scale, tornado, unitFr } from './model';

/** Tornado: which estimate's published uncertainty moves the result most (one at a time, 5 % ↔ 95 %). */
export function Sensitivity({ data, variable, period, onVariable, onPeriod }: { data: Sens; variable: string | null; period: string | null; onVariable: (id: string) => void; onPeriod: (p: string) => void }) {
  const vars = data.variables;
  if (!vars.length) return <EmptyState title="Rien à analyser">Aucune variable chiffrée : la sensibilité n’a pas d’objet (voir « Pourquoi »).</EmptyState>;
  const v = vars.find((x) => x.id === variable) ?? vars.find((x) => x.bars.length > 0) ?? vars[0]!;
  const p = period && v.periods.includes(period) ? period : (v.headline_period ?? v.periods[0] ?? 'undated');
  const t = tornado(v, p);
  const W = 720;
  const rowH = 28;
  const left = 230;
  const right = 170;
  const all = [...t.bars.flatMap((b) => [b.lo, b.hi]), ...(t.center != null ? [t.center] : [])];
  const sc = scale(all, W - left - right);
  const H = Math.max(1, t.bars.length) * rowH + 26;
  return (
    <div className="v8d-sens">
      <div className="v8-row v8d-sens-ctl">
        <label>
          Variable{' '}
          <select className="v8-input" value={v.id} onChange={(e) => onVariable(e.target.value)}>
            {vars.map((x) => (
              <option key={x.id} value={x.id}>
                {x.label_fr} · {x.geo} {x.bars.length ? `(${x.bars.length})` : ''}
              </option>
            ))}
          </select>
        </label>
        {v.periods.length > 1 && (
          <label>
            Période{' '}
            <select className="v8-input" value={p} onChange={(e) => onPeriod(e.target.value)}>
              {v.periods.map((x) => (
                <option key={x} value={x}>
                  {x}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <p className="v8d-answer">
        Valeur centrale ({p === 'undated' ? 'non daté' : p}) :{' '}
        {t.center != null ? (
          <strong className="v8-num">
            {num(t.center, true)} {unitFr(v.unit)}
          </strong>
        ) : (
          <MissingValue reason="période non calculée (délai non publié)" />
        )}
        <span className="v8-muted v8d-small"> — {data.center_label_fr}</span>
      </p>
      {t.bars.length > 0 ? (
        <svg className="v8d-tornado" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Tornade de sensibilité : ${t.bars.map((b) => `${b.label} de ${num(b.lo)} à ${num(b.hi)}`).join(' ; ')}`}>
          {t.center != null && <line x1={left + sc.x(t.center)} x2={left + sc.x(t.center)} y1={4} y2={H - 18} className="v8d-tor-center" />}
          <line x1={left + sc.x(0)} x2={left + sc.x(0)} y1={4} y2={H - 18} className="v8d-zero" />
          {t.bars.map((b, i) => (
            <g key={b.key} transform={`translate(0 ${6 + i * rowH})`}>
              <text x={left - 8} y={13} textAnchor="end" className="v8d-tor-label">
                {b.label.length > 30 ? `${b.label.slice(0, 29)}…` : b.label}
              </text>
              <rect x={left + sc.x(b.lo)} width={Math.max(2, sc.x(b.hi) - sc.x(b.lo))} y={2} height={16} className="v8d-tor-bar" />
              <text x={W - right + 12} y={14} className="v8d-tor-val">
                {num(b.lo, true)} → {num(b.hi, true)}
                {b.r2 != null ? ` · r² ${num(b.r2)}` : ''}
              </text>
            </g>
          ))}
          <text x={left + sc.x(sc.lo)} y={H - 4} className="v8d-tor-val">
            {num(sc.lo, true)}
          </text>
          <text x={left + sc.x(sc.hi)} y={H - 4} textAnchor="end" className="v8d-tor-val">
            {num(sc.hi, true)} {unitFr(v.unit)}
          </text>
        </svg>
      ) : (
        <EmptyState title="Aucune barre" compact>
          Aucune estimation de ce chemin ne publie d’incertitude utilisable : la sensibilité ne peut pas être calculée (rien n’est inventé).
        </EmptyState>
      )}
      {(t.flat.length > 0 || t.missing.length > 0) && (
        <ul className="v8d-flat">
          {t.flat.map((f) => (
            <li key={f.key}>
              <span className="v8d-hatch" aria-hidden="true" /> {f.label} : incertitude non publiée — pas de barre (scénario ponctuel)
            </li>
          ))}
          {t.missing.map((f) => (
            <li key={f.key}>
              <span className="v8d-hatch" aria-hidden="true" /> {f.label} : période non calculée
            </li>
          ))}
        </ul>
      )}
      <p className="v8-muted v8d-small">
        {data.method_fr} {data.runs} propagations, {Math.round(data.compute_ms)} ms{data.cached ? ' (en cache)' : ''}.
      </p>
    </div>
  );
}

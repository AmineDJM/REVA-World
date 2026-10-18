import { Badge, MissingValue } from '../../shell/ui';
import type { Comparison, CompareVar } from './api';
import { num, unitFr } from './model';

type Q = { p05?: number | null; p50?: number | null; p95?: number | null } | null;

/** A and B intervals on one axis (5–95 %, median), against zero. */
function Pair({ a, b, lo, hi }: { a: Q; b: Q; lo: number; hi: number }) {
  const W = 150;
  const span = hi - lo || 1;
  const x = (t: number) => 3 + ((t - lo) * (W - 6)) / span;
  const bar = (q: Q, y: number, cls: string) =>
    q && q.p50 != null && q.p05 != null && q.p95 != null ? (
      <g className={cls}>
        <rect x={Math.min(x(q.p05), x(q.p95))} width={Math.max(1.5, Math.abs(x(q.p95) - x(q.p05)))} y={y} height={5} />
        <line x1={x(q.p50)} x2={x(q.p50)} y1={y - 2} y2={y + 7} />
      </g>
    ) : null;
  return (
    <svg className="v8d-pair" viewBox={`0 0 ${W} 22`} aria-hidden="true">
      <line x1={x(0)} x2={x(0)} y1={0} y2={22} className="v8d-zero" />
      {bar(a, 3, 'v8d-pair-a')}
      {bar(b, 13, 'v8d-pair-b')}
    </svg>
  );
}

function lastShared(v: CompareVar) {
  const both = v.periods.filter((p) => p.a && p.b);
  return both[both.length - 1] ?? v.periods[v.periods.length - 1] ?? null;
}

export function Compare({ data }: { data: Comparison }) {
  return (
    <div className="v8d-compare">
      <div className="v8d-compare-head">
        <div>
          <span className="v8d-ab v8d-ab-a">A</span> {data.a.decision.label_fr}
          <p className="v8-muted v8d-small">{data.a.diagnosis.headline_fr}</p>
        </div>
        <div>
          <span className="v8d-ab v8d-ab-b">B</span> {data.b.decision.label_fr}
          <p className="v8-muted v8d-small">{data.b.diagnosis.headline_fr}</p>
        </div>
      </div>
      <p className="v8d-small">
        <Badge kind="scenario">deux scénarios non validés</Badge> Champs modifiés : {data.changed_fields.join(', ') || 'aucun'} · {data.paired_fr}
      </p>
      <div className="v8d-table-box">
        <table className="v8d-cmp" aria-label="Comparaison A / B par variable">
          <thead>
            <tr>
              <th scope="col">Variable · pays</th>
              <th scope="col">Période</th>
              <th scope="col" className="v8-num">A médiane</th>
              <th scope="col" className="v8-num">B médiane</th>
              <th scope="col" className="v8-num">B − A (5–95 %)</th>
              <th scope="col">A / B</th>
            </tr>
          </thead>
          <tbody>
            {data.variables.map((v) => {
              const p = lastShared(v);
              const vals = p ? [p.a?.p05, p.a?.p95, p.b?.p05, p.b?.p95].filter((x): x is number => x != null) : [];
              return (
                <tr key={v.id}>
                  <th scope="row">
                    {v.label_fr ?? v.id} · {v.geo} <span className="v8d-unit">{unitFr(v.unit)}</span>
                  </th>
                  <td className="v8-num">{p ? (p.period === 'undated' ? 'non daté' : p.period) : '—'}</td>
                  <td className="v8-num">{!v.in_a ? <MissingValue reason="non atteinte dans A" compact /> : p?.a ? num(p.a.p50, true) : <MissingValue reason={p?.a_missing ?? 'non calculé'} compact />}</td>
                  <td className="v8-num">{!v.in_b ? <MissingValue reason="non atteinte dans B" compact /> : p?.b ? num(p.b.p50, true) : <MissingValue reason={p?.b_missing ?? 'non calculé'} compact />}</td>
                  <td className="v8-num" title={p?.diff_note_fr ?? undefined}>
                    {p?.diff ? (
                      <>
                        <strong>{num(p.diff.p50, true)}</strong> {p.diff.degenerate ? <span className="v8-muted">(point)</span> : <span className="v8-muted">[{num(p.diff.p05, true)} ; {num(p.diff.p95, true)}]</span>}
                      </>
                    ) : (
                      <MissingValue reason={p?.diff_note_fr ?? 'écart non calculé'} compact />
                    )}
                  </td>
                  <td>{p && vals.length ? <Pair a={p.a} b={p.b} lo={Math.min(0, ...vals)} hi={Math.max(0, ...vals)} /> : null}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="v8-muted v8d-small">Dernière période calculée dans les deux scénarios. Écarts appariés tirage par tirage : la même estimation reçoit le même tirage dans A et B.</p>
    </div>
  );
}

import { FanChart, IntervalBar } from '../../../v5/decision/FanChart';
import { Badge, MissingValue } from '../../shell/ui';
import type { HeadlineRow, RunBundle, Value } from './api';
import { matrix, num, rangeText, unitFr, valueText } from './model';

/** One cell glyph: 5–95 % bar and median against zero, on the row's shared scale. Missing → hatched text. */
function Glyph({ v, lo, hi }: { v: Value; lo: number; hi: number }) {
  if (!v || v.p50 == null || v.p05 == null || v.p95 == null) return null;
  const W = 84;
  const span = hi - lo || 1;
  const x = (t: number) => 2 + ((t - lo) * (W - 4)) / span;
  return (
    <svg className="v8d-glyph" viewBox={`0 0 ${W} 12`} aria-hidden="true">
      <line x1={x(0)} x2={x(0)} y1={0} y2={12} className="v8d-zero" />
      <rect x={Math.min(x(v.p05), x(v.p95))} width={Math.max(1.5, Math.abs(x(v.p95) - x(v.p05)))} y={3} height={6} className="v8d-glyph-band" />
      <line x1={x(v.p50)} x2={x(v.p50)} y1={1} y2={11} className="v8d-glyph-med" />
    </svg>
  );
}

export function Effects({ bundle, selected, onSelect }: { bundle: RunBundle; selected: string | null; onSelect: (id: string) => void }) {
  const rows = bundle.headline;
  const m = matrix(rows);
  const sel = rows.find((r) => r.id === selected) ?? rows[0] ?? null;
  return (
    <div className="v8d-effects">
      <div className="v8d-matrix-box">
        <table className="v8d-matrix" aria-label="Effets chiffrés par variable et par pays (médiane, 5–95 %)">
          <thead>
            <tr>
              <th scope="col">Variable (variation)</th>
              {m.geos.map((g) => (
                <th key={g} scope="col" className="v8-num">
                  {rows.find((r) => r.geo === g)?.geo_fr ?? g}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {m.concepts.map((c) => {
              const cells = m.geos.map((g) => m.cell(c.key, g));
              const vals = cells.flatMap((r) => (r?.value ? [r.value.p05, r.value.p95].filter((x): x is number => x != null) : []));
              const lo = Math.min(0, ...vals);
              const hi = Math.max(0, ...vals);
              return (
                <tr key={c.key}>
                  <th scope="row">
                    <span className="v8d-var">{c.label}</span>
                    <span className="v8d-unit">{unitFr(c.unit)}</span>
                  </th>
                  {cells.map((r, i) =>
                    r ? (
                      <td key={i} className={`v8d-cell${sel?.id === r.id ? ' is-sel' : ''}`}>
                        <button type="button" onClick={() => onSelect(r.id)} aria-pressed={sel?.id === r.id} title={r.flags.map((f) => f.title_fr ?? f.label_fr).join(' · ') || undefined}>
                          {r.value ? (
                            <>
                              <span className="v8-num v8d-val">{num(r.value.p50, true)}</span>
                              <Glyph v={r.value} lo={lo} hi={hi} />
                              <span className="v8d-per">{r.value.period ?? 'non daté'}</span>
                            </>
                          ) : (
                            <MissingValue reason="aucune période calculée" compact />
                          )}
                          {r.flags.some((f) => f.code === 'incomplete') && (
                            <span className="v8d-flag" aria-label="distribution incomplète">
                              ◌
                            </span>
                          )}
                        </button>
                      </td>
                    ) : (
                      <td key={i} className="v8d-cell v8d-cell-none">
                        <MissingValue reason="variable non atteinte pour ce pays" compact />
                      </td>
                    ),
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="v8d-legend v8-muted">
          Médiane des tirages à la dernière période calculée, barre 5–95 % ; ◌ = distribution incomplète (une estimation du chemin ne publie pas d’incertitude). Variations par rapport à la trajectoire sans décision, pas des niveaux.
        </p>
      </div>
      {sel && <Detail row={sel} bundle={bundle} />}
    </div>
  );
}

function Detail({ row, bundle }: { row: HeadlineRow; bundle: RunBundle }) {
  const v = bundle.result.variables.find((x) => x.id === row.id);
  const unit = unitFr(row.unit);
  return (
    <section className="v8d-detail" aria-label={`Détail : ${row.label_fr}, ${row.geo_fr}`}>
      <header className="v8d-detail-head">
        <h3>
          {row.label_fr} · {row.geo_fr}
        </h3>
        <div className="v8-row">
          <Badge kind="scenario" title={bundle.honesty.label}>
            {bundle.honesty.validated ? 'chaîne validée' : 'scénario non validé — pas une probabilité'}
          </Badge>
          {row.flags.map((f) => (
            <Badge key={f.code} kind={f.code === 'incomplete' ? 'missing' : 'info'} title={f.title_fr ?? undefined}>
              {f.label_fr}
            </Badge>
          ))}
        </div>
      </header>
      <p className="v8d-answer">
        {row.value ? (
          <>
            <strong className="v8-num">{valueText(row.value, row.unit)}</strong> {row.value.period_label_fr} <span className="v8-muted">{rangeText(row.value)}</span>
          </>
        ) : (
          <MissingValue reason="aucune période calculée sur ce chemin" />
        )}
      </p>
      {v && v.frequency !== 'undated' && v.periods.length > 0 && (
        <div className="v8d-fan">
          <FanChart periods={v.periods} unit={unit} label={`${row.label_fr} (${row.geo_fr})`} />
        </div>
      )}
      {v && v.frequency === 'undated' && v.undated && (
        <div className="v8d-fan">
          <IntervalBar q={v.undated} unit={unit} label={row.label_fr} kind={v.undated.kind_label_fr ?? 'non daté'} />
          <p className="v8-muted v8d-small">{v.undated.kind_label_fr ?? 'résultat non daté'} : pas d’axe temporel (la source ne date pas l’effet).</p>
        </div>
      )}
      <dl className="v8d-facts">
        <dt>Chemin</dt>
        <dd>{row.sources.length ? row.sources.map((s) => s.short).join(' → ') : row.models.join(', ') || '—'}</dd>
        {v?.baseline && (
          <>
            <dt>Référence</dt>
            <dd>
              {(v.baseline.label_fr as string | undefined) ?? 'trajectoire de référence'}
              {v.baseline.value != null && ` : ${v.baseline.value} ${v.baseline.unit ?? ''} (${v.baseline.year ?? ''})`}
            </dd>
          </>
        )}
      </dl>
      {row.notes_fr.length > 0 && (
        <ul className="v8d-notes">
          {row.notes_fr.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * The scoreboard matrix: rows = targets grouped by domain, columns = official forecasters (+ naive reference).
 * A cell shows the share of verdicts as a segmented bar (wins / losses / ties / pending, never a single colour
 * that hides losses), the counts and the ratio range. "Pas encore de piste" cells are hatched with their reason.
 * Arrow keys move between cells; Enter opens the cell (?cell=…).
 */
import { useRef, type KeyboardEvent } from 'react';
import type { Cell, Forecaster, MatrixDomain } from './api';
import { VERDICT_FR, fmtRange, total } from './model';

function Segments({ c }: { c: Cell }) {
  const counts = c.counts;
  const n = total(counts);
  if (!counts || n === 0) return null;
  return (
    <span className="sc-seg" aria-hidden="true">
      {(['bat', 'egalite', 'ne_bat_pas', 'en_attente'] as const).map((k) =>
        counts[k] ? <span key={k} className={`sc-seg-${k}`} style={{ flexGrow: counts[k] }} /> : null,
      )}
    </span>
  );
}

function cellLabel(c: Cell, target: string, f: string): string {
  if (c.status === 'no_track') return `${target} × ${f} : pas encore de piste. ${c.reason_fr ?? ''}`;
  const k = c.counts;
  const parts = k ? (['bat', 'ne_bat_pas', 'egalite', 'en_attente'] as const).filter((v) => k[v]).map((v) => `${k[v]} ${VERDICT_FR[v]}`) : [];
  const weak = c.weak_reference && Object.values(c.weak_reference).some(Boolean) ? ' ; plus des comparaisons contre un repère faible (non comptées)' : '';
  const r = fmtRange(c.ratio_min, c.ratio_max);
  return `${target} × ${f} : ${parts.join(', ') || 'aucune comparaison comptée'}${r ? `, ratios ${r}` : ''}${weak}`;
}

export function CellButton({ c, target, f, onOpen, active, r, col }: { c: Cell; target: string; f: string; onOpen: (key: string) => void; active: boolean; r: number; col: number }) {
  const k = c.counts;
  const n = total(k);
  const range = fmtRange(c.ratio_min, c.ratio_max);
  const weakOnly = c.status === 'weak_only' || (n === 0 && c.weak_reference && Object.keys(c.weak_reference).length > 0);
  return (
    <button
      type="button"
      className={`sc-cell sc-cell-${c.status}${active ? ' sc-cell-active' : ''}`}
      onClick={() => onOpen(c.key)}
      aria-label={cellLabel(c, target, f)}
      title={cellLabel(c, target, f)}
      data-r={r}
      data-c={col}
      data-testid={`cell-${c.key}`}
    >
      {c.status === 'no_track' ? (
        <span className="sc-cell-none">{c.partial ? 'pas de piste V7' : 'pas encore de piste'}</span>
      ) : weakOnly ? (
        <span className="sc-cell-weak">repère faible seulement</span>
      ) : (
        <>
          <span className="sc-cell-top">
            {k && k.bat > 0 && <span className="sc-cell-win v8-num">{k.bat} ✓</span>}
            {k && k.ne_bat_pas > 0 && <span className="sc-cell-loss v8-num">{k.ne_bat_pas} ✗</span>}
            {k && k.egalite > 0 && <span className="sc-cell-tie v8-num">{k.egalite} =</span>}
            {k && k.en_attente > 0 && <span className="sc-cell-pend v8-num">{k.en_attente} ⏳</span>}
          </span>
          <Segments c={c} />
          <span className="sc-cell-ratio v8-num">{range ?? (k && k.en_attente === n ? 'à juger' : '')}</span>
        </>
      )}
    </button>
  );
}

export function Matrix({ matrix, forecasters, onOpen, activeKey, focusForecaster, domainFilter }: { matrix: MatrixDomain[]; forecasters: Forecaster[]; onOpen: (key: string) => void; activeKey: string; focusForecaster: string; domainFilter: string }) {
  const ref = useRef<HTMLTableElement>(null);
  const cols = focusForecaster ? forecasters.filter((f) => f.id === focusForecaster) : forecasters;
  const domains = domainFilter ? matrix.filter((d) => d.domain === domainFilter) : matrix;
  const showNaive = !focusForecaster;
  const onKey = (e: KeyboardEvent<HTMLTableElement>) => {
    const t = e.target as HTMLElement;
    const r = Number(t.dataset.r);
    const c = Number(t.dataset.c);
    if (!Number.isFinite(r) || !Number.isFinite(c)) return;
    const d = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }[e.key];
    if (!d) return;
    e.preventDefault();
    const all = [...(ref.current?.querySelectorAll<HTMLElement>('[data-r]') ?? [])];
    const cand = all.filter((el) => (d[0] ? Number(el.dataset.c) === c && Math.sign(Number(el.dataset.r) - r) === d[0] : Number(el.dataset.r) === r && Math.sign(Number(el.dataset.c) - c) === d[1]));
    cand.sort((a, b) => Math.abs(Number(a.dataset.r) - r) + Math.abs(Number(a.dataset.c) - c) - (Math.abs(Number(b.dataset.r) - r) + Math.abs(Number(b.dataset.c) - c)));
    cand[0]?.focus();
  };
  let rowIdx = 0;
  return (
    <div className="sc-matrix-box">
      <table className="sc-matrix" ref={ref} onKeyDown={onKey} aria-describedby="sc-matrix-help">
        <caption className="v8-sr-only">Tableau des scores : cibles en lignes, prévisionnistes officiels en colonnes</caption>
        <thead>
          <tr>
            <th scope="col" className="sc-matrix-corner">
              Cible
            </th>
            {cols.map((f) => (
              <th key={f.id} scope="col" title={`${f.label_fr} — ${f.org ?? ''}${f.product ? ` · ${f.product}` : ''}`} className={f.status === 'planned' ? 'sc-col-planned' : undefined}>
                {f.short}
              </th>
            ))}
            {showNaive && (
              <th scope="col" className="sc-col-naive" title="Persistance, marche aléatoire : comptées à part, jamais dans le titre">
                Réf. naïve
              </th>
            )}
          </tr>
        </thead>
        {domains.map((d) => (
          <tbody key={d.domain}>
            <tr className="sc-domain-row">
              <th scope="rowgroup" colSpan={cols.length + (showNaive ? 2 : 1)}>
                {d.label_fr}
              </th>
            </tr>
            {d.targets.map((t) => {
              const r = rowIdx++;
              return (
                <tr key={t.target}>
                  <th scope="row" className="sc-target">
                    {t.label_fr}
                  </th>
                  {cols.map((f, ci) => {
                    const c = t.cells[f.id];
                    return <td key={f.id}>{c ? <CellButton c={c} target={t.label_fr} f={f.label_fr} onOpen={onOpen} active={activeKey === c.key} r={r} col={ci} /> : <span className="sc-cell-na" title="sans objet : ce prévisionniste ne publie pas cette cible" />}</td>;
                  })}
                  {showNaive && (
                    <td>
                      {t.naive ? (
                        <CellButton c={t.naive} target={t.label_fr} f="référence naïve" onOpen={onOpen} active={activeKey === t.naive.key} r={r} col={cols.length} />
                      ) : t.no_track ? (
                        <span className="sc-cell sc-cell-no_track sc-cell-static" title={t.no_track.reason_fr}>
                          <span className="sc-cell-none">pas encore de piste</span>
                        </span>
                      ) : (
                        <span className="sc-cell-na" title="aucune comparaison avec une référence naïve" />
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        ))}
      </table>
      <p id="sc-matrix-help" className="sc-help">
        ✓ bat · ✗ ne bat pas · = égalité · ⏳ en attente. Barre = part de chaque verdict ; ratio = erreur REVA / erreur officielle (plus bas = REVA meilleure). Flèches pour
        se déplacer, Entrée pour ouvrir.
      </p>
    </div>
  );
}

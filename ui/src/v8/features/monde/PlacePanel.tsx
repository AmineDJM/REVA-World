import { useMemo } from 'react';
import { Link } from 'react-router';
import { v8Links } from '../../contract';
import { Badge, ErrorState, MiniLine, MissingValue, Skeleton, SourceChip, frValue } from '../../shell/ui';
import { getPlace, type PlaceResponse, type Selector } from './api';
import { selectorKey } from './url';
import { dataOf, useAsync } from './useAsync';

/** Numeric x of a point for the sparkline (year, or fractional year for sub-annual periods). */
export function xOf(ps: string): number {
  const neg = ps.startsWith('-');
  const [y, m, d] = (neg ? ps.slice(1) : ps).split('-').map(Number) as [number, number, number];
  const year = neg ? -y : y;
  return year + ((m || 1) - 1) / 12 + ((d || 1) - 1) / 365;
}

/**
 * The selected place's whole series (answer first: the latest observed value), sparkline (projections dashed),
 * every period in a table with its source, and the links onward (Fiche, Comparer).
 */
export function PlacePanel({ sel, geo, name, precision, onClose }: { sel: Selector; geo: string; name: string; precision: string; onClose: () => void }) {
  const a = useAsync<PlaceResponse>(`${selectorKey(sel)}@${geo}@${precision}`, () => getPlace(sel, geo, precision));
  const r = dataOf(a);
  const d = r?.data;
  const pts = useMemo(() => d?.points ?? [], [d]);
  const latest = [...pts].reverse().find((p) => p.v !== null && !p.proj) ?? [...pts].reverse().find((p) => p.t !== null && !p.proj);
  const firstProj = pts.find((p) => p.proj);
  const isCountry = /^[A-Z]{3}$/.test(geo);
  return (
    <section className="v8m-card v8m-panel" aria-label={`Série de ${name}`} data-testid="place-panel">
      <header className="v8m-panel-head">
        <div>
          <h3>{name}</h3>
          <span className="v8-muted v8-mono">{geo}</span>
        </div>
        <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={onClose} aria-label="Fermer le panneau">
          ✕
        </button>
      </header>
      {a.status === 'error' && !d && <ErrorState error={a.error} what="la série de ce lieu" onRetry={a.reload} />}
      {!d && a.status === 'loading' && <Skeleton lines={5} />}
      {d && (
        <>
          <div className="v8m-panel-answer">
            {latest ? (
              <>
                <span className="v8m-panel-value v8-num">
                  {latest.v !== null ? frValue(latest.v, latest.unit) : latest.t} <span className="v8m-panel-unit">{latest.unit !== '1' ? latest.unit : ''}</span>
                </span>
                <span className="v8-muted">dernière valeur observée · {latest.p}</span>
              </>
            ) : (
              <MissingValue reason="aucune valeur observée pour ce lieu dans cette série" />
            )}
          </div>
          <MiniLine
            points={pts.filter((p) => p.v !== null).map((p) => ({ x: xOf(p.ps), y: Number(p.v) }))}
            split={firstProj ? xOf(firstProj.ps) : undefined}
            width={300}
            height={64}
            label={`Série ${d.meta.series} pour ${name}, ${pts.length} périodes`}
          />
          {firstProj && (
            <p className="v8m-panel-note">
              <Badge kind="projection" /> à partir de {firstProj.p} (tirets) — jamais mêlé aux observations.
            </p>
          )}
          <div className="v8m-panel-table" tabIndex={0}>
            <table>
              <caption className="v8-sr-only">Valeurs de {name} par période</caption>
              <thead>
                <tr>
                  <th scope="col">Période</th>
                  <th scope="col" className="v8-num">
                    Valeur
                  </th>
                  <th scope="col">Source</th>
                </tr>
              </thead>
              <tbody>
                {[...pts].reverse().map((p) => (
                  <tr key={p.ps + (p.pe ?? '')} className={p.proj ? 'is-proj' : undefined}>
                    <td className="v8-num">
                      {p.p}
                      {p.proj && <span className="v8m-proj-pill">proj.</span>}
                    </td>
                    <td className="v8-num">{p.v !== null ? frValue(p.v, p.unit) : p.t !== null ? p.t : <MissingValue reason={p.miss} compact />}</td>
                    <td>
                      <SourceChip source={p.series ?? d.meta.series} label={d.meta.source} licence={p.lic !== null ? (d.licences[p.lic] ?? null) : null} period={`${p.ps} → ${p.pe}`} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav className="v8m-panel-links" aria-label="Aller plus loin">
            {isCountry && <Link to={v8Links.entity('country', geo)}>Fiche {name} →</Link>}
            {isCountry && <Link to={`/v8/comparer?${new URLSearchParams({ s: d.meta.series, g: geo }).toString()}`}>Comparer →</Link>}
          </nav>
        </>
      )}
    </section>
  );
}

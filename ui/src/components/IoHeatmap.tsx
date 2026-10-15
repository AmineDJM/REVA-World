import { useMemo, useState } from 'react';
import { DARK_FILL, classify, legendRows, niceScale } from '../lib/colorScale';
import { displayDecimal, missingReasonLabel } from '../lib/format';
import { cellKey, type IoCell, type IoMatrix } from '../lib/runviews';

export interface IoHeatmapProps {
  matrix: IoMatrix;
  countryLabel?: (iso: string) => string;
}

function cellText(c: IoCell | undefined): string {
  if (!c) return 'not reported';
  if (c.value === null) return `missing (${missingReasonLabel(c.missingReason)})`;
  return `${displayDecimal(c.value, 'unit.percent').text} %`;
}

/**
 * Country × industry heatmap of the output-price change (%). Every cell carries its value as
 * text for assistive technology; colour classes come from fixed breaks printed in the legend.
 * Shocked (exogenous) cells are outlined; industries without output are marked "n/a".
 */
export function IoHeatmap({ matrix, countryLabel = (c) => c }: IoHeatmapProps) {
  const values = useMemo(() => [...matrix.cells.values()].map((c) => c.value).filter((v): v is string => v !== null), [matrix]);
  const scale = useMemo(() => niceScale(values, '%'), [values]);
  const [hover, setHover] = useState<{ country: string; industry: string } | null>(null);
  const hc = hover ? matrix.cells.get(cellKey(hover.country, hover.industry)) : undefined;

  return (
    <div className="io-heatmap">
      <p className="chart-readout" aria-live="polite" data-testid="heatmap-readout">
        {hover ? (
          <>
            <strong>
              {countryLabel(hover.country)} · {hover.industry}
            </strong>
            : {cellText(hc)}
            {hc?.exogenous && <span className="muted"> · {hc.exogenous}</span>}
            {hc?.note && <span className="muted"> · {hc.note}</span>}
          </>
        ) : (
          <span className="muted">Hover a cell for its exact value. Outlined cells are the shocked industries (price set by the scenario).</span>
        )}
      </p>
      <div className="table-wrap heatmap-wrap">
        <table className="heatmap" data-testid="io-heatmap">
          <caption>
            Output-price change by country (rows) and industry (columns), % of the baseline price; {matrix.countries.length} countries ×{' '}
            {matrix.industries.length} industries
          </caption>
          <thead>
            <tr>
              <th scope="col">Country</th>
              {matrix.industries.map((ind) => (
                <th scope="col" key={ind} className="hm-col">
                  <span>{ind}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody onPointerLeave={() => setHover(null)}>
            {matrix.countries.map((c) => (
              <tr key={c}>
                <th scope="row">{countryLabel(c)}</th>
                {matrix.industries.map((ind) => {
                  const cell = matrix.cells.get(cellKey(c, ind));
                  const cls = cell?.value != null ? classify(cell.value, scale) : null;
                  const na = !!cell?.note;
                  return (
                    <td
                      key={ind}
                      className={`hm-cell${cell?.exogenous ? ' hm-shocked' : ''}${cell?.value === null ? ' hm-missing' : ''}${na ? ' hm-na' : ''}`}
                      style={cls ? { background: cls.color, color: DARK_FILL.has(cls.color) ? '#fff' : '#111' } : undefined}
                      onPointerEnter={() => setHover({ country: c, industry: ind })}
                      data-exact={cell?.value ?? undefined}
                    >
                      <span className="sr-only">
                        {ind}: {cellText(cell)}
                        {cell?.exogenous ? ' (shocked)' : ''}
                      </span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="legend legend-scale" aria-label="Heatmap legend">
        {legendRows(scale).map((r) => (
          <li key={r.step}>
            <span className="swatch" style={{ background: r.color }} aria-hidden="true" /> {r.label}
          </li>
        ))}
        <li>
          <span className="swatch swatch-hatch" aria-hidden="true" /> missing / no output (n/a)
        </li>
        <li>
          <span className="swatch swatch-shocked" aria-hidden="true" /> shocked industry (exogenous)
        </li>
      </ul>
    </div>
  );
}

import { useEffect, useRef, useState } from 'react';
import type uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { installLocaleGuard } from '../../../lib/locale';
import { frCompact, frNumber } from '../../shell/ui';
import type { Shown } from './transforms';

export interface ChartPlace {
  iso3: string;
  name: string;
  color: string;
  points: Shown[];
}

export interface CompareChartProps {
  places: ChartPlace[];
  /** Shared x range of every chart (aligned panels). */
  xRange: [number, number];
  /** Integer x (annual) or fractional (sub-annual). */
  annual: boolean;
  unit: string;
  label: string;
  syncKey: string;
  height?: number;
  /** Shared cursor x (from any chart) → value readout in the legend. */
  onCursor?: (x: number | null) => void;
  cursorX: number | null;
}

function cssVar(name: string, fallback: string): string {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  } catch {
    return fallback;
  }
}

/** Value of a place at x (exact x match; aligned periods). */
export function valueAt(pts: Shown[], x: number | null): Shown | null {
  if (x === null) return null;
  let best: Shown | null = null;
  for (const p of pts) if (Math.abs(p.x - x) < 1e-6) best = p;
  return best;
}

/**
 * One indicator, N places: uPlot lines on a shared x range with a synchronised crosshair across the panels.
 * Observations solid; projections dashed in the same place colour (separate series, never joined into the
 * observed line); missing values are gaps (never 0).
 */
export function CompareChart({ places, xRange, annual, unit, label, syncKey, height = 220, onCursor, cursorX }: CompareChartProps) {
  const el = useRef<HTMLDivElement>(null);
  const cb = useRef(onCursor);
  cb.current = onCursor;
  const [fallback, setFallback] = useState(false);

  useEffect(() => {
    const node = el.current;
    if (!node) return;
    let ctx: CanvasRenderingContext2D | null = null;
    try {
      ctx = typeof ResizeObserver === 'undefined' ? null : document.createElement('canvas').getContext('2d');
    } catch {
      ctx = null;
    }
    if (!ctx) {
      setFallback(true);
      return;
    }
    const xs = [...new Set(places.flatMap((p) => p.points.map((q) => q.x)))].sort((a, b) => a - b);
    const data: (number | null)[][] = [xs];
    const series: uPlot.Series[] = [{}];
    for (const pl of places) {
      const by = new Map(pl.points.map((q) => [q.x, q]));
      const obs = xs.map((x) => {
        const q = by.get(x);
        return q && !q.proj ? q.y : null;
      });
      // the projection line starts at the last observation before it, so the eye sees the hand-over
      const firstProjIdx = xs.findIndex((x) => by.get(x)?.proj && by.get(x)?.y !== null);
      const proj = xs.map((x, i) => {
        const q = by.get(x);
        if (q?.proj) return q.y;
        if (firstProjIdx > 0 && i === firstProjIdx - 1 && q && !q.proj) return q.y;
        return null;
      });
      data.push(obs, proj);
      series.push(
        { label: pl.name, stroke: pl.color, width: 2, spanGaps: false, points: { show: xs.length < 40, size: 4, fill: pl.color } },
        { label: `${pl.name} (projection)`, stroke: pl.color, width: 1.5, dash: [5, 4], spanGaps: false, points: { show: false } },
      );
    }
    const grid = cssVar('--v8-border', '#e2e4e8');
    const ink = cssVar('--v8-fg-muted', '#565d6a');
    let plot: uPlot | null = null;
    let ro: ResizeObserver | null = null;
    let cancelled = false;
    installLocaleGuard();
    import('uplot')
      .then(({ default: UPlot }) => {
        if (cancelled) return;
        plot = new UPlot(
          {
            width: Math.max(280, node.clientWidth || 600),
            height,
            series,
            scales: { x: { time: false, range: () => xRange } },
            axes: [
              {
                stroke: ink,
                grid: { stroke: grid, width: 1 },
                ticks: { stroke: grid },
                incrs: annual ? [1, 2, 5, 10, 20, 50, 100] : undefined,
                values: (_u, vals) => vals.map((v) => (annual ? String(Math.round(v)) : Number.isInteger(v) ? String(v) : `${Math.floor(v)}`)),
              },
              { stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, size: 56, values: (_u, vals) => vals.map((v) => frCompact(v)) },
            ],
            legend: { show: false },
            cursor: {
              sync: { key: syncKey, setSeries: false },
              drag: { x: false, y: false },
              points: { size: 6 },
            },
            hooks: {
              setCursor: [
                (u) => {
                  const i = u.cursor.idx;
                  cb.current?.(i === null || i === undefined ? null : (u.data[0][i] ?? null));
                },
              ],
            },
          },
          data as uPlot.AlignedData,
          node,
        );
        ro = new ResizeObserver(() => plot?.setSize({ width: Math.max(280, node.clientWidth), height }));
        ro.observe(node);
      })
      .catch(() => !cancelled && setFallback(true));
    return () => {
      cancelled = true;
      ro?.disconnect();
      plot?.destroy();
    };
  }, [places, xRange, annual, height, syncKey]);

  return (
    <div className="v8c-chart">
      <ul className="v8c-legend" aria-label="Légende">
        {places.map((pl) => {
          const at = valueAt(pl.points, cursorX) ?? [...pl.points].reverse().find((q) => q.y !== null && !q.proj) ?? [...pl.points].reverse().find((q) => q.y !== null) ?? null;
          return (
            <li key={pl.iso3}>
              <span className="v8c-sw" style={{ background: pl.color }} aria-hidden="true" />
              <span className="v8c-legend-name">{pl.name}</span>
              {at && at.y !== null ? (
                <span className={`v8-num v8c-legend-val${at.proj ? ' is-proj' : ''}`}>
                  {frNumber(at.y, Math.abs(at.y) < 10 ? 2 : 1)} <span className="v8-subtle">{at.p}</span>
                  {at.proj ? ' ᴾ' : ''}
                </span>
              ) : (
                <span className="v8c-legend-miss">{at ? `manquant : ${at.miss ?? 'non rapporté'}` : ''}</span>
              )}
            </li>
          );
        })}
      </ul>
      {fallback ? (
        <p className="v8c-nochart v8-muted">Graphique indisponible dans ce navigateur : la vue Tableau contient les mêmes valeurs.</p>
      ) : (
        <div ref={el} className="v8c-plot" role="img" aria-label={`${label} (${unit}) : ${places.map((p) => p.name).join(', ')}. La vue Tableau donne les valeurs.`} />
      )}
    </div>
  );
}

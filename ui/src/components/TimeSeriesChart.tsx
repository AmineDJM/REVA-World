import { useEffect, useRef } from 'react';
import type uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';

export interface SeriesPoint {
  /** Year (x). */
  x: number;
  /** Numeric value or null for missing (drawn as a gap, never as 0). */
  y: number | null;
  lower?: number | null;
  upper?: number | null;
}

export interface TimeSeriesChartProps {
  points: SeriesPoint[];
  label: string;
  yLabel: string;
  /** How the band (if any) must be read. */
  bandKind?: 'prediction' | 'sensitivity' | 'bounds' | null;
  height?: number;
}

/**
 * Small uPlot line chart. Missing points are gaps. A band, if present, is
 * styled by kind (prediction interval: solid fill; sensitivity range: dashed
 * edges; bounds: dotted edges).
 */
export function TimeSeriesChart({ points, label, yLabel, bandKind = null, height = 220 }: TimeSeriesChartProps) {
  const el = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = el.current;
    if (!node || points.length === 0) return;
    if (typeof (window as unknown as { ResizeObserver?: unknown }).ResizeObserver === 'undefined') return;
    let ctx: CanvasRenderingContext2D | null = null;
    try {
      ctx = document.createElement('canvas').getContext('2d');
    } catch {
      ctx = null;
    }
    if (!ctx) return; // no canvas (tests / very old browsers): table alternative remains
    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    const hasBand = bandKind && points.some((p) => p.lower != null && p.upper != null);
    const series: uPlot.Series[] = [
      {},
      { label: yLabel, stroke: '#1f4e79', width: 2, spanGaps: false, points: { show: true, size: 5 } },
    ];
    const data: uPlot.AlignedData = [xs, ys];
    if (hasBand) {
      const dash = bandKind === 'sensitivity' ? [6, 4] : bandKind === 'bounds' ? [2, 3] : undefined;
      series.push(
        { label: 'lower', stroke: '#5b7fa6', dash, width: 1, spanGaps: false, points: { show: false } },
        { label: 'upper', stroke: '#5b7fa6', dash, width: 1, spanGaps: false, points: { show: false } },
      );
      data.push(points.map((p) => p.lower ?? null), points.map((p) => p.upper ?? null));
    }
    const width = Math.max(280, node.clientWidth || 560);
    let plot: uPlot | null = null;
    let ro: ResizeObserver | null = null;
    let cancelled = false;
    // Loaded lazily: a chart failure (e.g. an unsupported browser locale) must never break the page;
    // the data table next to the chart carries the same values.
    import('uplot')
      .then(({ default: UPlot }) => {
        if (cancelled) return;
        plot = new UPlot(
      {
        width,
        height,
        series,
        bands: hasBand && bandKind === 'prediction' ? [{ series: [3, 2], fill: 'rgba(31,78,121,0.15)' }] : undefined,
        scales: { x: { time: false } },
        axes: [
          // Years are integers: tick only at whole-year increments so labels never repeat.
          { incrs: [1, 2, 5, 10, 20, 50, 100], values: (_u, vals) => vals.map((v) => String(Math.round(v))) },
          { label: yLabel, size: 80, values: (_u, vals) => vals.map((v) => compact(v)) },
        ],
        legend: { show: false },
        cursor: { drag: { x: false, y: false } },
      },
      data,
      node,
    );
        ro = new ResizeObserver(() => plot?.setSize({ width: Math.max(280, node.clientWidth), height }));
        ro.observe(node);
      })
      .catch(() => {
        if (!cancelled) node.textContent = 'Chart unavailable in this browser; see the table.';
      });
    return () => {
      cancelled = true;
      ro?.disconnect();
      plot?.destroy();
    };
  }, [points, bandKind, height, yLabel]);

  return <div ref={el} className="chart" role="img" aria-label={label} />;
}

function compact(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e12) return `${(v / 1e12).toFixed(1)}T`;
  if (a >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(v);
}

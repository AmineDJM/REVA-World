import type { AtlasSeriesPoint } from '../api/atlas';
import { toNumber } from '../lib/format';

/**
 * Tiny line of a best-available annual series. Gaps between reported years are drawn as breaks
 * (no interpolation across missing years); points from a different source than the latest are
 * hollow, and the line is interrupted where the answering source changes (review V3b HL-1: two sources
 * are never drawn as one continuous line), so a source change is never read as a change of the value.
 */
export function Sparkline({ points, width = 120, height = 28, latestSeries }: { points: AtlasSeriesPoint[]; width?: number; height?: number; latestSeries?: string }) {
  const pts = points.map((p) => ({ year: p.year, v: toNumber(p.value), series: p.series })).filter((p): p is { year: number; v: number; series: string } => p.v !== null);
  if (pts.length < 2) return null;
  const y0 = pts[0]!.year;
  const y1 = pts[pts.length - 1]!.year;
  const lo = Math.min(...pts.map((p) => p.v));
  const hi = Math.max(...pts.map((p) => p.v));
  const X = (y: number) => 2 + ((y - y0) / Math.max(1, y1 - y0)) * (width - 4);
  const Y = (v: number) => height - 3 - ((v - lo) / (hi - lo || 1)) * (height - 6);
  const groups: (typeof pts)[] = [];
  const breaks: number[] = [];
  pts.forEach((p, i) => {
    const switched = i > 0 && p.series !== pts[i - 1]!.series;
    if (switched) breaks.push(p.year);
    if (i === 0 || p.year - pts[i - 1]!.year > 1 || switched) groups.push([p]);
    else groups[groups.length - 1]!.push(p);
  });
  const segs = groups.map((g) => g.map((p, i) => `${i ? 'L' : 'M'}${X(p.year).toFixed(1)},${Y(p.v).toFixed(1)}`).join('') + (g.length === 1 ? 'h0.01' : ''));
  const changed = latestSeries ? pts.filter((p) => p.series !== latestSeries) : [];
  return (
    <svg width={width} height={height} className="v3-spark" role="img" aria-label={`Série ${y0}–${y1}, ${pts.length} années rapportées${breaks.length ? ` ; changement de source en ${breaks.join(', ')} (ligne interrompue)` : ''}`} data-source-breaks={breaks.join(',')}>
      {segs.filter(Boolean).map((d, i) => (
        <path key={i} d={d} fill="none" stroke="currentColor" strokeWidth="1.4" />
      ))}
      {changed.map((p) => (
        <circle key={p.year} cx={X(p.year)} cy={Y(p.v)} r="1.8" fill="var(--bg, #fff)" stroke="currentColor" strokeWidth="0.8" />
      ))}
    </svg>
  );
}

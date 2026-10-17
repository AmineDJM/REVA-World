/**
 * Mini chart of a series on a board card. Observed values: solid line; projections: dashed line in the
 * projection colour (never drawn as observations); a missing year interrupts the line (no interpolation);
 * a change of answering source interrupts it too and is marked with a tick.
 */
import type { SeriesPoint } from './live';

interface Seg {
  projection: boolean;
  pts: { x: number; y: number }[];
}

export function MiniSeries({ points, width = 260, height = 56, breaks = [] }: { points: SeriesPoint[]; width?: number; height?: number; breaks?: number[] }) {
  const num = points.filter((p) => p.value !== null).map((p) => ({ ...p, v: Number(p.value) })).filter((p) => Number.isFinite(p.v));
  if (num.length < 2) return null;
  const y0 = points[0]!.year;
  const y1 = points[points.length - 1]!.year;
  const lo = Math.min(...num.map((p) => p.v));
  const hi = Math.max(...num.map((p) => p.v));
  const pad = 4;
  const X = (y: number) => pad + ((y - y0) / Math.max(1, y1 - y0)) * (width - 2 * pad);
  const Y = (v: number) => height - pad - ((v - lo) / (hi - lo || 1)) * (height - 2 * pad);
  const segs: Seg[] = [];
  let prev: (typeof num)[number] | null = null;
  for (const p of num) {
    const cont = prev && p.year - prev.year === 1 && p.projection === prev.projection && p.source === prev.source;
    if (!cont) {
      const seg: Seg = { projection: p.projection, pts: [] };
      // a projection continues from the last observation (visual join, still dashed)
      if (prev && p.year - prev.year === 1 && p.projection && !prev.projection && p.source === prev.source) seg.pts.push({ x: X(prev.year), y: Y(prev.v) });
      segs.push(seg);
    }
    segs[segs.length - 1]!.pts.push({ x: X(p.year), y: Y(p.v) });
    prev = p;
  }
  const lastObs = [...num].reverse().find((p) => !p.projection);
  const nProj = num.filter((p) => p.projection).length;
  const gaps = points.filter((p) => p.value === null).length;
  const label = `Série ${y0}–${y1} : ${num.length - nProj} valeurs observées${nProj ? `, ${nProj} projections (pointillés)` : ''}${gaps ? `, ${gaps} années manquantes (ligne interrompue)` : ''}${breaks.length ? `, changement de source en ${breaks.join(', ')}` : ''}.`;
  return (
    <svg className="v8b-mini" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
      {breaks.map((b) => (
        <line key={b} x1={X(b) - 0.5} x2={X(b) - 0.5} y1={0} y2={height} className="v8b-mini-break" />
      ))}
      {segs.map((s, i) =>
        s.pts.length === 1 ? (
          <circle key={i} cx={s.pts[0]!.x} cy={s.pts[0]!.y} r={1.6} className={s.projection ? 'v8b-mini-proj-dot' : 'v8b-mini-obs-dot'} />
        ) : (
          <polyline
            key={i}
            points={s.pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')}
            className={s.projection ? 'v8b-mini-proj' : 'v8b-mini-obs'}
          />
        ),
      )}
      {lastObs && <circle cx={X(lastObs.year)} cy={Y(lastObs.v)} r={2.4} className="v8b-mini-last" />}
    </svg>
  );
}

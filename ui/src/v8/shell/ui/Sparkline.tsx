/**
 * Sparklines. `Sparkline` is the V3 one (atlas best-available annual series: gaps break the line,
 * a source change interrupts it). `MiniLine` is a generic one for {x, y|null} points: null = gap
 * (never interpolated, never 0); `kind="projection"|"scenario"` draws dashed in the honesty colour.
 */
export { Sparkline } from '../../../v3/components/Sparkline';

export interface MiniPoint {
  x: number;
  y: number | null;
}

export function MiniLine({
  points,
  width = 120,
  height = 28,
  kind = 'observed',
  label,
  split,
}: {
  points: MiniPoint[];
  width?: number;
  height?: number;
  kind?: 'observed' | 'projection' | 'scenario' | 'estimate';
  label?: string;
  /** x from which points are projections (drawn dashed), e.g. the last observed year + 1. */
  split?: number;
}) {
  const pts = points.filter((p): p is { x: number; y: number } => p.y !== null && Number.isFinite(p.y));
  if (pts.length < 2) return <svg width={width} height={height} className="v8-mini" role="img" aria-label={label ?? 'Série trop courte pour un tracé'} />;
  const x0 = Math.min(...pts.map((p) => p.x));
  const x1 = Math.max(...pts.map((p) => p.x));
  const lo = Math.min(...pts.map((p) => p.y));
  const hi = Math.max(...pts.map((p) => p.y));
  const X = (x: number) => 2 + ((x - x0) / (x1 - x0 || 1)) * (width - 4);
  const Y = (y: number) => height - 3 - ((y - lo) / (hi - lo || 1)) * (height - 6);
  // Segments break at nulls (gaps), and at the observed→projection split.
  const segs: { d: string; dashed: boolean }[] = [];
  let cur: string[] = [];
  let curDashed = kind !== 'observed';
  const flush = () => {
    if (cur.length) segs.push({ d: cur.join('') + (cur.length === 1 ? 'h0.01' : ''), dashed: curDashed });
    cur = [];
  };
  const sorted = [...points].sort((a, b) => a.x - b.x);
  for (const p of sorted) {
    if (p.y === null || !Number.isFinite(p.y)) {
      flush();
      continue;
    }
    const dashed = kind !== 'observed' || (split !== undefined && p.x >= split);
    if (dashed !== curDashed && cur.length) {
      const last = cur[cur.length - 1]!;
      flush();
      cur.push(last.replace(/^L/, 'M'));
    }
    curDashed = dashed;
    cur.push(`${cur.length ? 'L' : 'M'}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`);
  }
  flush();
  return (
    <svg width={width} height={height} className={`v8-mini v8-mini-${kind}`} role="img" aria-label={label ?? `Série ${x0}–${x1}, ${pts.length} points`}>
      {segs.map((s, i) => (
        <path key={i} d={s.d} fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray={s.dashed ? '3 2' : undefined} className={s.dashed ? 'v8-mini-dash' : undefined} />
      ))}
    </svg>
  );
}

import { useMemo, useRef, useState } from 'react';
import { frCompact } from '../../shell/ui';

export interface ScatterPoint {
  iso3: string;
  name: string;
  x: number;
  y: number;
  /** Either coordinate is a projection: drawn hollow, dashed ring. */
  proj: boolean;
  xText: string;
  yText: string;
}

export interface ScatterProps {
  points: ScatterPoint[];
  xLabel: string;
  yLabel: string;
  logx: boolean;
  logy: boolean;
  selected: string | null;
  onSelect: (iso3: string) => void;
  onHover?: (iso3: string | null) => void;
  height?: number;
}

function niceTicks(lo: number, hi: number, n = 5): number[] {
  if (!(hi > lo)) return [lo];
  const step0 = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) ?? 10 * mag;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

function logTicks(lo: number, hi: number): number[] {
  const out: number[] = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
    const v = 10 ** e;
    if (v >= lo / 1.0001 && v <= hi * 1.0001) out.push(v);
  }
  return out.length >= 2 ? out : niceTicks(lo, hi);
}

/**
 * Bivariate scatter (SVG): one dot per country with a value for both indicators in the period. Labels on the
 * extremes and the selected country; hover shows both exact values. Linked to the map selection.
 * Log axes only when every value on the axis is > 0 (the toggle says so otherwise).
 */
export function Scatter({ points, xLabel, yLabel, logx, logy, selected, onSelect, onHover, height = 420 }: ScatterProps) {
  const wrap = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<ScatterPoint | null>(null);
  const W = 640;
  const H = height;
  const M = { l: 64, r: 16, t: 14, b: 44 };
  const lx = logx && points.every((p) => p.x > 0);
  const ly = logy && points.every((p) => p.y > 0);
  const geom = useMemo(() => {
    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    let x0 = Math.min(...xs);
    let x1 = Math.max(...xs);
    let y0 = Math.min(...ys);
    let y1 = Math.max(...ys);
    if (x0 === x1) [x0, x1] = [x0 - 1, x1 + 1];
    if (y0 === y1) [y0, y1] = [y0 - 1, y1 + 1];
    const fx = lx ? Math.log10 : (v: number) => v;
    const fy = ly ? Math.log10 : (v: number) => v;
    const padX = (fx(x1) - fx(x0)) * 0.04;
    const padY = (fy(y1) - fy(y0)) * 0.06;
    const X = (v: number) => M.l + ((fx(v) - fx(x0) + padX) / (fx(x1) - fx(x0) + 2 * padX)) * (W - M.l - M.r);
    const Y = (v: number) => H - M.b - ((fy(v) - fy(y0) + padY) / (fy(y1) - fy(y0) + 2 * padY)) * (H - M.t - M.b);
    const xt = lx ? logTicks(x0, x1) : niceTicks(x0, x1);
    const yt = ly ? logTicks(y0, y1) : niceTicks(y0, y1, 5);
    // label the most extreme points (by distance from the centroid in screen space) + the selected one
    const cx = points.reduce((s, p) => s + X(p.x), 0) / Math.max(1, points.length);
    const cy = points.reduce((s, p) => s + Y(p.y), 0) / Math.max(1, points.length);
    const ranked = [...points].sort((a, b) => Math.hypot(X(b.x) - cx, Y(b.y) - cy) - Math.hypot(X(a.x) - cx, Y(a.y) - cy));
    const labelled = new Set((points.length <= 20 ? ranked : ranked.slice(0, Math.min(12, Math.ceil(points.length / 8)))).map((p) => p.iso3));
    return { X, Y, xt, yt, labelled };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, lx, ly, H]);
  if (points.length === 0) return null;
  const { X, Y, xt, yt, labelled } = geom;
  const tipPos = hover && wrap.current ? { left: `${(X(hover.x) / W) * 100}%`, top: `${(Y(hover.y) / H) * 100}%` } : null;
  return (
    <div className="v8m-scatter" ref={wrap} data-testid="scatter">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Nuage de points : ${yLabel} en fonction de ${xLabel}, ${points.length} pays`} preserveAspectRatio="xMidYMid meet">
        <g className="v8m-sc-grid">
          {xt.map((t) => (
            <g key={`x${t}`}>
              <line x1={X(t)} x2={X(t)} y1={M.t} y2={H - M.b} />
              <text x={X(t)} y={H - M.b + 16} textAnchor="middle">
                {frCompact(t)}
              </text>
            </g>
          ))}
          {yt.map((t) => (
            <g key={`y${t}`}>
              <line x1={M.l} x2={W - M.r} y1={Y(t)} y2={Y(t)} />
              <text x={M.l - 6} y={Y(t) + 4} textAnchor="end">
                {frCompact(t)}
              </text>
            </g>
          ))}
        </g>
        <text className="v8m-sc-axis" x={(M.l + W - M.r) / 2} y={H - 6} textAnchor="middle">
          {xLabel}
          {lx ? ' (log)' : ''}
        </text>
        <text className="v8m-sc-axis" transform={`translate(14 ${(M.t + H - M.b) / 2}) rotate(-90)`} textAnchor="middle">
          {yLabel}
          {ly ? ' (log)' : ''}
        </text>
        <g>
          {points.map((p) => {
            const sel = p.iso3 === selected;
            return (
              <g
                key={p.iso3}
                className={`v8m-dot${sel ? ' is-sel' : ''}${p.proj ? ' is-proj' : ''}`}
                transform={`translate(${X(p.x).toFixed(1)} ${Y(p.y).toFixed(1)})`}
                onMouseEnter={() => {
                  setHover(p);
                  onHover?.(p.iso3);
                }}
                onMouseLeave={() => {
                  setHover(null);
                  onHover?.(null);
                }}
                onClick={() => onSelect(p.iso3)}
                data-iso3={p.iso3}
              >
                <circle r={12} className="v8m-dot-hit" />
                <circle r={sel ? 6 : 4.5} className="v8m-dot-mark" />
                {(labelled.has(p.iso3) || sel) && (
                  <text x={7} y={-6} className="v8m-dot-label">
                    {p.iso3}
                  </text>
                )}
              </g>
            );
          })}
        </g>
      </svg>
      {hover && tipPos && (
        <div className="v8m-tip v8m-sc-tip" style={tipPos} role="tooltip">
          <strong>{hover.name}</strong>
          <span>
            {xLabel} : <b className="v8-num">{hover.xText}</b>
          </span>
          <span>
            {yLabel} : <b className="v8-num">{hover.yText}</b>
          </span>
          {hover.proj && <span className="v8m-proj-pill">contient une projection</span>}
        </div>
      )}
    </div>
  );
}

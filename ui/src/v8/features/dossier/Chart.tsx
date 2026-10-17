import { useMemo, useState, type MouseEvent } from 'react';
import type { ChartBlock, Footnote } from './api';
import { VALUE_KIND_LABEL } from './api';
import { FnRef } from './Footnote';

const W = 680;
const H = 240;
const CAT = ['var(--v8-cat-1, #3653d3)', 'var(--v8-cat-2, #d9711c)', 'var(--v8-cat-3, #138a72)', 'var(--v8-cat-4, #c43d7a)', 'var(--v8-cat-5, #6b7a1a)', 'var(--v8-cat-6, #7c3fc4)'];

export function frTick(v: number): string {
  const a = Math.abs(v);
  const digits = a >= 100 || Number.isInteger(v) ? 0 : a >= 1 ? 1 : 2;
  return v.toLocaleString('fr-FR', { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function niceScale(lo: number, hi: number, ticks = 5): { bot: number; top: number; step: number } {
  if (!(hi > lo)) hi = lo + 1;
  const raw = (hi - lo) / ticks;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => (hi - lo) / s <= ticks) ?? 10 * mag;
  return { bot: Math.floor(lo / step) * step, top: Math.ceil(hi / step) * step, step };
}

/** Inline chart of a dossier block: lines (time), bars (categories), fan (line + published bounds). */
export function DossierChart({ b, notes }: { b: ChartBlock; notes: Map<number, Footnote> }) {
  const [hover, setHover] = useState<number | null>(null);
  const cat = b.x_kind === 'category' || b.chart === 'bar';
  const padL = 52;
  const padR = 12;
  const padT = 10;
  const padB = cat ? 58 : 24;
  const model = useMemo(() => {
    const xs: string[] = [];
    for (const s of b.series) for (const p of s.points) if (!xs.includes(p.x)) xs.push(p.x);
    if (!cat) xs.sort();
    const ys = b.series.flatMap((s) => s.points.flatMap((p) => [p.y, p.lo, p.hi])).filter((v): v is number => v !== null && Number.isFinite(v));
    const lo = Math.min(0, ...ys);
    const hi = Math.max(...ys, lo + 1);
    return { xs, ...niceScale(lo, hi) };
  }, [b, cat]);
  const { xs, bot, top, step } = model;
  const n = Math.max(1, xs.length);
  const iw = W - padL - padR;
  const ih = H - padT - padB;
  const X = (i: number) => padL + (cat ? (iw * (i + 0.5)) / n : n === 1 ? iw / 2 : (iw * i) / (n - 1));
  const Y = (v: number) => padT + ih * (1 - (v - bot) / (top - bot));
  const idx = new Map(xs.map((x, i) => [x, i]));
  const ticks: number[] = [];
  for (let t = bot; t <= top + step / 1e6; t += step) ticks.push(t);
  const every = Math.max(1, Math.ceil(n / (cat ? 12 : 8)));

  const onMove = (e: MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    let bd = Infinity;
    for (let i = 0; i < n; i++) {
      const d = Math.abs(X(i) - x);
      if (d < bd) {
        bd = d;
        best = i;
      }
    }
    setHover(best);
  };
  const hx = hover !== null ? xs[hover] : null;

  return (
    <figure className="dos-chart" data-testid="dos-chart">
      <div className="dos-chart-box">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${b.title} (${b.unit})`} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
          <defs>
            <pattern id={`hatch-${b.id}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <line x1="0" y1="0" x2="0" y2="6" style={{ stroke: 'var(--v8-missing-stripe, rgba(122,90,28,.32))' }} strokeWidth="1.5" />
            </pattern>
          </defs>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={padL} x2={W - padR} y1={Y(t)} y2={Y(t)} className="dos-grid" />
              <text x={padL - 6} y={Y(t) + 3.5} textAnchor="end" className="dos-tick">
                {frTick(t)}
              </text>
            </g>
          ))}
          {xs.map((x, i) =>
            i % every === 0 || i === n - 1 ? (
              <text
                key={x}
                x={X(i)}
                y={H - padB + 14}
                textAnchor={cat ? 'end' : 'middle'}
                transform={cat ? `rotate(-32 ${X(i)} ${H - padB + 14})` : undefined}
                className="dos-tick"
              >
                {x.length > 18 ? `${x.slice(0, 17)}…` : x}
              </text>
            ) : null,
          )}
          {b.series.map((s, si) => {
            const color = CAT[si % CAT.length];
            const dashed = s.kind === 'projection' || s.kind === 'forecast' || s.kind === 'scenario';
            if (cat) {
              const bw = (iw / n) * 0.66;
              return (
                <g key={s.label}>
                  {s.points.map((p) => {
                    const i = idx.get(p.x) ?? 0;
                    if (p.y === null)
                      return <rect key={p.x} x={X(i) - bw / 2} y={padT} width={bw} height={ih} fill={`url(#hatch-${b.id})`} aria-label={`${p.x} : manquant`} />;
                    return <rect key={p.x} x={X(i) - bw / 2} y={Y(Math.max(p.y, 0))} width={bw} height={Math.abs(Y(p.y) - Y(0))} style={{ fill: color }} opacity={hover === i ? 1 : 0.82} rx={1.5} />;
                  })}
                </g>
              );
            }
            const band = s.points.filter((p) => p.lo !== null && p.hi !== null && p.y !== null);
            const segs: string[][] = [[]];
            for (const p of [...s.points].sort((a, c) => (idx.get(a.x) ?? 0) - (idx.get(c.x) ?? 0))) {
              if (p.y === null) segs.push([]);
              else segs[segs.length - 1]!.push(`${X(idx.get(p.x) ?? 0).toFixed(1)},${Y(p.y).toFixed(1)}`);
            }
            return (
              <g key={s.label}>
                {band.length > 1 && (
                  <polygon
                    points={[...band.map((p) => `${X(idx.get(p.x) ?? 0)},${Y(p.hi!)}`), ...[...band].reverse().map((p) => `${X(idx.get(p.x) ?? 0)},${Y(p.lo!)}`)].join(' ')}
                    style={{ fill: color }}
                    opacity={0.14}
                  />
                )}
                {segs.map((seg, k) =>
                  seg.length > 1 ? (
                    <polyline key={k} points={seg.join(' ')} fill="none" style={{ stroke: color }} strokeWidth={1.8} strokeDasharray={dashed ? '5 4' : undefined} />
                  ) : seg.length === 1 ? (
                    <circle key={k} cx={seg[0]!.split(',')[0]} cy={seg[0]!.split(',')[1]} r={2.5} style={{ fill: color }} />
                  ) : null,
                )}
              </g>
            );
          })}
          {hover !== null && !cat && <line x1={X(hover)} x2={X(hover)} y1={padT} y2={padT + ih} className="dos-cross" />}
        </svg>
        {hx !== null && (
          <div className="dos-chart-tip" style={{ left: `${(X(hover!) / W) * 100}%` }} role="status">
            <b>{hx}</b>
            {b.series.map((s, si) => {
              const p = s.points.find((q) => q.x === hx);
              return (
                <span key={s.label}>
                  <i style={{ background: CAT[si % CAT.length] }} /> {s.label} :{' '}
                  {p && p.y !== null ? (
                    <>
                      {p.exact ?? frTick(p.y)} {b.unit}
                      {p.lo !== null && p.hi !== null ? ` (${frTick(p.lo)} – ${frTick(p.hi)})` : ''}
                    </>
                  ) : (
                    <em className="dos-miss-inline">manquant</em>
                  )}
                </span>
              );
            })}
          </div>
        )}
      </div>
      <figcaption>
        <b>{b.title}</b> <span className="dos-muted">({b.unit})</span>
        <span className="dos-legend">
          {b.series.map((s, si) => (
            <span key={s.label} className="dos-legend-item">
              <i style={{ background: CAT[si % CAT.length] }} className={s.kind === 'projection' || s.kind === 'scenario' || s.kind === 'forecast' ? 'dos-dashed' : undefined} />
              {s.label}
              {s.kind !== 'observed' && s.kind !== 'label' && <span className={`dos-kind dos-kind-${s.kind}`}>{VALUE_KIND_LABEL[s.kind]}</span>}
              <FnRef n={s.fn} f={notes.get(s.fn)} />
            </span>
          ))}
        </span>
        {b.note && <span className="dos-muted"> — {b.note}</span>}
      </figcaption>
    </figure>
  );
}

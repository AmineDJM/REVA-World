import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import { displayDecimal } from '../lib/format';
import { formatNumber } from '../lib/locale';
import type { RangePoint, SeriesPoint } from '../lib/runviews';

export interface PeriodChartProps {
  l1: SeriesPoint[];
  range?: RangePoint[];
  /** Accessible summary; a table with the same values must follow the chart. */
  label: string;
  unitRef?: string | null;
  height?: number;
}

const M = { top: 12, right: 14, bottom: 26, left: 64 };

function niceTicks(lo: number, hi: number, n = 4): number[] {
  if (!(hi > lo)) return [lo];
  const step0 = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) ?? step0;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

function compactTick(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `${formatNumber(v / 1e9, { maximumFractionDigits: 2 })}B`;
  if (a >= 1e6) return `${formatNumber(v / 1e6, { maximumFractionDigits: 2 })}M`;
  if (a >= 1e4) return `${formatNumber(v / 1e3, { maximumFractionDigits: 1 })}k`;
  return formatNumber(v, { maximumFractionDigits: 4 });
}

/**
 * Multi-period line chart: the L1 path (2 px line), the L2 assumption-sensitivity ribbon when
 * present (never drawn as a confidence band: it is a min/max over assumption variants), a
 * crosshair readout on hover and with the arrow keys, and gaps for missing years.
 */
export function PeriodChart({ l1, range = [], label, unitRef, height = 220 }: PeriodChartProps) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [active, setActive] = useState<number | null>(null);
  const descId = useId();

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const measure = () => setWidth(Math.max(260, Math.round(el.getBoundingClientRect().width) || 640));
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const years = useMemo(() => [...new Set([...l1.map((p) => p.year), ...range.map((r) => r.year)])].sort((a, b) => a - b), [l1, range]);
  const geo = useMemo(() => {
    const vals: number[] = [];
    for (const p of l1) if (p.value !== null) vals.push(Number(p.value));
    for (const r of range) vals.push(Number(r.lower), Number(r.upper));
    const finite = vals.filter(Number.isFinite);
    let lo = finite.length ? Math.min(...finite) : 0;
    let hi = finite.length ? Math.max(...finite) : 1;
    if (lo === hi) {
      lo -= Math.abs(lo) * 0.05 || 1;
      hi += Math.abs(hi) * 0.05 || 1;
    }
    const pad = (hi - lo) * 0.06;
    lo -= pad;
    hi += pad;
    const x0 = years[0] ?? 0;
    const x1 = years[years.length - 1] ?? 1;
    const iw = width - M.left - M.right;
    const ih = height - M.top - M.bottom;
    const x = (yr: number) => M.left + (x1 === x0 ? iw / 2 : ((yr - x0) / (x1 - x0)) * iw);
    const y = (v: number) => M.top + ih - ((v - lo) / (hi - lo)) * ih;
    return { lo, hi, x, y, iw, ih };
  }, [l1, range, years, width, height]);

  // L1 path segments, broken at missing years.
  const segments = useMemo(() => {
    const segs: string[] = [];
    let cur: string[] = [];
    for (const p of l1) {
      if (p.value === null || !Number.isFinite(Number(p.value))) {
        if (cur.length) segs.push(cur.join(' '));
        cur = [];
        continue;
      }
      cur.push(`${cur.length ? 'L' : 'M'}${geo.x(p.year).toFixed(1)},${geo.y(Number(p.value)).toFixed(1)}`);
    }
    if (cur.length) segs.push(cur.join(' '));
    return segs;
  }, [l1, geo]);

  const ribbon = useMemo(() => {
    if (range.length < 1) return null;
    const up = range.map((r) => `${geo.x(r.year).toFixed(1)},${geo.y(Number(r.upper)).toFixed(1)}`);
    const dn = [...range].reverse().map((r) => `${geo.x(r.year).toFixed(1)},${geo.y(Number(r.lower)).toFixed(1)}`);
    return `M${up.join(' L')} L${dn.join(' L')} Z`;
  }, [range, geo]);

  const yTicks = niceTicks(geo.lo, geo.hi);
  const step = Math.max(1, Math.ceil(years.length / Math.max(2, Math.floor(geo.iw / 56))));
  const xTicks = years.filter((_, i) => i % step === 0);
  const activeYear: number | null = active !== null ? (years[active] ?? null) : null;
  const ap = activeYear !== null ? l1.find((p) => p.year === activeYear) : undefined;
  const ar = activeYear !== null ? range.find((r) => r.year === activeYear) : undefined;
  const showMarkers = l1.length <= 30;

  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * width;
    let best = 0;
    let bd = Infinity;
    years.forEach((yr, i) => {
      const d = Math.abs(geo.x(yr) - px);
      if (d < bd) {
        bd = d;
        best = i;
      }
    });
    setActive(best);
  };
  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    if (!years.length) return;
    if (e.key === 'ArrowRight') setActive((a) => Math.min(years.length - 1, (a ?? -1) + 1));
    else if (e.key === 'ArrowLeft') setActive((a) => Math.max(0, (a ?? years.length) - 1));
    else if (e.key === 'Home') setActive(0);
    else if (e.key === 'End') setActive(years.length - 1);
    else if (e.key === 'Escape') setActive(null);
    else return;
    e.preventDefault();
  };
  const fmt = (v: string) => displayDecimal(v, unitRef).text;

  if (years.length === 0) return <p className="muted">No dated values to chart.</p>;
  return (
    <div className="period-chart" ref={wrap}>
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={label}
        aria-describedby={descId}
        tabIndex={0}
        onPointerMove={onMove}
        onPointerLeave={() => setActive(null)}
        onKeyDown={onKey}
        onBlur={() => setActive(null)}
        data-testid="period-chart"
      >
        {yTicks.map((t) => (
          <g key={`y${t}`}>
            <line x1={M.left} x2={width - M.right} y1={geo.y(t)} y2={geo.y(t)} className="grid-line" />
            <text x={M.left - 6} y={geo.y(t)} dy="0.32em" textAnchor="end" className="tick">
              {compactTick(t)}
            </text>
          </g>
        ))}
        {xTicks.map((yr) => (
          <text key={`x${yr}`} x={geo.x(yr)} y={height - 8} textAnchor="middle" className="tick">
            {yr}
          </text>
        ))}
        <line x1={M.left} x2={width - M.right} y1={M.top + geo.ih} y2={M.top + geo.ih} className="axis-line" />
        {ribbon && <path d={ribbon} className="ribbon" data-testid="sensitivity-ribbon" />}
        {segments.map((d, i) => (
          <path key={i} d={d} className="l1-line" />
        ))}
        {showMarkers &&
          l1
            .filter((p) => p.value !== null)
            .map((p) => <circle key={p.year} cx={geo.x(p.year)} cy={geo.y(Number(p.value))} r={4} className="l1-marker" />)}
        {l1
          .filter((p) => p.value === null)
          .map((p) => (
            <g key={`m${p.year}`} className="missing-mark">
              <line x1={geo.x(p.year)} x2={geo.x(p.year)} y1={M.top} y2={M.top + geo.ih} />
            </g>
          ))}
        {activeYear !== null && (
          <line x1={geo.x(activeYear)} x2={geo.x(activeYear)} y1={M.top} y2={M.top + geo.ih} className="crosshair" />
        )}
      </svg>
      <p id={descId} className="chart-readout" aria-live="polite" data-testid="chart-readout">
        {activeYear === null ? (
          <span className="muted">Hover or focus the chart and use ← → to read each year.</span>
        ) : (
          <>
            <strong>{activeYear}</strong>
            {ap?.period && (
              <span className="muted">
                {' '}
                [{ap.period.start}, {ap.period.end})
              </span>
            )}
            {': '}
            {ap ? ap.value === null ? <em>missing ({ap.missingReason ?? 'reason not stated'})</em> : <span className="num">{fmt(ap.value)}</span> : '—'}
            {ar && (
              <>
                {' '}
                · sensitivity range{' '}
                <span className="num">
                  {fmt(ar.lower)} to {fmt(ar.upper)}
                </span>{' '}
                <span className="muted">(not a confidence interval)</span>
              </>
            )}
          </>
        )}
      </p>
    </div>
  );
}

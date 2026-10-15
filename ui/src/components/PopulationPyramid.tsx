import { useEffect, useRef, useState } from 'react';
import { displayDecimal } from '../lib/format';
import { formatNumber } from '../lib/locale';
import type { Pyramid } from '../lib/runviews';

export interface PopulationPyramidProps {
  pyramid: Pyramid;
  /** Fixed scale across years (max persons in any band of any year), so bars are comparable. */
  maxValue: number;
  label: string;
}

const FEMALE = '#c2410c';
const MALE = '#2a78d6';

/**
 * Population pyramid for one year: males to the left, females to the right, one bar per
 * single age (or per broad group when single ages were not requested). The scale is fixed
 * across years by the caller so the slider never rescales the bars. Hover a bar for its
 * exact value; the table alternative is rendered by the caller.
 */
export function PopulationPyramid({ pyramid, maxValue, label }: PopulationPyramidProps) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(560);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const measure = () => setWidth(Math.max(260, Math.min(720, Math.round(el.getBoundingClientRect().width) || 560)));
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const bands = pyramid.bands;
  const single = pyramid.kind === 'single_age';
  const barH = single ? Math.max(2, Math.min(5, Math.floor(420 / Math.max(1, bands.length)))) : 34;
  const gap = single ? 0.5 : 6;
  const top = 18;
  const labelW = single ? 34 : 54;
  const height = top + bands.length * (barH + gap) + 24;
  const half = (width - labelW) / 2;
  const cx = half;
  const scale = (v: string | null) => (v === null || !(maxValue > 0) ? 0 : (Math.max(0, Number(v)) / maxValue) * (half - 6));
  // Oldest at the top.
  const ordered = [...bands].reverse();
  const tick = single ? 10 : 1;
  const hv = hover !== null ? ordered[hover] : undefined;
  const fmt = (v: string | null) => (v === null ? 'missing' : `${displayDecimal(v, 'unit.persons').text} persons`);

  return (
    <div className="pyramid" ref={wrap}>
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} data-testid="pyramid" onPointerLeave={() => setHover(null)}>
        <text x={cx - 8} y={12} textAnchor="end" className="tick">
          ← males
        </text>
        <text x={cx + labelW + 8} y={12} className="tick">
          females →
        </text>
        {ordered.map((b, i) => {
          const y = top + i * (barH + gap);
          const mw = scale(b.male);
          const fw = scale(b.female);
          return (
            <g key={b.label} onPointerEnter={() => setHover(i)} className={hover === i ? 'pyr-row active' : 'pyr-row'}>
              <rect x={0} y={y} width={width} height={barH + gap} fill="transparent" />
              {b.male === null ? (
                <rect x={cx - 24} y={y} width={22} height={barH} className="pyr-missing" />
              ) : (
                <rect x={cx - mw} y={y} width={mw} height={barH} fill={MALE} rx={single ? 0 : 3} />
              )}
              {b.female === null ? (
                <rect x={cx + labelW + 2} y={y} width={22} height={barH} className="pyr-missing" />
              ) : (
                <rect x={cx + labelW} y={y} width={fw} height={barH} fill={FEMALE} rx={single ? 0 : 3} />
              )}
              {(!single || b.lo % tick === 0) && (
                <text x={cx + labelW / 2} y={y + barH / 2} dy="0.32em" textAnchor="middle" className="tick">
                  {b.label}
                </text>
              )}
            </g>
          );
        })}
        <text x={cx} y={height - 6} textAnchor="end" className="tick">
          {formatNumber(maxValue, { notation: 'compact', maximumFractionDigits: 1 })}
        </text>
        <text x={cx + labelW} y={height - 6} className="tick">
          scale fixed across years
        </text>
      </svg>
      <p className="chart-readout" aria-live="polite">
        {hv ? (
          <>
            <strong>Age {hv.label}</strong>, {pyramid.year}: males {fmt(hv.male)} · females {fmt(hv.female)}
          </>
        ) : (
          <span className="muted">
            <span className="swatch" style={{ background: MALE }} aria-hidden="true" /> males{' '}
            <span className="swatch" style={{ background: FEMALE }} aria-hidden="true" /> females (recorded sex, 1 January). Hover a bar for exact
            values.
          </span>
        )}
      </p>
    </div>
  );
}

import { useEffect } from 'react';
import type { PeriodInfo } from './api';

const PREC_FR: Record<string, string> = { year: 'Année', semester: 'Semestre', quarter: 'Trimestre', month: 'Mois', day: 'Jour' };

export interface PeriodBarProps {
  periods: PeriodInfo[];
  period: string | null;
  precision: string;
  precisions: Record<string, number>;
  playing: boolean;
  /** True once the slice of the current period is drawn (play waits for it). */
  settled: boolean;
  onPeriod: (code: string) => void;
  onPrecision: (p: string) => void;
  onPlay: (on: boolean) => void;
}

/**
 * Period slider at the series' own precision (year / semester / quarter / month / day), with play. Periods that
 * hold only projections are marked (violet ticks) and named "projection" — never silently mixed in.
 * Keys (outside inputs): ← → previous / next period, Espace play/pause.
 */
export function PeriodBar({ periods, period, precision, precisions, playing, settled, onPeriod, onPrecision, onPlay }: PeriodBarProps) {
  const idx = Math.max(0, periods.findIndex((p) => p.code === period));
  const cur = periods[idx];

  useEffect(() => {
    if (!playing || !settled) return;
    if (idx >= periods.length - 1) {
      onPlay(false);
      return;
    }
    const t = setTimeout(() => onPeriod(periods[idx + 1]!.code), 650);
    return () => clearTimeout(t);
  }, [playing, settled, idx, periods, onPeriod, onPlay]);

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === 'ArrowLeft' && idx > 0) onPeriod(periods[idx - 1]!.code);
      else if (e.key === 'ArrowRight' && idx < periods.length - 1) onPeriod(periods[idx + 1]!.code);
      else if (e.key === ' ' && t?.tagName !== 'BUTTON') {
        e.preventDefault();
        onPlay(!playing);
      } else return;
    };
    window.addEventListener('keydown', on);
    return () => window.removeEventListener('keydown', on);
  }, [idx, periods, playing, onPeriod, onPlay]);

  if (periods.length === 0) return null;
  const precs = Object.keys(precisions).filter((p) => p in PREC_FR);
  const projOnly = cur ? cur.obs === 0 && cur.proj > 0 : false;
  return (
    <div className="v8m-period" data-testid="period-bar">
      <button
        type="button"
        className="v8-btn v8-btn-sm"
        onClick={() => {
          if (!playing && idx >= periods.length - 1) onPeriod(periods[0]!.code);
          onPlay(!playing);
        }}
        aria-pressed={playing}
        disabled={periods.length < 2}
        title="Lecture / pause (Espace)"
      >
        {playing ? '❚❚ Pause' : '▶ Lecture'}
      </button>
      <div className="v8m-slider">
        <input
          type="range"
          min={0}
          max={periods.length - 1}
          value={idx}
          onChange={(e) => {
            onPlay(false);
            onPeriod(periods[Number(e.target.value)]!.code);
          }}
          aria-label="Période"
          aria-valuetext={cur?.label}
        />
        <div className="v8m-ticks" aria-hidden="true">
          {periods.map((p, i) =>
            p.obs === 0 && p.proj > 0 ? <span key={p.code} className="v8m-tick-proj" style={{ left: `${(i / Math.max(1, periods.length - 1)) * 100}%` }} /> : null,
          )}
        </div>
      </div>
      <output className="v8m-period-out v8-num" aria-live="polite">
        {cur?.label}
        {projOnly && <span className="v8m-proj-pill">projection</span>}
      </output>
      <span className="v8m-period-range v8-muted">
        {periods[0]!.label} – {periods[periods.length - 1]!.label} · {periods.length} périodes
      </span>
      {precs.length > 1 && (
        <label className="v8m-inline">
          <span className="v8-sr-only">Précision</span>
          <select className="v8-input" value={precision} onChange={(e) => onPrecision(e.target.value)}>
            {precs.map((p) => (
              <option key={p} value={p}>
                {PREC_FR[p]}
              </option>
            ))}
          </select>
        </label>
      )}
    </div>
  );
}

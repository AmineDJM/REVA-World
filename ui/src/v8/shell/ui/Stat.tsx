import type { ReactNode } from 'react';
import { Badge, type HonestyKind } from './Badge';
import { displayValue } from './format';
import { MissingValue } from './MissingValue';
import { SourceChip, type SourceInfo } from './SourceChip';

export interface StatProps {
  label: ReactNode;
  /** Exact decimal string (atlas) or number; null = missing (then `missing` gives the reason). */
  value: string | number | null;
  unit?: string | null;
  /** Period of the value ("2024", "2025-T2", "au 2026-10-01"). */
  period?: string | null;
  source?: SourceInfo | null;
  /** Honesty kind; "observed" is not badged unless `showObserved`. */
  kind?: HonestyKind;
  showObserved?: boolean;
  missing?: { reason?: string | null; note?: string } | null;
  /** Small text under the number (delta, rank, comparison). */
  sub?: ReactNode;
  maxFractionDigits?: number;
  format?: (v: string | number) => string;
  size?: 'sm' | 'md' | 'lg';
  href?: string;
}

/** A big number with unit, period, honesty badge and its source chip. Missing → hatched with reason. */
export function Stat({ label, value, unit, period, source, kind = 'observed', showObserved = false, missing, sub, maxFractionDigits = 3, format, size = 'md' }: StatProps) {
  const isMissing = value === null || value === undefined;
  return (
    <div className={`v8-stat v8-stat-${size}`} data-kind={isMissing ? 'missing' : kind}>
      <div className="v8-stat-label">{label}</div>
      <div className="v8-stat-value">
        {isMissing ? (
          <MissingValue reason={missing?.reason} note={missing?.note} />
        ) : (
          <>
            <span className={`v8-num v8-stat-num${kind === 'projection' || kind === 'scenario' ? ' v8-stat-num-proj' : ''}`}>{format ? format(value) : displayValue(value, maxFractionDigits)}</span>
            {unit && <span className="v8-stat-unit">{unit}</span>}
          </>
        )}
      </div>
      <div className="v8-stat-meta">
        {period && <span className="v8-stat-period">{period}</span>}
        {!isMissing && (kind !== 'observed' || showObserved) && <Badge kind={kind} size="xs" />}
        {source && <SourceChip {...source} />}
      </div>
      {sub && <div className="v8-stat-sub">{sub}</div>}
    </div>
  );
}

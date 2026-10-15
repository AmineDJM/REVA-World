import { useState } from 'react';
import type { DecimalValue, FloatValue, MeasurementBasis, SensitivityRangeValue, TypedValue } from '../api/types';
import { basisLabel, compactMagnitude, displayDecimal, unitLabel } from '../lib/format';
import { formatNumber } from '../lib/locale';
import { MissingValue } from './MissingValue';
import { UncertaintyGlyph } from './UncertaintyGlyph';

export interface ValueViewProps {
  value: TypedValue | null | undefined;
  /** Show the valuation/price basis after the unit. */
  showBasis?: boolean;
  /** Show a compact magnitude ("68.2 billion") after the value. */
  compact?: boolean;
}

/**
 * A toggle revealing the exact decimal string(s) behind a value rounded for display. The exact
 * string is also in the element's title (hover) and is what exports carry.
 */
export function ExactToggle({ exact, unitRef }: { exact: string; unitRef?: string | null }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      {' '}
      <button
        type="button"
        className="exact-toggle"
        aria-expanded={open}
        aria-label={open ? 'Hide exact value' : 'Show exact value'}
        title={`exact: ${exact}${unitRef ? ` ${unitRef}` : ''}`}
        onClick={() => setOpen((o) => !o)}
      >
        {open ? 'hide exact' : 'exact'}
      </button>
      {open && (
        <span className="exact-value mono" data-testid="exact-value">
          {' '}
          = {exact}
        </span>
      )}
    </>
  );
}

/**
 * Renders a typed REVA value. Missing or absent values render via MissingValue, never as 0.
 * Decimals are rounded for display per unit (US$ to the cent, percentages to 4 decimals); the
 * exact served string stays available on hover and via the "exact" toggle.
 */
export function ValueView({ value, showBasis = false, compact = false }: ValueViewProps) {
  if (value === null || value === undefined) return <MissingValue reason="not_provided_by_api" />;
  switch (value.kind) {
    case 'missing':
      return <MissingValue reason={String(value.reason)} note={typeof value.detail === 'string' ? value.detail : undefined} />;
    case 'decimal': {
      const v = value as DecimalValue;
      const unit = unitLabel(v.unit_ref);
      const basis = showBasis ? basisLabel(v.basis as MeasurementBasis | undefined) : '';
      const mag = compact ? compactMagnitude(v.value) : null;
      const d = displayDecimal(v.value, v.unit_ref);
      return (
        <span className="value" title={`exact: ${v.value} ${v.unit_ref}`} data-exact={v.value}>
          <span className="num">{d.text}</span>
          {unit && <span className="unit"> {unit}</span>}
          {mag && <span className="magnitude"> ({mag}{unit ? ` ${unit}` : ''})</span>}
          {basis && <span className="basis"> · {basis}</span>}
          {d.rounded && <ExactToggle exact={d.exact} unitRef={v.unit_ref} />}
        </span>
      );
    }
    case 'float': {
      const v = value as FloatValue;
      const unit = unitLabel(v.unit_ref);
      return (
        <span className="value" title={`${v.value} ${v.unit_ref}`}>
          <span className="num">{formatNumber(v.value, { maximumSignificantDigits: 15 })}</span>
          {unit && <span className="unit"> {unit}</span>}
        </span>
      );
    }
    case 'sensitivity_range': {
      // A range over assumed elasticities: rendered as a range, never as a point estimate.
      const v = value as SensitivityRangeValue;
      const unit = unitLabel(v.unit_ref);
      const lo = displayDecimal(v.lower, v.unit_ref);
      const hi = displayDecimal(v.upper, v.unit_ref);
      return (
        <span className="value sensitivity-range" title={v.statement} data-exact-lower={v.lower} data-exact-upper={v.upper}>
          <UncertaintyGlyph kind="sensitivity" className="uglyph-inline" />
          <span className="num">{lo.text}</span> to <span className="num">{hi.text}</span>
          {unit && <span className="unit"> {unit}</span>}
          <span className="basis"> · assumption sensitivity range (not a confidence interval)</span>
          {(lo.rounded || hi.rounded) && <ExactToggle exact={`${v.lower} to ${v.upper}`} unitRef={v.unit_ref} />}
        </span>
      );
    }
    case 'category':
      return <span className="value category">{String(value.code ?? '')}</span>;
    case 'text':
      return <span className="value text">{String(value.text ?? 'text artifact')}</span>;
    default:
      return <span className="value">{value.kind} value</span>;
  }
}

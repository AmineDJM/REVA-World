import type { DecimalValue, FloatValue, MeasurementBasis, SensitivityRangeValue, TypedValue } from '../api/types';
import { basisLabel, compactMagnitude, formatDecimalString, unitLabel } from '../lib/format';
import { MissingValue } from './MissingValue';

export interface ValueViewProps {
  value: TypedValue | null | undefined;
  /** Show the valuation/price basis after the unit. */
  showBasis?: boolean;
  /** Show a compact magnitude ("68.2 billion") after the exact value. */
  compact?: boolean;
}

/** Renders a typed REVA value. Missing or absent values render via MissingValue, never as 0. */
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
      return (
        <span className="value" title={`${v.value} ${v.unit_ref}`}>
          <span className="num">{formatDecimalString(v.value)}</span>
          {unit && <span className="unit"> {unit}</span>}
          {mag && <span className="magnitude"> ({mag})</span>}
          {basis && <span className="basis"> · {basis}</span>}
        </span>
      );
    }
    case 'float': {
      const v = value as FloatValue;
      const unit = unitLabel(v.unit_ref);
      return (
        <span className="value" title={`${v.value} ${v.unit_ref}`}>
          <span className="num">{v.value.toLocaleString('en-US', { maximumSignificantDigits: 15 })}</span>
          {unit && <span className="unit"> {unit}</span>}
        </span>
      );
    }
    case 'sensitivity_range': {
      // A range over assumed elasticities: rendered as a range, never as a point estimate.
      const v = value as SensitivityRangeValue;
      const unit = unitLabel(v.unit_ref);
      return (
        <span className="value sensitivity-range" title={v.statement}>
          <span className="num">{formatDecimalString(v.lower)}</span> to <span className="num">{formatDecimalString(v.upper)}</span>
          {unit && <span className="unit"> {unit}</span>}
          <span className="basis"> · assumption sensitivity range (not a confidence interval)</span>
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

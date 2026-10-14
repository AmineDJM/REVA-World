import type { Uncertainty, UncertaintySummary } from '../api/types';
import { formatDecimalString } from '../lib/format';

type AnyUncertainty = Omit<Partial<Uncertainty>, 'status'> &
  Omit<Partial<UncertaintySummary>, 'status'> & { status?: Uncertainty['status'] | UncertaintySummary['status'] | null };

export interface UncertaintyLabel {
  /** Short heading, e.g. "Sensitivity range (not a confidence interval)". */
  title: string;
  /** CSS modifier selecting the visual treatment. */
  treatment: 'none' | 'definition' | 'sensitivity' | 'prediction' | 'confidence' | 'credible' | 'bounds' | 'distribution' | 'mixed';
  /** Whether the range carries a probabilistic coverage claim. */
  probabilistic: boolean;
  note?: string;
}

function pct(level: number | null | undefined): string | null {
  if (level === null || level === undefined || !Number.isFinite(level)) return null;
  return `${Math.round(level * 1000) / 10}%`;
}

/** Map an uncertainty record to a precise label. Exported for tests and exports. */
export function describeUncertainty(u: AnyUncertainty | null | undefined): UncertaintyLabel {
  if (!u || !u.status) {
    return { title: 'Uncertainty not reported', treatment: 'none', probabilistic: false };
  }
  switch (u.status) {
    case 'unquantified':
      return {
        title: 'Uncertainty not quantified',
        treatment: 'none',
        probabilistic: false,
        note: 'No error model is available. This does not mean the value is exact.',
      };
    case 'exact_by_definition':
      return {
        title: 'Exact by definition',
        treatment: 'definition',
        probabilistic: false,
        note: 'Exact as a definition or transcription; not a claim of exact knowledge of reality.',
      };
    case 'mixed':
      return { title: 'Mixed uncertainty status across rows', treatment: 'mixed', probabilistic: false, note: 'See each row.' };
    case 'not_applicable':
      return { title: 'Uncertainty not applicable', treatment: 'none', probabilistic: false };
    case 'quantified':
      break;
    default:
      return { title: `Uncertainty: ${u.status}`, treatment: 'none', probabilistic: false };
  }
  const level = pct(u.level);
  switch (u.kind) {
    case 'sensitivity_range':
      return {
        title: 'Sensitivity range (not a confidence interval)',
        treatment: 'sensitivity',
        probabilistic: false,
        note: 'Spread of results across assumed parameter values. No probability is attached.',
      };
    case 'prediction_interval':
      return {
        title: `Prediction interval${level ? ` (${level} nominal)` : ''}`,
        treatment: 'prediction',
        probabilistic: true,
        note: 'Range expected to contain the future released value at the stated nominal coverage.',
      };
    case 'confidence_interval':
      return { title: `Confidence interval${level ? ` (${level} nominal)` : ''}`, treatment: 'confidence', probabilistic: true };
    case 'credible_interval':
      return { title: `Credible interval${level ? ` (${level})` : ''}`, treatment: 'credible', probabilistic: true };
    case 'bounds':
      return {
        title: 'Bounds (no probability attached)',
        treatment: 'bounds',
        probabilistic: false,
        note: 'Feasible range; values outside are excluded, values inside are not weighted.',
      };
    case 'distribution':
      return { title: 'Distribution', treatment: 'distribution', probabilistic: true };
    case 'probability':
      return { title: 'Probability', treatment: 'distribution', probabilistic: true };
    default:
      return { title: `Quantified (${u.kind ?? 'kind not stated'})`, treatment: 'distribution', probabilistic: false };
  }
}

export interface UncertaintyPanelProps {
  uncertainty: AnyUncertainty | null | undefined;
  /** Inline badge instead of a full panel. */
  compact?: boolean;
  unit?: string;
}

/**
 * Shows uncertainty status/kind/level with distinct visual treatments for a
 * prediction interval, a sensitivity range and bounds. An unquantified value is
 * labelled as such and never drawn as a zero-width interval.
 */
export function UncertaintyPanel({ uncertainty, compact = false, unit }: UncertaintyPanelProps) {
  const d = describeUncertainty(uncertainty);
  const u = uncertainty ?? undefined;
  const hasRange = d.treatment !== 'none' && d.treatment !== 'definition' && !!u?.lower && !!u?.upper;
  const range = hasRange ? `${formatDecimalString(String(u!.lower))} – ${formatDecimalString(String(u!.upper))}${unit ? ` ${unit}` : ''}` : null;

  if (compact) {
    return (
      <span className={`ubadge ubadge-${d.treatment}`} title={d.note ?? d.title}>
        <span className="ubadge-mark" aria-hidden="true" />
        {d.title}
        {range && <span className="ubadge-range">: {range}</span>}
      </span>
    );
  }

  return (
    <section className={`uncertainty-panel u-${d.treatment}`} aria-label="Uncertainty">
      <h4 className="u-title">
        <span className="ubadge-mark" aria-hidden="true" /> {d.title}
      </h4>
      {range && (
        <div className="u-range">
          <span className={`ubar ubar-${d.treatment}`} aria-hidden="true">
            <span className="ubar-lo" />
            <span className="ubar-fill" />
            <span className="ubar-hi" />
          </span>
          <span className="u-range-text">{range}</span>
        </div>
      )}
      {d.note && <p className="u-note">{d.note}</p>}
      <dl className="kv compact">
        {u?.description && (
          <div className="kv-row">
            <dt>Description</dt>
            <dd>{u.description}</dd>
          </div>
        )}
        {u?.method_version && (
          <div className="kv-row">
            <dt>Method</dt>
            <dd>{u.method_version}</dd>
          </div>
        )}
        {(u?.dependence || u?.dependence_group) && (
          <div className="kv-row">
            <dt>Dependence</dt>
            <dd>{u.dependence ?? u.dependence_group}</dd>
          </div>
        )}
        {u?.conditioning && u.conditioning.length > 0 && (
          <div className="kv-row">
            <dt>Conditional on</dt>
            <dd>{u.conditioning.join('; ')}</dd>
          </div>
        )}
        {u?.note && (
          <div className="kv-row">
            <dt>Note</dt>
            <dd>{u.note}</dd>
          </div>
        )}
      </dl>
    </section>
  );
}

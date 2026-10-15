/** Fixed honesty labels for V2 model outputs (docs/roadmap/v2_design.md, "Honesty rules specific to V2"). */

export const PROJECTION_LABEL = 'Conditional projection under stated assumptions, not a forecast';
export const EXPERIMENTAL_LABEL = 'Experimental model — maintainer review pending';
export const IO_LABEL = 'First-round cost-push, full pass-through; not an inflation forecast';
export const IO_L2_LABEL = 'First-round cost-push under a stated pass-through share; sensitivity over the pass-through grid; not an inflation forecast';
export const SENSITIVITY_LABEL = 'assumption sensitivity range (not a confidence interval)';

export function ProjectionLabel() {
  return (
    <p className="honesty-label honesty-projection" data-testid="projection-label">
      <strong>{PROJECTION_LABEL}.</strong> Each value follows from the stated assumptions; no probability is attached and the future is not
      predicted.
    </p>
  );
}

export function ExperimentalLabel({ reviewStatus }: { reviewStatus?: string | null }) {
  return (
    <p className="honesty-label honesty-experimental" data-testid="experimental-label">
      <strong>{EXPERIMENTAL_LABEL}.</strong> No maintainer has reviewed this model yet
      {reviewStatus && reviewStatus !== 'maintainer_review_pending' ? ` (review status: ${reviewStatus.replace(/_/g, ' ')})` : ''}; literature
      parameters enter as cited scenario assumptions, not reviewed evidence.
    </p>
  );
}

export function IoLabel({ l2 = false }: { l2?: boolean }) {
  return (
    <p className="honesty-label honesty-io" data-testid="io-label">
      <strong>{l2 ? IO_L2_LABEL : IO_LABEL}.</strong> Fixed technical coefficients, no substitution and no demand response; the household basket
      is valued at basic prices and is not a consumer price index.
    </p>
  );
}

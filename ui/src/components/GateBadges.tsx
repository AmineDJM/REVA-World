/**
 * Promotion-gate decisions and permission tiers (spec §18.2). A stored gate decision is one of
 * pass / fail / blocked / needs_review; needs_review is its own outcome (a human must decide),
 * not a pass and not a failure. Each decision has a text label and a glyph, so colour is never
 * the only cue.
 */

export type GateDecision = 'pass' | 'fail' | 'blocked' | 'needs_review';

const DECISIONS: Record<GateDecision, { label: string; glyph: string; note: string }> = {
  pass: { label: 'Pass', glyph: '✓', note: 'The gate passed for this proposal.' },
  fail: { label: 'Fail', glyph: '✗', note: 'The gate failed; the proposal cannot be promoted.' },
  blocked: { label: 'Blocked', glyph: '■', note: 'The gate could not run (missing input, credential or capability); nothing was promoted.' },
  needs_review: {
    label: 'Needs human review',
    glyph: '?',
    note: 'The gate did not decide automatically: a named human review is required before promotion.',
  },
};

export function GateDecisionBadge({ decision }: { decision: string }) {
  const d = (DECISIONS as Record<string, (typeof DECISIONS)[GateDecision]>)[decision];
  if (!d) {
    return (
      <span className="gate-badge gate-unknown" data-decision={decision} title="Decision value not recognised by this UI">
        Unrecognised decision: {decision}
      </span>
    );
  }
  return (
    <span className={`gate-badge gate-${decision}`} data-decision={decision} title={d.note}>
      <span aria-hidden="true" className="gate-glyph">
        {d.glyph}
      </span>{' '}
      {d.label}
    </span>
  );
}

export const TIER_TEXT: Record<number, string> = {
  0: 'T0 — read, discover and generate proposals; automatic within budget.',
  1: 'T1 — new series through an approved template, approved semantics/rights and known hosts. T1 within the signed policy scope promotes automatically without human review.',
  2: 'T2 — new parser code, model, ontology mapping or executable legal rule: isolated evaluation, then human review is required.',
  3: 'T3 — kernel, security, licences, sensitive data, causal claims, destructive migrations or paid resources: explicit human (maintainer) review is required.',
};

/** Short promotion rule shown wherever tiers appear. */
export const TIER_POLICY_TEXT = 'T1 within the signed policy scope promotes automatically without human review; T2/T3 require human review.';

export function TierBadge({ tier }: { tier: number | null | undefined }) {
  if (tier === null || tier === undefined || !Number.isInteger(tier) || !(tier in TIER_TEXT)) {
    return <span className="tier-badge tier-unknown">tier not stated</span>;
  }
  return (
    <span className={`tier-badge tier-${tier}`} title={TIER_TEXT[tier]} data-tier={`T${tier}`}>
      T{tier}
      {tier >= 2 ? ' · human review required' : tier === 1 ? ' · auto-promotes within signed policy scope' : ' · automatic within budget'}
    </span>
  );
}

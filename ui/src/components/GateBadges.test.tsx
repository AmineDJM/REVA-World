import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { GateDecisionBadge, TIER_POLICY_TEXT, TierBadge } from './GateBadges';
import { ProvenanceProvider } from './ProvenanceDrawer';
import AcquisitionPage from '../pages/AcquisitionPage';
import { envelope, mockFetch } from '../test/fetchMock';

afterEach(() => vi.unstubAllGlobals());

describe('GateDecisionBadge', () => {
  it('renders each stored gate decision distinctly, with text (not colour alone)', () => {
    const { container } = render(
      <>
        {['pass', 'fail', 'blocked', 'needs_review'].map((d) => (
          <GateDecisionBadge key={d} decision={d} />
        ))}
      </>,
    );
    const badges = Array.from(container.querySelectorAll('.gate-badge'));
    expect(badges.map((b) => b.className)).toEqual([
      'gate-badge gate-pass',
      'gate-badge gate-fail',
      'gate-badge gate-blocked',
      'gate-badge gate-needs_review',
    ]);
    expect(badges.map((b) => b.textContent?.replace(/^\S+\s/, ''))).toEqual(['Pass', 'Fail', 'Blocked', 'Needs human review']);
    // needs_review is neither a pass nor a fail.
    expect(badges[3]).not.toHaveTextContent(/pass|fail/i);
  });

  it('does not coerce an unknown decision into a known one', () => {
    render(<GateDecisionBadge decision="maybe" />);
    expect(screen.getByText('Unrecognised decision: maybe')).toHaveClass('gate-unknown');
  });
});

describe('TierBadge', () => {
  it('shows T0–T3 with the promotion rule', () => {
    const { container } = render(
      <>
        {[0, 1, 2, 3].map((t) => (
          <TierBadge key={t} tier={t} />
        ))}
        <TierBadge tier={null} />
      </>,
    );
    const tiers = Array.from(container.querySelectorAll('.tier-badge')).map((b) => b.textContent);
    expect(tiers[0]).toMatch(/^T0/);
    expect(tiers[1]).toBe('T1 · auto-promotes within signed policy scope');
    expect(tiers[2]).toBe('T2 · human review required');
    expect(tiers[3]).toBe('T3 · human review required');
    expect(tiers[4]).toBe('tier not stated');
    expect(TIER_POLICY_TEXT).toBe('T1 within the signed policy scope promotes automatically without human review; T2/T3 require human review.');
  });
});

describe('Acquisition proposals', () => {
  it('shows the tier, the policy copy and a needs_review gate decision distinctly', async () => {
    const proposal = {
      id: '9a1b2c3d-0000-4000-8000-000000000001',
      proposal_type: 'connector_change',
      manifest_sha256: 'a'.repeat(64),
      permission_tier: 2,
      summary: { title: 'Eurostat demo_pjan parser' },
      author: 'agent:discovery',
      recorded_at: '2026-10-15T00:00:00Z',
      reviews: [],
      gate_results: [
        { id: 'g1', gate_name: 'semantic_equivalence', policy_version: 'p1', decision: 'pass', evidence_sha256: 'b'.repeat(64), evaluator: 'gate', recorded_at: '2026-10-15T00:00:00Z' },
        { id: 'g2', gate_name: 'human_review', policy_version: 'p1', decision: 'needs_review', evidence_sha256: 'c'.repeat(64), evaluator: 'gate', recorded_at: '2026-10-15T00:00:00Z' },
      ],
    };
    mockFetch({
      'GET /v1/connectors': { body: envelope([], { context_mode: 'registry', modality: 'metadata' }) },
      'GET /v1/proposals': { body: envelope([proposal], { context_mode: 'registry', modality: 'metadata' }) },
    });
    render(
      <MemoryRouter>
        <ProvenanceProvider>
          <AcquisitionPage />
        </ProvenanceProvider>
      </MemoryRouter>,
    );
    const card = await screen.findByRole('article', { name: /Proposal 9a1b2c3d/ });
    expect(within(card).getByText('T2 · human review required')).toBeInTheDocument();
    expect(within(card).getByRole('note')).toHaveTextContent(/needs human review/);
    const table = within(card).getByRole('table', { name: 'Gate results' });
    const row = within(table).getByRole('row', { name: /Human review/i });
    expect(row.querySelector('.gate-needs_review')).toHaveTextContent('Needs human review');
    expect(within(table).getByRole('row', { name: /Semantic equivalence/i }).querySelector('.gate-pass')).toHaveTextContent('Pass');
    expect(screen.getAllByText(new RegExp(TIER_POLICY_TEXT.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&'))).length).toBeGreaterThan(0);
  });
});

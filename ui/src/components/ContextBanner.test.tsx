import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ContextBanner, combineOrigins, displayOrigin } from './ContextBanner';
import type { EnvelopeMeta } from '../api/types';

const base: Partial<EnvelopeMeta> = {
  context_id: '11111111-2222-4333-8444-555555555555',
  context_mode: 'belief',
  origin: 'live',
  modality: 'selected_estimate',
  knowledge_cutoff: '2026-10-14T21:00:00Z',
  valid_time: { start: '2023-01-01', end: '2024-01-01' },
  warnings: [],
};

describe('ContextBanner', () => {
  it('shows modality, context, valid time and knowledge cutoff', () => {
    render(<ContextBanner meta={base} />);
    expect(screen.getByText('Selected estimate')).toBeInTheDocument();
    expect(screen.getByText(/Belief release/)).toBeInTheDocument();
    expect(screen.getByText('[2023-01-01, 2024-01-01)')).toBeInTheDocument();
    expect(screen.getByText('2026-10-14 21:00Z')).toBeInTheDocument();
    expect(screen.queryByTestId('simulation-banner')).toBeNull();
    expect(screen.queryByTestId('fixture-label')).toBeNull();
  });

  it('shows the persistent SIMULATION banner for a simulation context', () => {
    render(<ContextBanner meta={{ ...base, context_mode: 'simulation', modality: 'assumption_conditioned_scenario' }} />);
    const banner = screen.getByTestId('simulation-banner');
    expect(banner).toHaveTextContent('SIMULATION');
    expect(banner).toHaveTextContent(/stress test/);
    expect(banner).toHaveTextContent(/not observations or forecasts/);
    expect(banner).toHaveTextContent(/not an estimate of GDP loss/);
    expect(banner.closest('.context-banner')).toHaveClass('context-simulation');
  });

  it('shows the SIMULATION banner when forced, even before data loads', () => {
    render(<ContextBanner meta={null} simulation />);
    expect(screen.getByTestId('simulation-banner')).toHaveTextContent('SIMULATION');
    expect(screen.getAllByText('not loaded').length).toBeGreaterThan(0);
  });

  it('labels test-fixture origin data', () => {
    render(<ContextBanner meta={{ ...base, origin: 'test_fixture' }} />);
    expect(screen.getByTestId('fixture-label')).toHaveTextContent('TEST FIXTURE DATA');
  });

  it('labels a fixture baseline release even when the envelope origin is live', () => {
    render(
      <ContextBanner
        meta={{
          ...base,
          baseline_release: { id: 'a', sequence: 1, knowledge_cutoff: '2026-01-01T00:00:00Z', published_at: null, origin: 'test_fixture' },
        }}
      />,
    );
    expect(screen.getByTestId('fixture-label')).toHaveTextContent('TEST FIXTURE DATA');
  });

  it('renders API warnings', () => {
    render(<ContextBanner meta={{ ...base, warnings: [{ code: 'missing_values', message: '2 period(s) are typed missing' }] }} />);
    expect(screen.getByText(/2 period\(s\) are typed missing/)).toBeInTheDocument();
  });

  it('labels how the knowledge cutoff was determined (read as of)', () => {
    render(<ContextBanner meta={{ ...base, context_mode: 'evidence', knowledge_cutoff_basis: 'read_time' }} />);
    expect(screen.getByText('(read as of)')).toBeInTheDocument();
  });

  it('derives the origin from the records shown when the envelope reports none (no "no records" next to TEST FIXTURE)', () => {
    const entityMeta: Partial<EnvelopeMeta> = {
      ...base,
      origin: 'none',
      baseline_release: { id: '22222222-2222-4222-8222-222222222222', knowledge_cutoff: '2026-10-14T21:00:00Z', origin: 'test_fixture' },
    };
    const { unmount } = render(<ContextBanner meta={entityMeta} dataOrigin="test_fixture" />);
    expect(screen.getByTestId('fixture-label')).toHaveTextContent('TEST FIXTURE');
    const origin = screen.getByTestId('banner-origin');
    expect(origin).toHaveAttribute('data-origin', 'test_fixture');
    expect(origin).toHaveAttribute('data-origin-derived', 'true');
    expect(origin).toHaveTextContent('test fixture (of the records shown on this page)');
    expect(origin).not.toHaveTextContent('no records');
    unmount();

    // Mixed data: the label says so.
    render(<ContextBanner meta={{ ...entityMeta, baseline_release: null }} dataOrigin="mixed" />);
    expect(screen.getByTestId('fixture-label')).toHaveTextContent('some records come from captured test fixtures');
    expect(screen.getByTestId('banner-origin')).toHaveTextContent('mixed (live and test fixture)');
  });

  it('keeps the envelope origin when it states one, and explains "no records" next to a fixture baseline', () => {
    expect(displayOrigin({ origin: 'live' }, 'test_fixture')).toEqual({ origin: 'live', derived: false });
    expect(displayOrigin({ origin: 'none' }, 'none')).toEqual({ origin: 'none', derived: false });
    expect(displayOrigin({ origin: 'none' }, null)).toEqual({ origin: 'none', derived: false });
    render(
      <ContextBanner
        meta={{ ...base, origin: 'none', baseline_release: { id: '22222222-2222-4222-8222-222222222222', knowledge_cutoff: '2026-10-14T21:00:00Z', origin: 'test_fixture' } }}
      />,
    );
    expect(screen.getByTestId('banner-origin')).toHaveTextContent('no records (none shown here; baseline release is a test fixture)');
  });

  it('combines record origins', () => {
    expect(combineOrigins([])).toBe('none');
    expect(combineOrigins(['test_fixture', 'test_fixture', null])).toBe('test_fixture');
    expect(combineOrigins(['live'])).toBe('live');
    expect(combineOrigins(['live', 'test_fixture'])).toBe('mixed');
    expect(combineOrigins(['none', 'mixed'])).toBe('mixed');
  });
});

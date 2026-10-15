import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CORE_GLYPHS, UncertaintyLegend, glyphKindFor, glyphKindsOf } from './UncertaintyGlyph';
import { UncertaintyPanel } from './UncertaintyPanel';
import { MissingValue } from './MissingValue';
import { ValueView } from './ValueView';

const dec = { kind: 'decimal' as const, value: '1', unit_ref: 'unit.usd', basis: {} };

describe('glyphKindFor (driven by the API uncertainty status/kind)', () => {
  it('maps the four core kinds', () => {
    expect(glyphKindFor({ status: 'exact_by_definition', kind: 'exact_by_definition' }, dec)).toBe('exact');
    expect(glyphKindFor({ status: 'unquantified', kind: 'unknown' }, dec)).toBe('unquantified');
    expect(glyphKindFor({ status: 'quantified', kind: 'sensitivity_range', lower: '1', upper: '2' }, dec)).toBe('sensitivity');
    expect(glyphKindFor({ status: 'unquantified', kind: 'unknown' }, { kind: 'missing', reason: 'unavailable' })).toBe('missing');
  });

  it('never draws an unreported or not-applicable uncertainty as exact', () => {
    expect(glyphKindFor(null, dec)).toBe('unquantified');
    expect(glyphKindFor({ status: 'not_applicable' }, dec)).toBe('unquantified');
  });

  it('uses distinct glyphs for probabilistic intervals, bounds and mixed rows', () => {
    expect(glyphKindFor({ status: 'quantified', kind: 'prediction_interval', level: 0.8 })).toBe('interval');
    expect(glyphKindFor({ status: 'quantified', kind: 'bounds' })).toBe('bounds');
    expect(glyphKindFor({ status: 'mixed' })).toBe('mixed');
  });

  it('treats an L2 sensitivity-range value as a ribbon and a missing value as hatched whatever the record says', () => {
    const range = { kind: 'sensitivity_range' as const, lower: '1', upper: '2', unit_ref: 'unit.usd', statement: 's' };
    expect(glyphKindFor({ status: 'unquantified' }, range)).toBe('sensitivity');
    expect(glyphKindFor({ status: 'exact_by_definition' }, { kind: 'missing', reason: 'suppressed' })).toBe('missing');
  });
});

describe('UncertaintyLegend', () => {
  it('always lists the four core glyphs with their meaning', () => {
    render(<UncertaintyLegend />);
    const legend = screen.getByRole('group', { name: 'Uncertainty glyph legend' });
    const items = within(legend).getAllByRole('listitem');
    expect(items.map((li) => li.getAttribute('data-glyph-kind'))).toEqual(CORE_GLYPHS);
    expect(legend).toHaveTextContent('Solid line = exact by definition');
    expect(legend).toHaveTextContent('Dotted line = uncertainty not quantified (this does not mean exact)');
    expect(legend).toHaveTextContent('Ribbon = assumption sensitivity range');
    expect(legend).toHaveTextContent('Hatched box = missing value (a data gap, never zero)');
    for (const k of CORE_GLYPHS) expect(legend.querySelector(`svg[data-glyph="${k}"]`)).not.toBeNull();
    expect(legend).not.toHaveTextContent('shown here');
  });

  it('marks the glyphs used by the listed rows and appends non-core kinds only when present', () => {
    const rows = [
      { value: dec, uncertainty: { status: 'unquantified' as const } },
      { value: { kind: 'missing' as const, reason: 'unavailable' }, uncertainty: { status: 'unquantified' as const } },
      { value: dec, uncertainty: { status: 'quantified' as const, kind: 'prediction_interval', level: 0.9 } },
    ];
    const present = glyphKindsOf(rows);
    expect([...present].sort()).toEqual(['interval', 'missing', 'unquantified']);
    render(<UncertaintyLegend present={present} />);
    const legend = screen.getByTestId('uncertainty-legend');
    const state = Object.fromEntries([...legend.querySelectorAll('li')].map((li) => [li.getAttribute('data-glyph-kind'), li.getAttribute('data-present')]));
    expect(state).toEqual({ exact: 'false', unquantified: 'true', sensitivity: 'false', missing: 'true', interval: 'true' });
  });
});

describe('glyphs are the same everywhere values are listed', () => {
  it('badge, missing value and range use the legend glyphs', () => {
    const { container } = render(
      <>
        <UncertaintyPanel uncertainty={{ status: 'unquantified', kind: 'unknown' }} compact />
        <UncertaintyPanel uncertainty={{ status: 'exact_by_definition' }} compact />
        <UncertaintyPanel uncertainty={{ status: 'quantified', kind: 'sensitivity_range', lower: '1', upper: '2' }} compact />
        <MissingValue reason="unavailable" />
        <ValueView value={{ kind: 'sensitivity_range', lower: '1', upper: '2', unit_ref: 'unit.usd', statement: 's' }} />
      </>,
    );
    const glyphs = [...container.querySelectorAll('svg[data-glyph]')].map((g) => g.getAttribute('data-glyph'));
    expect(glyphs).toEqual(['unquantified', 'exact', 'sensitivity', 'missing', 'sensitivity']);
    // Decorative only: the text carries the meaning, and a missing value still reads "missing (reason)".
    for (const g of container.querySelectorAll('svg[data-glyph]')) expect(g).toHaveAttribute('aria-hidden', 'true');
    expect(container.querySelector('.missing-value')).toHaveTextContent(/^missing \(unavailable\)$/);
    // Pattern ids are unique per glyph instance.
    const ids = [...container.querySelectorAll('pattern')].map((p) => p.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

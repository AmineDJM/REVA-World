import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ValueView } from './ValueView';

describe('ValueView', () => {
  it('renders an L2 sensitivity range as a range, never as a point or a confidence interval', () => {
    render(
      <ValueView
        value={{
          kind: 'sensitivity_range',
          lower: '397.5',
          upper: '562.5',
          unit_ref: 'unit.usd',
          evaluated_points: 3,
          includes_fixed_quantity_point: true,
          statement: 'assumption sensitivity, not a confidence interval',
        }}
      />,
    );
    const el = screen.getByTitle('assumption sensitivity, not a confidence interval');
    expect(el.textContent).toMatch(/397\.5.*to.*562\.5/);
    expect(el.textContent).toContain('sensitivity range (not a confidence interval)');
  });
});

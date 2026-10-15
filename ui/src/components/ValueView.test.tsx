import { fireEvent, render, screen } from '@testing-library/react';
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

  it('rounds a simulation output for display and keeps the exact string on hover and expand', () => {
    const { container } = render(<ValueView value={{ kind: 'decimal', value: '57082396439.600717', unit_ref: 'unit.usd', basis: {} }} compact />);
    const el = container.querySelector('.value')!;
    expect(el.querySelector('.num')!.textContent).toBe('57,082,396,439.60');
    expect(el.textContent).toContain('(57.08 billion US$)');
    expect(el.textContent).not.toContain('600717');
    expect(el.getAttribute('title')).toBe('exact: 57082396439.600717 unit.usd');
    const toggle = screen.getByRole('button', { name: 'Show exact value' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    expect(screen.getByTestId('exact-value')).toHaveTextContent('= 57082396439.600717');
    expect(screen.getByRole('button', { name: 'Hide exact value' })).toHaveAttribute('aria-expanded', 'true');
  });

  it('shows percent of GDP to 4 decimals and no toggle when nothing was rounded', () => {
    const { container, unmount } = render(<ValueView value={{ kind: 'decimal', value: '1.234567891', unit_ref: 'unit.percent_of_baseline_gdp', basis: {} }} />);
    expect(container.querySelector('.num')!.textContent).toBe('1.2346');
    expect(screen.getByRole('button', { name: 'Show exact value' })).toBeInTheDocument();
    unmount();
    render(<ValueView value={{ kind: 'decimal', value: '5564', unit_ref: 'unit.usd', basis: {} }} />);
    expect(screen.getByText('5,564')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('rounds sensitivity-range ends the same way and exposes both exact ends', () => {
    render(
      <ValueView
        value={{ kind: 'sensitivity_range', lower: '397.123', upper: '562.5', unit_ref: 'unit.usd', statement: 'assumption sensitivity, not a confidence interval' }}
      />,
    );
    const el = screen.getByTitle('assumption sensitivity, not a confidence interval');
    expect(el.textContent).toMatch(/397\.12 to 562\.50 US\$/);
    expect(el.querySelector('[data-glyph="sensitivity"]')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show exact value' }));
    expect(screen.getByTestId('exact-value')).toHaveTextContent('397.123 to 562.5');
  });
});

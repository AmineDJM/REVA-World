import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Sparkline } from './Sparkline';

describe('Sparkline (review V3b HL-1)', () => {
  it('interrupts the line where the answering source changes and says so', () => {
    const p = (year: number, value: string, series: string) => ({ year, value, value_text: null, series, flags: {} });
    const { container } = render(
      <Sparkline points={[p(2022, '66277409', 'un_wpp.x'), p(2023, '66438822', 'un_wpp.x'), p(2024, '68551653', 'wb.y'), p(2025, '68720337', 'wb.y')]} latestSeries="wb.y" />,
    );
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('data-source-breaks')).toBe('2024');
    expect(svg.getAttribute('aria-label')).toContain('changement de source en 2024');
    expect(svg.querySelectorAll('path')).toHaveLength(2); // two segments, never one line across sources
  });
});

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MissingValue } from './MissingValue';
import { ValueView } from './ValueView';
import { formatDecimalString, valueToText, valueNumericString } from '../lib/format';

describe('MissingValue', () => {
  it('renders "missing (reason)"', () => {
    render(<MissingValue reason="unavailable" />);
    expect(screen.getByText('missing (unavailable)')).toBeInTheDocument();
  });

  it('labels every kernel reason and never renders 0', () => {
    for (const reason of ['not_collected', 'unavailable', 'not_applicable', 'suppressed', 'source_error', 'unmatched_semantics', 'outside_support', 'not_extracted']) {
      const { container, unmount } = render(<MissingValue reason={reason} />);
      expect(container.textContent).toMatch(/^missing \(.+\)$/);
      expect(container.textContent).not.toMatch(/\b0\b/);
      unmount();
    }
  });

  it('states an unstated reason explicitly', () => {
    render(<MissingValue />);
    expect(screen.getByText('missing (reason not stated)')).toBeInTheDocument();
  });
});

describe('ValueView', () => {
  it('renders a typed missing value as missing, never as zero', () => {
    const { container } = render(<ValueView value={{ kind: 'missing', reason: 'unavailable' }} compact showBasis />);
    expect(container.textContent).toBe('missing (unavailable)');
    expect(container.textContent).not.toContain('0');
  });

  it('renders an absent value as missing, never as zero', () => {
    const { container } = render(<ValueView value={null} />);
    expect(container.textContent).toMatch(/^missing \(/);
    expect(container.textContent).not.toMatch(/\b0\b/);
  });

  it('renders an explicit decimal zero as 0 (a reported zero is a value)', () => {
    const { container } = render(<ValueView value={{ kind: 'decimal', value: '0', unit_ref: 'unit.usd', basis: {} }} />);
    expect(container.textContent).toContain('0');
    expect(container.textContent).not.toContain('missing');
  });

  it('formats decimal strings without float loss', () => {
    render(<ValueView value={{ kind: 'decimal', value: '68210345678.123456', unit_ref: 'unit.usd', basis: { valuation: 'CIF' } }} showBasis />);
    expect(screen.getByText('68,210,345,678.123456')).toBeInTheDocument();
    expect(screen.getByText(/CIF/)).toBeInTheDocument();
  });
});

describe('format helpers', () => {
  it('groups digits and trims trailing zeros', () => {
    expect(formatDecimalString('1234567.5000')).toBe('1,234,567.5');
    expect(formatDecimalString('-1000')).toBe('−1,000');
    expect(formatDecimalString('0.1234567', 3)).toBe('0.123');
  });
  it('exports missing values as empty numbers with a reason', () => {
    expect(valueNumericString({ kind: 'missing', reason: 'unavailable' })).toBeNull();
    expect(valueToText({ kind: 'missing', reason: 'suppressed' })).toBe('missing (suppressed)');
  });
});

import { describe, expect, it } from 'vitest';
import { formatDecimalString } from './format';

describe('formatDecimalString', () => {
  it('keeps the rounding carry into the integer part', () => {
    expect(formatDecimalString('0.9999999')).toBe('1');
    expect(formatDecimalString('1999.99999995')).toBe('2,000');
    expect(formatDecimalString('999999.9999999')).toBe('1,000,000');
    expect(formatDecimalString('-0.9999999')).toBe('−1');
    expect(formatDecimalString('0.99999995', 2)).toBe('1');
    expect(formatDecimalString('12.3456785')).toBe('12.345679');
  });

  it('does not round down or change exact short values', () => {
    expect(formatDecimalString('1234567.5')).toBe('1,234,567.5');
    expect(formatDecimalString('0.0000004')).toBe('0');
    expect(formatDecimalString('-0.0000004')).toBe('0');
    expect(formatDecimalString('-1.25')).toBe('−1.25');
    expect(formatDecimalString('0')).toBe('0');
  });
});

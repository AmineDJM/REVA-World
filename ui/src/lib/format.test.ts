import { describe, expect, it } from 'vitest';
import { formatDecimalString, sourceFlagNotes } from './format';

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

describe('sourceFlagNotes', () => {
  it('surfaces provider estimation and not-reported flags', () => {
    expect(
      sourceFlagNotes({ isReported: false, legacyEstimationFlag: '6', provider_estimation_flagged: true, classificationCode: 'H4' }),
    ).toEqual(['provider estimate (legacyEstimationFlag 6)']);
    expect(sourceFlagNotes({ isReported: false, legacyEstimationFlag: '0', provider_estimation_flagged: false })).toEqual([
      'not directly reported (isReported=false)',
    ]);
    expect(sourceFlagNotes({ provider_returned_no_rows: true, flowCode: 'M' })).toEqual(['provider returned no rows for this key']);
  });

  it('uses an API note verbatim and says nothing for clean flags', () => {
    expect(
      sourceFlagNotes({ note: 'provider estimation flagged: not a direct customs observation', provider_estimation_flagged: true, isReported: false }),
    ).toEqual(['provider estimation flagged: not a direct customs observation']);
    expect(sourceFlagNotes({ indicator: 'SP.POP.TOTL', obs_status: '' })).toEqual([]);
    expect(sourceFlagNotes(null)).toEqual([]);
  });
});

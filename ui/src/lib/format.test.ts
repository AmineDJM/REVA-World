import { describe, expect, it } from 'vitest';
import { compareDecimalStrings, formatDecimalString, pointInRange, sourceFlagNotes } from './format';

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

describe('compareDecimalStrings / pointInRange', () => {
  it('compares decimal strings exactly, including sign and trailing zeros', () => {
    expect(compareDecimalStrings('84411166795', '84411166795.000')).toBe(0);
    expect(compareDecimalStrings('-10279108880.596527', '13816425164.75')).toBe(-1);
    expect(compareDecimalStrings('0.0000000000000000001', '0')).toBe(1);
    expect(compareDecimalStrings('-0', '0')).toBe(0);
    expect(compareDecimalStrings('-2.5', '-2.25')).toBe(-1);
    expect(compareDecimalStrings('abc', '1')).toBeNull();
  });

  it('places L1 at an end of a one-sided sensitivity range', () => {
    expect(pointInRange('13816425164.75', '-10279108880.596527', '13816425164.75')).toBe('upper_end');
    expect(pointInRange('210950531.25', '210950531.25', '283975909.495112')).toBe('lower_end');
    expect(pointInRange('5', '1', '9')).toBe('inside');
    expect(pointInRange('3', '3', '3')).toBe('degenerate');
    expect(pointInRange('10', '1', '9')).toBe('outside');
  });
});

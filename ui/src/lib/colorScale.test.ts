import { describe, expect, it } from 'vitest';
import { EXPOSURE_SCALE, NEG_STEPS, NEUTRAL, POS_STEPS, classify, legendRows, niceScale } from './colorScale';

describe('diverging colour scale', () => {
  it('classifies by exact decimal comparison, lower bound inclusive', () => {
    expect(classify('0', EXPOSURE_SCALE)!.color).toBe(NEUTRAL);
    expect(classify('0.000', EXPOSURE_SCALE)!.step).toBe(0);
    expect(classify('0.2499999999999999999999', EXPOSURE_SCALE)!.step).toBe(1);
    expect(classify('0.25', EXPOSURE_SCALE)!.step).toBe(2);
    expect(classify('5', EXPOSURE_SCALE)!.color).toBe(POS_STEPS[4]);
    expect(classify('-0.2576', EXPOSURE_SCALE)!.color).toBe(NEG_STEPS[1]);
    expect(classify('-12', EXPOSURE_SCALE)!.step).toBe(-5);
    expect(classify('missing', EXPOSURE_SCALE)).toBeNull();
  });

  it('has a legend row for every class, neutral in the middle', () => {
    const rows = legendRows(EXPOSURE_SCALE);
    expect(rows).toHaveLength(11);
    expect(rows[5]!.step).toBe(0);
    expect(rows[0]!.label).toBe('−5 % or beyond');
    expect(rows[6]!.label).toBe('above 0 to +0.25 %');
    expect(rows[4]!.label).toBe('below 0 to −0.25 %');
  });

  it('nice scale breaks cover the largest magnitude', () => {
    expect(niceScale(['0.3', '-1.7', '12.2'], '%').breaks).toEqual(['2.5', '5', '10', '20']);
    expect(niceScale([], '%').breaks).toHaveLength(4);
  });
});

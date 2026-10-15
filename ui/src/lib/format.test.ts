import { describe, expect, it } from 'vitest';
import { compareDecimalStrings, displayDecimal, formatDecimalString, isQuantityConcept, valueToText, pointInRange, sourceFlagNotes } from './format';

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
  const VALUE = 'trade.hs2709.import_value.world_total';
  const WEIGHT = 'trade.hs2709.import_net_weight.world_total';

  it('labels a value as provider-estimated only when provider_value_estimated === true', () => {
    expect(sourceFlagNotes({ provider_value_estimated: true, isReported: false }, { concept: VALUE })).toEqual([
      'provider estimate (value marked estimated by the provider)',
    ]);
    // Comtrade 1.2.0 value rows: null = undeterminable, never claimed either way.
    expect(sourceFlagNotes({ provider_value_estimated: null, legacyEstimationFlag: '6', isReported: false }, { concept: VALUE })).toEqual([
      'not directly reported (isReported=false)',
    ]);
    expect(sourceFlagNotes({ provider_value_estimated: false, legacyEstimationFlag: '6' }, { concept: VALUE })).toEqual([]);
  });

  it('shows quantity estimation only on quantity / net-weight concepts', () => {
    const f = { provider_quantity_estimated: true, legacyEstimationFlag: '6', isReported: true };
    expect(sourceFlagNotes(f, { concept: WEIGHT })).toEqual(['provider estimate of quantity/net weight (legacyEstimationFlag 6)']);
    expect(sourceFlagNotes(f, { concept: VALUE })).toEqual([]);
    expect(sourceFlagNotes({ provider_quantity_estimated: false }, { concept: WEIGHT })).toEqual([]);
  });

  it('reads the legacy provider_estimation_flagged key on old rows as quantity/net weight, never as value-estimated', () => {
    const notes = sourceFlagNotes({ isReported: false, legacyEstimationFlag: '6', provider_estimation_flagged: true, classificationCode: 'H4' }, { concept: VALUE });
    expect(notes).toEqual(['legacy flag (meaning: quantity/net weight estimated), legacyEstimationFlag 6', 'not directly reported (isReported=false)']);
    expect(notes.join(' ')).not.toMatch(/provider estimate/);
    // An old API note that read the legacy flag as a value estimate is not repeated.
    const old = sourceFlagNotes({ note: 'provider estimation flagged (legacyEstimationFlag != 0): not a direct customs observation', provider_estimation_flagged: true });
    expect(old).toEqual(['legacy flag (meaning: quantity/net weight estimated)']);
  });

  it('surfaces not-reported, no-rows and unrecognised-code flags', () => {
    expect(sourceFlagNotes({ isReported: false, legacyEstimationFlag: '0', provider_value_estimated: null })).toEqual([
      'not directly reported (isReported=false)',
    ]);
    expect(sourceFlagNotes({ provider_returned_no_rows: true, flowCode: 'M' })).toEqual(['provider returned no rows for this key']);
    expect(sourceFlagNotes({ legacy_estimation_code_unrecognised: true, legacyEstimationFlag: '9', provider_value_estimated: null })).toEqual([
      'unrecognised provider estimation code (legacyEstimationFlag 9)',
    ]);
  });

  it('uses an API note verbatim and says nothing for clean flags', () => {
    expect(sourceFlagNotes({ note: 'provider marks this value as estimated', provider_value_estimated: true })).toEqual([
      'provider marks this value as estimated',
    ]);
    expect(sourceFlagNotes({ note: 'provider marks this value as not directly reported (isReported=false)', isReported: false })).toEqual([
      'provider marks this value as not directly reported (isReported=false)',
    ]);
    expect(sourceFlagNotes({ indicator: 'SP.POP.TOTL', obs_status: '' })).toEqual([]);
    expect(sourceFlagNotes(null)).toEqual([]);
  });

  it('uses the concept carried in simulation input flags', () => {
    expect(isQuantityConcept(WEIGHT)).toBe(true);
    expect(isQuantityConcept(VALUE)).toBe(false);
    expect(sourceFlagNotes({ concept: WEIGHT, provider_quantity_estimated: true })).toEqual(['provider estimate of quantity/net weight']);
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

describe('displayDecimal (display precision; exact string untouched)', () => {
  it('shows US$ to the cent and flags the rounding', () => {
    const d = displayDecimal('57082396439.600717', 'unit.usd');
    expect(d).toEqual({ text: '57,082,396,439.60', exact: '57082396439.600717', rounded: true });
    expect(displayDecimal('-12.345', 'unit.usd').text).toBe('−12.35');
    expect(displayDecimal('0.004', 'unit.usd')).toEqual({ text: '0.00', exact: '0.004', rounded: true });
    expect(displayDecimal('1999.999', 'unit.usd').text).toBe('2,000.00');
  });

  it('keeps integers and short fractions exact (nothing to expand)', () => {
    expect(displayDecimal('5564', 'unit.usd')).toEqual({ text: '5,564', exact: '5564', rounded: false });
    expect(displayDecimal('1.5', 'unit.usd')).toEqual({ text: '1.50', exact: '1.5', rounded: false });
    expect(displayDecimal('1.2300000', 'unit.usd').rounded).toBe(false);
  });

  it('shows percent of GDP to 4 decimals', () => {
    expect(displayDecimal('0.123456789', 'unit.percent_of_baseline_gdp')).toEqual({ text: '0.1235', exact: '0.123456789', rounded: true });
    expect(displayDecimal('-1.20004', 'unit.percent_of_baseline_gdp').text).toBe('−1.2');
    expect(displayDecimal('2.5', 'unit.percent').rounded).toBe(false);
  });

  it('keeps up to 6 decimals for other units', () => {
    expect(displayDecimal('0.1234567', 'unit.ratio').text).toBe('0.123457');
    expect(displayDecimal('68000000', 'unit.persons').text).toBe('68,000,000');
  });

  it('uses the same display precision in plain-text renderings', () => {
    expect(valueToText({ kind: 'decimal', value: '57082396439.600717', unit_ref: 'unit.usd', basis: {} })).toBe('57,082,396,439.60 US$');
  });
});

import { describe, expect, it } from 'vitest';
import { DEFAULT_FORM, FIXTURE_BASELINE_UUID, buildScenarioSpec, csvCell, resultsToCsv, resultsToJson, validateBaselineReleaseId, validateForm } from './scenario';
import type { SimResult } from '../api/types';

const REAL = '7a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d';
const release = { id: REAL, knowledge_cutoff: '2026-10-14T20:00:00Z' };

describe('scenario spec', () => {
  it('rejects the fixture baseline UUID and malformed ids', () => {
    expect(validateBaselineReleaseId(FIXTURE_BASELINE_UUID)).toMatch(/fixture/i);
    expect(validateBaselineReleaseId(FIXTURE_BASELINE_UUID.toUpperCase())).toMatch(/fixture/i);
    expect(validateBaselineReleaseId('not-a-uuid')).toMatch(/UUID/);
    expect(validateBaselineReleaseId(undefined)).toMatch(/No published baseline/);
    expect(validateBaselineReleaseId(REAL)).toBeNull();
    expect(() => buildScenarioSpec(DEFAULT_FORM, { id: FIXTURE_BASELINE_UUID, knowledge_cutoff: '2026-10-14T21:00:00Z' })).toThrow(/fixture/i);
  });

  it('builds an L1 spec with fixed quantities and an empty grid, pinned to the release cutoff', () => {
    const spec = buildScenarioSpec({ ...DEFAULT_FORM, fidelity: 'L1' }, release);
    expect(spec.baseline_release_id).toBe(REAL);
    expect(spec.knowledge_cutoff).toBe(release.knowledge_cutoff);
    expect(spec.assumptions).toEqual({ quantity_response: 'fixed', elasticity_grid: [], no_general_equilibrium: true });
    expect(spec.missing_input_policy).toBe('fail');
    expect(spec.intervention.import_multiplier).toBe(1.25);
    expect(spec.countries).toEqual(['FRA', 'CHN', 'USA']);
  });

  it('builds an L2 spec with labelled scenario-assumption elasticities', () => {
    const spec = buildScenarioSpec({ ...DEFAULT_FORM, fidelity: 'L2' }, release);
    expect(spec.assumptions.quantity_response).toBe('elasticity_sensitivity');
    expect(spec.assumptions.elasticity_grid.length).toBe(3);
    expect(spec.assumptions.elasticity_grid.every((g) => g.basis === 'scenario_assumption')).toBe(true);
  });

  it('validates ranges from the schema', () => {
    expect(validateForm({ ...DEFAULT_FORM, importMultiplier: '0' }).importMultiplier).toBeDefined();
    expect(validateForm({ ...DEFAULT_FORM, exportMultiplier: '3.5' }).exportMultiplier).toBeDefined();
    expect(validateForm({ ...DEFAULT_FORM, referenceYear: '2019' }).referenceYear).toBeDefined();
    expect(validateForm({ ...DEFAULT_FORM, countries: [] }).countries).toBeDefined();
    expect(validateForm({ ...DEFAULT_FORM, fidelity: 'L2', grid: [{ importElasticity: '-3', exportElasticity: '0' }] })['grid.0.import']).toBeDefined();
    expect(validateForm({ ...DEFAULT_FORM, fidelity: 'L2', grid: [] }).grid).toBeDefined();
    expect(Object.keys(validateForm(DEFAULT_FORM))).toHaveLength(0);
  });

  it('embeds the SIMULATION label and full scenario context in exports', () => {
    const results: SimResult[] = [
      {
        id: 'r1',
        output_key: 'import_value_change_usd',
        subject: { id: 'e1', label: 'France' },
        variant: 'L1',
        value: { kind: 'decimal', value: '1000', unit_ref: 'unit.usd', basis: {} },
        uncertainty: { status: 'quantified', kind: 'sensitivity_range', lower: '900', upper: '1000', method_version: 'm' },
        applicability: {},
      },
      {
        id: 'r2',
        output_key: 'import_value_change_usd',
        subject: { id: 'e2', label: 'Algeria' },
        variant: 'L1',
        value: { kind: 'missing', reason: 'unavailable' },
        uncertainty: { status: 'unquantified', kind: 'unknown' },
        applicability: {},
      },
    ];
    const ctx = {
      simulation_id: 'sim-1',
      scenario_id: 'scn-1',
      scenario_title: 'T',
      baseline_release_id: REAL,
      baseline_release_sequence: 3,
      knowledge_cutoff: '2026-10-14T20:00:00Z',
      model_versions: ['economic.crude_exposure@1.0.0'],
      fidelity: 'L2',
      assumptions: ['No general equilibrium'],
      origin: 'live',
    };
    const csv = resultsToCsv(ctx, results);
    expect(csv.split('\n')[0]).toMatch(/^# SIMULATION/);
    for (const s of ['scenario_id: scn-1', `baseline_release_id: ${REAL}`, 'knowledge_cutoff: 2026-10-14T20:00:00Z', 'economic.crude_exposure@1.0.0', 'assumption: No general equilibrium']) {
      expect(csv).toContain(s);
    }
    expect(csv).toContain('Sensitivity range (not a confidence interval)');
    const algeria = csv.split('\n').find((l) => l.startsWith('Algeria'))!;
    expect(algeria).toContain(',,'); // empty value, never 0
    expect(algeria).toContain('unavailable');
    const json = JSON.parse(resultsToJson(ctx, results));
    expect(json.banner).toMatch(/^SIMULATION/);
    expect(json.modality).toBe('assumption_conditioned_scenario');
    expect(json.context.baseline_release_id).toBe(REAL);
    expect(json.results[1].value).toEqual({ kind: 'missing', reason: 'unavailable' });
  });

  it('exports an L2 sensitivity-range row as bounds, never as a point value', () => {
    const results: SimResult[] = [
      {
        id: 'r3',
        output_key: 'import_value_change_usd',
        subject: { id: 'e1', label: 'France' },
        variant: 'L2:sensitivity_range',
        value: {
          kind: 'sensitivity_range',
          lower: '397.5',
          upper: '562.5',
          unit_ref: 'unit.usd',
          basis: {},
          evaluated_points: 3,
          includes_fixed_quantity_point: true,
          statement: 'assumption sensitivity, not a confidence interval',
        },
        uncertainty: { status: 'quantified', kind: 'sensitivity_range', lower: '397.5', upper: '562.5' },
        applicability: {},
      },
    ];
    const ctx = {
      simulation_id: 'sim-2', scenario_id: 'scn-2', scenario_title: 'T', baseline_release_id: 'b',
      baseline_release_sequence: 1, knowledge_cutoff: '2026-10-14T20:00:00Z', model_versions: [], fidelity: 'L2',
      assumptions: [], origin: 'live',
    };
    const row = resultsToCsv(ctx, results).split('\n').find((l) => l.startsWith('France'))!;
    expect(row).toBe('France,import_value_change_usd,L2:sensitivity_range,,unit.usd,,' + row.split(',')[6] + ',397.5,562.5');
  });

  it('neutralises spreadsheet formulas, including number-led payloads', () => {
    for (const evil of ["-2+3+cmd|' /C calc'!A0", '=HYPERLINK("x")', '+1+1', '@SUM(A1)', '\t=1', '\r=1', '-1e3+cmd']) {
      expect(csvCell(evil).replace(/^"/, '').startsWith("'")).toBe(true);
    }
    for (const ok of ['-2', '-2.5', '1e-7', '-3.5E+2', '1000', 'France']) expect(csvCell(ok)).toBe(ok);
    expect(csvCell('a,b')).toBe('"a,b"');
    expect(csvCell('a\n=1')).toBe('"a\n=1"');
  });

  it('cannot inject rows or cells through header values', () => {
    const ctx = {
      simulation_id: 's', scenario_id: 'x', scenario_title: 'Stress\n=HYPERLINK("http://evil"),=1+1\r\n-2+cmd',
      baseline_release_id: 'b', baseline_release_sequence: 1, knowledge_cutoff: 'k', model_versions: [], fidelity: 'L1',
      assumptions: ['a\n=EVIL()'], origin: 'live',
    };
    const lines = resultsToCsv(ctx, []).trimEnd().split('\n');
    expect(lines.every((l) => l.startsWith('# ') || l.startsWith('"# ') || l.startsWith('country,'))).toBe(true);
    const title = lines.find((l) => l.includes('scenario_title'))!;
    expect(title.startsWith('"# scenario_title: Stress =HYPERLINK(""http://evil""),=1+1 -2+cmd"')).toBe(true);
  });
});

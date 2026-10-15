import { describe, expect, it } from 'vitest';
import type { Relation, SimResult, SimulationStatus } from '../api/types';
import { anchorsByEntity, arcWidth, exposureRows, featureAnchor, greatCircle, mergeReleaseRelations, mirrorFlows, problemText, reportedPartnerFlows, runCountryStatuses } from './worldmap';

function rel(id: string, reporter: [string, string], partner: [string, string, string], flow: 'M' | 'X', value: string | null, partnerCode = '999', period = '2023'): Relation {
  return {
    id,
    predicate: 'predicate.trade_flow',
    layer: 'trade.crude_petroleum',
    support: { id: `s${id}`, temporal: { label: period }, spatial: { key: `COMTRADE:251->${partnerCode}` }, population: {}, aggregation: 'period_flow_total', period_label: period },
    component: { flow, valuation: flow === 'M' ? 'CIF' : 'FOB' },
    existence_uncertainty: {},
    value: value === null ? { kind: 'missing', reason: 'unavailable' } : { kind: 'decimal', value, unit_ref: 'unit.usd' },
    evidence_observation_id: `o${id}`,
    origin: 'test_fixture',
    recorded_at: '2026-10-15T00:00:00Z',
    participants: [
      { ordinal: 0, role: 'role.reporter', entity_id: reporter[0], entity_label: reporter[1], entity_kind: 'entity.country', direction: flow === 'M' ? 'in' : 'out' },
      { ordinal: 1, role: 'role.partner', entity_id: partner[0], entity_label: partner[1], entity_kind: partner[2], direction: flow === 'M' ? 'out' : 'in' },
      { ordinal: 2, role: 'role.product', entity_id: 'hs', entity_label: 'HS 2709', entity_kind: 'entity.product', direction: 'undirected' },
    ],
  };
}

const FRA: [string, string] = ['fra', 'France'];
const DZA: [string, string, string] = ['dza', 'Algeria', 'entity.country'];

function result(country: string, id: string, output: string, value: string, variant = 'L1'): SimResult {
  return {
    id: `${country}-${output}-${variant}`,
    output_key: output,
    subject: { id, label: country },
    variant,
    value: { kind: 'decimal', value, unit_ref: 'unit.percent_of_baseline_gdp' },
    uncertainty: { status: 'unquantified', kind: 'unknown' },
    applicability: { country },
  };
}

describe('runCountryStatuses', () => {
  it('marks computed, missing_input and skipped countries with typed reasons, never zero', () => {
    const st = {
      results: [result('FRA', 'fra', 'exposure_percent_baseline_gdp', '0.2576')],
      scenario: { spec: { countries: ['FRA', 'DZA', 'USA', 'CHN'] } },
      outcome: {
        status: 'succeeded',
        applicability: {
          countries: {
            FRA: { status: 'ready', problems: [] },
            DZA: { status: 'missing_input', problems: [{ input: 'M', concept: 'trade.hs2709.import_value.world_total', year: 2023, state: 'missing:unavailable' }] },
          },
          skipped_countries: [{ country: 'USA', missing: [{ input: 'G', year: 2023, state: 'absent' }] }],
        },
        problems: [],
        cost: {},
        recorded_at: '',
      },
    } as unknown as SimulationStatus;
    const s = runCountryStatuses(st);
    expect(s.FRA!.state).toBe('computed');
    expect(s.DZA!.state).toBe('missing_input');
    expect(s.DZA!.reasons[0]).toBe('M · trade.hs2709.import_value.world_total 2023 — missing: unavailable');
    expect(s.USA!.state).toBe('skipped');
    expect(s.USA!.reasons).toEqual(['G 2023 — absent']);
    expect(s.CHN!.state).toBe('not_reported');
  });

  it('formats unknown problem shapes without dropping them', () => {
    expect(problemText('x')).toBe('x');
    expect(problemText({ code: 'missing_input', detail: 'd' })).toBe('missing_input');
    expect(problemText({ foo: 1 })).toBe('{"foo":1}');
  });
});

describe('exposureRows', () => {
  it('keeps only L1 decimal exposure rows, exact strings, sorted by value', () => {
    const rows = exposureRows([
      result('FRA', 'fra', 'exposure_percent_baseline_gdp', '0.2576'),
      result('CHN', 'chn', 'exposure_percent_baseline_gdp', '0.4608569928647694504609'),
      result('CHN', 'chn', 'exposure_percent_baseline_gdp', '0.1', 'L2:eM=-0.5;eX=0'),
      result('CHN', 'chn', 'import_value_change_usd', '1'),
    ]);
    expect(rows.map((r) => r.iso3)).toEqual(['CHN', 'FRA']);
    expect(rows[0]!.value).toBe('0.4608569928647694504609');
  });
});

describe('partner flows', () => {
  const rels = [
    rel('1', FRA, DZA, 'M', '3042655857.775', '12'),
    rel('2', FRA, ['kaz', 'Kazakhstan', 'entity.country'], 'M', '3603711989.944', '398'),
    rel('3', FRA, ['nes', 'Areas, nes', 'entity.world_aggregate'], 'M', '1203.697', '899'),
    rel('4', FRA, ['w', 'World', 'entity.world_aggregate'], 'M', '31530866245.503', '0'),
    rel('5', FRA, ['fra', 'France', 'entity.country'], 'M', '12', '251'),
    rel('6', FRA, ['zaf', 'South Africa', 'entity.country'], 'X', '3.271', '710'),
    rel('7', ['usa', 'United States'], DZA, 'M', '530769604', '12'),
  ];

  it('separates countries, composites, World and the reporter itself; World is never a country partner', () => {
    const f = reportedPartnerFlows(rels, 'fra', 'M');
    expect(f.countries.map((r) => r.partnerLabel)).toEqual(['Kazakhstan', 'Algeria']);
    expect(f.composites.map((r) => r.partnerLabel)).toEqual(['Areas, nes']);
    expect(f.world.map((r) => r.partnerLabel)).toEqual(['World']);
    expect(f.self.map((r) => r.partnerLabel)).toEqual(['France']);
    expect(reportedPartnerFlows(rels, 'fra', 'X').countries.map((r) => r.partnerLabel)).toEqual(['South Africa']);
  });

  it('mirror rows are what partners report about the country, never its own reports', () => {
    const m = mirrorFlows(rels, 'dza');
    expect(m.map((r) => r.reporterLabel)).toEqual(['France', 'United States']);
    expect(m.every((r) => r.partnerId === 'dza' && r.reporterId !== 'dza')).toBe(true);
    expect(m[0]!.value).toBe('3042655857.775');
    expect(mirrorFlows(rels, 'nobody')).toEqual([]);
  });

  it('keeps a typed missing partner value as missing (never 0)', () => {
    const f = reportedPartnerFlows([rel('9', FRA, DZA, 'M', null)], 'fra', 'M');
    expect(f.countries[0]!.value).toBeNull();
    expect(f.countries[0]!.missingReason).toBe('unavailable');
  });
});

describe('geometry', () => {
  it('anchors on the largest polygon and only for matched features', () => {
    const square = (x: number, y: number, s: number) => [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]];
    const g = { type: 'MultiPolygon', coordinates: [square(0, 0, 1), square(10, 10, 4)] };
    expect(featureAnchor(g)).toEqual([12, 12]);
    const anchors = anchorsByEntity([
      { id: 'a', geometry: g, properties: { feature_key: 'A', entity_id: 'e1', match_status: 'reviewed_match', dataset_sha256: '' } },
      { id: 'b', geometry: g, properties: { feature_key: 'B', entity_id: 'e2', match_status: 'unmatched', dataset_sha256: '' } },
    ]);
    expect([...anchors.keys()]).toEqual(['e1']);
  });

  it('great-circle arcs keep longitudes continuous across the antimeridian', () => {
    const pts = greatCircle([170, 10], [-170, 10], 10);
    for (let i = 1; i < pts.length; i++) expect(Math.abs(pts[i]![0] - pts[i - 1]![0])).toBeLessThan(10);
    expect(pts[0]).toEqual([170, 10]);
  });

  it('arc width grows with the square root of the value', () => {
    expect(arcWidth('100', 100)).toBe(4);
    expect(arcWidth('25', 100)).toBe(3);
    expect(arcWidth(null, 100)).toBe(1);
  });
});

describe('mergeReleaseRelations', () => {
  it('keeps release values and fills only unrecorded ones from evidence as of the cutoff, marked', () => {
    const pinned = [
      { ...rel('r1', FRA, DZA, 'M', '3042655857.775', '12'), release_id: 'rel' },
      { ...rel('r2', FRA, ['kaz', 'Kazakhstan', 'entity.country'], 'M', null, '398'), value: null, release_id: 'rel' },
      { ...rel('r3', FRA, ['nga', 'Nigeria', 'entity.country'], 'M', null, '566'), value: null, release_id: 'rel' },
    ];
    const evidence = [rel('e2', FRA, ['kaz', 'Kazakhstan', 'entity.country'], 'M', '3603711989.944', '398'), rel('e1', FRA, DZA, 'M', '1', '12')];
    const flows = reportedPartnerFlows(mergeReleaseRelations(pinned, evidence), 'fra', 'M').countries;
    const by = Object.fromEntries(flows.map((f) => [f.partnerLabel, f]));
    expect(by.Algeria).toMatchObject({ value: '3042655857.775', valueSource: 'release' });
    expect(by.Kazakhstan).toMatchObject({ value: '3603711989.944', valueSource: 'evidence_as_of' });
    expect(by.Nigeria).toMatchObject({ value: null, missingReason: 'not_reported', valueSource: 'release' });
  });
});

import { describe, expect, it } from 'vitest';
import { feature } from './index';
import { buildCommands, daysUntil, fmtP, fmtRange, fmtRatio, notCountedReason, rowSentence, whenLabel } from './model';
import type { Row } from './api';

describe('scores model', () => {
  it('formats ratios, ranges and p-values in French, never inventing a value', () => {
    expect(fmtRatio(0.629713)).toBe('×0,63');
    expect(fmtRatio(null)).toBeNull();
    expect(fmtRatio(Number.NaN)).toBeNull();
    expect(fmtRange(0.44, 0.97)).toBe('×0,44–0,97');
    expect(fmtRange(0.5, 0.5)).toBe('×0,50');
    expect(fmtRange(null, 1)).toBeNull();
    expect(fmtP(0.0264935)).toBe('0,026');
    expect(fmtP(2.1e-7)).toBe('2,1e-7');
    expect(fmtP(undefined)).toBeNull();
  });

  it('labels calendar dates by what the source states (day, month, after a date, undated)', () => {
    expect(whenLabel({ date: '2027-07-03' })).toBe('3 juil. 2027');
    expect(whenLabel({ month: '2027-04' })).toBe('avr. 2027');
    expect(whenLabel({ not_before: '2027-04-30' })).toBe('après le 30 avr. 2027');
    expect(whenLabel({})).toBe('date non fixée');
    expect(daysUntil('2026-10-22', new Date(Date.UTC(2026, 9, 17)))).toBe(5);
    expect(daysUntil(null, new Date())).toBeNull();
  });

  it('palette: « scores FMI » and « scores énergie » resolve to the filtered scoreboard', () => {
    const cmds = buildCommands();
    const fmi = cmds.find((c) => c.keywords?.includes('scores fmi'));
    expect(fmi?.run('scores fmi')).toBe('/v8/scores?f=imf_weo');
    const nrj = cmds.find((c) => c.keywords?.includes('scores énergie'));
    expect(nrj?.run('scores énergie')).toBe('/v8/scores?d=energy');
    expect(cmds.find((c) => c.id === 'scores.calendar')?.run('')).toBe('/v8/scores?section=calendrier');
    expect(new Set(cmds.map((c) => c.id)).size).toBe(cmds.length);
  });

  it('feature contract: route scores, order 60, g p', () => {
    expect(feature.id).toBe('scores');
    expect(feature.path).toBe('scores');
    expect(feature.order).toBe(60);
    expect(feature.goKey).toBe('p');
    expect(feature.commands?.length).toBeGreaterThan(10);
  });

  it('says why a row is not counted and words a row with its verdict', () => {
    expect(notCountedReason({ benchmark_kind: 'weak_reference' })).toMatch(/repère faible/);
    expect(notCountedReason({ benchmark_kind: 'naive' })).toMatch(/naïve/);
    expect(notCountedReason({ benchmark_kind: 'official_point', quarantined: true })).toMatch(/quarantaine/);
    expect(notCountedReason({ benchmark_kind: 'official_point' })).toBeNull();
    const r = { candidate_label_fr: 'Combinaison', benchmark_label_fr: 'point WEO', verdict: 'bat', metric: 'ae', ratio: 0.63 } as unknown as Row;
    expect(rowSentence(r)).toBe('Combinaison bat point WEO (AE, ×0,63)');
  });
});

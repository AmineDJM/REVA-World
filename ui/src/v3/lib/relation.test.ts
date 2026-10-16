import { describe, expect, it } from 'vitest';
import { PairLinksData } from '../api/links';
import * as F from '../test/fixtures';
import { NODE_RE, parseList, parseSteps, PROP_RE } from './pathOptions';
import { buildRelation, laneMarks, themeOf } from './relation';

describe('relation view model', () => {
  const d = PairLinksData.parse(F.PAIR_BRA_PRT.data);
  const v = buildRelation(d);

  it('names both countries from the KG labels and groups kinds into themes', () => {
    expect([v.aLabel, v.bLabel]).toEqual(['Brésil', 'Portugal']);
    expect(v.themes.map((t) => t.id)).toEqual(['trade', 'votes', 'language', 'treaty']);
    expect(themeOf('co_party')).toBe('treaty');
    expect(themeOf('peacekeeping_contributor_to_host')).toBe('peacekeeping');
    expect(v.span).toEqual([1950, 2025]);
  });

  it('keeps each trade direction a separate series of exact decimal strings (never merged, never zero-filled)', () => {
    const trade = v.themes[0]!.kinds[0]!;
    expect(trade.series.map((s) => `${s.from}→${s.to}:${s.points.map((p) => `${p.year}=${p.value}`).join(',')}`)).toEqual([
      'Brésil→Portugal:2000=379774421,2025=3299445083',
      'Portugal→Brésil:2025=1202294976',
    ]);
  });

  it('vote agreement rows carry the share exactly and its counts', () => {
    const votes = v.themes[1]!.kinds[0]!.votes;
    expect(votes.map((r) => [r.period, r.shareShown, r.nIdentical, r.nCommon])).toEqual([
      ['1950–1959', '0,5989', 112, 187],
      ['1960–1969', '0,3902', 80, 205],
    ]);
  });

  it('places marks on the overview and lists kinds without links', () => {
    const marks = laneMarks(v.themes[3]!.kinds[0]!, v.span!);
    expect(marks).toHaveLength(1);
    expect(marks[0]!.left).toBeCloseTo((52 / 75) * 100);
    expect(marks[0]!.left + marks[0]!.width).toBeCloseTo(100); // open-ended: still party
    expect(v.without.map((w) => w.kind)).toEqual(['migrant_stock', 'land_border']);
    expect(v.predecessors[0]!.succession[0]!.sentence).toBe('Portugal — successeur de → Empire colonial portugais');
  });
});

describe('Chemin advanced options', () => {
  it('parses ordered steps and lists, refusing malformed identifiers', () => {
    expect(parseSteps('P1891, P710 > P37 > link:co_member')).toEqual(['P1891,P710', 'P37', 'link:co_member']);
    expect(parseSteps('untc:party')).toEqual(['untc:party']);
    expect(parseSteps('')).toEqual([]);
    expect(parseSteps('P37 > drop table')).toBeNull();
    expect(parseList('Q155, PRT', NODE_RE)).toEqual(['Q155', 'PRT']);
    expect(parseList('brazil', NODE_RE)).toBeNull();
    expect(parseList('P37 P2936', PROP_RE)).toEqual(['P37', 'P2936']);
  });
});

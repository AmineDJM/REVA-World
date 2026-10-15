import { describe, expect, it } from 'vitest';
import { AtlasMapResponse } from '../api/atlas';
import { KgPathResponse, KgNeighboursResponse, KgTimelineResponse } from '../api/kg';
import { LinksPathResponse, type LinksPathData } from '../api/links';
import * as F from '../test/fixtures';
import { buildScale, countryStates, iso3OfFeatureKey, NO_SOURCE_REASON, ranking, tooltipFor } from './choropleth';
import { citeKgStatement, wikidataStatementUrl } from './citations';
import { frDecimal } from './format';
import { emptyGraph, mergeNeighbours, visibleAt, yearRange } from './graph';
import { connectedSentence, fromKg, fromLinks } from './paths';
import { formatKgTime, formatYear, inForceAt, temporalOf, yearOf } from './time';
import { buildTimeline, ticks, zoomWindow } from './timeline';

const CAUSAL_WORDS = /cause|effet|provoqu|entraîn|because|caused/i;

describe('missing ≠ 0 (atlas choropleth)', () => {
  const m = AtlasMapResponse.parse(F.mapResponse(2022));

  it('countries without a value are gaps with a reason, never coloured, never zero', () => {
    const scale = buildScale(m.data.values);
    const st = countryStates(m.data.values, m.data.countries_without_value, scale);
    for (const iso of ['PRK', 'ERI', 'SOM']) {
      const s = st.get(iso)!;
      expect(s.state).toBe('gap');
      expect(s.state === 'gap' && s.reason).toBe(NO_SOURCE_REASON);
      const tip = tooltipFor(iso, s, 2022);
      expect(tip).toContain('manquante');
      expect(tip).not.toMatch(/\b0\b/);
    }
    expect(st.get('FRA')!.state).toBe('value');
    expect(ranking(m.data.values).map((c) => c.iso3)).not.toContain('PRK');
  });

  it('a reported zero is a value; a cell with no value and a missing flag is a gap', () => {
    const cells = AtlasMapResponse.parse({
      ...F.mapResponse(2022),
      data: { ...F.mapResponse(2022).data, values: [F.cell('AAA', 2022, '0'), F.cell('BBB', 2022, '5'), F.cell('CCC', 2022, null, { flags: { missing: 'confidential' } })] },
    }).data.values;
    const st = countryStates(cells, [], buildScale(cells));
    expect(st.get('AAA')!.state).toBe('value');
    const c = st.get('CCC')!;
    expect(c.state).toBe('gap');
    expect(c.state === 'gap' && c.reason).toBe('confidentiel');
  });

  it('categorical (textual) concepts get a category legend', () => {
    const cells = [F.cell('FRA', 2020, null, { text: 'droit civil' }), F.cell('GBR', 2020, null, { text: 'common law' })];
    const s = buildScale(AtlasMapResponse.parse({ ...F.mapResponse(2020), data: { ...F.mapResponse(2020).data, values: cells } }).data.values);
    expect(s?.kind).toBe('categorical');
  });

  it('maps Natural Earth display keys to crosswalk ISO3 keys (display join only)', () => {
    expect(iso3OfFeatureKey('FRA:43')).toBe('FRA');
    expect(iso3OfFeatureKey('SDS:7')).toBe('SSD');
    expect(iso3OfFeatureKey('KOS:1')).toBe('XKX');
    expect(iso3OfFeatureKey('-99:9')).toBeNull();
  });
});

describe('a path is not a cause', () => {
  const links = LinksPathResponse.parse(F.LINKS_PATH_COL_BRA).data;

  it('ordinary hops say "relié via « propriété »" with no causal wording', () => {
    const v = fromLinks(links);
    const p0 = v.paths[0]!;
    expect(p0.hops.map((h) => h.sentence)).toEqual([connectedSentence('participant à'), connectedSentence('signataire'), connectedSentence('ancienne colonie de')]);
    for (const h of p0.hops) {
      expect(h.causal).toBeNull();
      expect(h.sentence).not.toMatch(CAUSAL_WORDS);
    }
    expect(v.disclaimer).toContain('pas une explication causale');
  });

  it('causal wording only for a source-asserted causal statement, attributed to the source', () => {
    const hop = fromLinks(links).paths[1]!.hops[1]!;
    expect(hop.causal).not.toBeNull();
    expect(hop.sentence).toMatch(/^selon Wikidata \(contribution communautaire, non vérifiée\)/);
    expect(hop.sentence).toContain('non établie par REVA');
  });

  it('a derived link flagged causal, or a causal hop without evidence, is not worded causally', () => {
    const tampered: LinksPathData = structuredClone(links);
    tampered.paths[0]!.hops[2]!.causal = true; // kind "link"
    tampered.paths[1]!.hops[1]!.evidence = [];
    const v = fromLinks(tampered);
    expect(v.paths[0]!.hops[2]!.causal).toBeNull();
    expect(v.paths[1]!.hops[1]!.causal).toBeNull();
    expect(v.paths[1]!.hops[1]!.sentence).toBe(connectedSentence('a pour effet'));
  });

  it('KG engine: causal only with causal_assertion on a has-cause/has-effect property', () => {
    const d = KgPathResponse.parse(F.KG_PATH_COL_BRA).data;
    const v = fromKg(d);
    expect(v.paths[0]!.hops[0]!.causal).toBeNull();
    expect(v.paths[0]!.hops[1]!.causal?.assertedBy).toContain('Wikidata');
    const forged = structuredClone(d);
    forged.paths[0]!.hops[1]!.property.causal_assertion = false;
    expect(fromKg(forged).paths[0]!.hops[1]!.causal).toBeNull();
  });

  it('every hop carries citations; Wikidata links only for Wikidata ids', () => {
    const v = fromLinks(links);
    for (const p of v.paths) for (const h of p.hops) expect(h.citations.length).toBeGreaterThan(0);
    const c = v.paths[0]!.hops[0]!.citations[0]!;
    expect(c.href).toBe('https://www.wikidata.org/wiki/Q7322#Q7322$FIXTURE-L1');
    expect(c.revisionHref).toBe('https://www.wikidata.org/w/index.php?oldid=2250000001');
    expect(c.community).toBe(true);
    expect(c.fixture).toBe(true);
    expect(v.paths[0]!.hops[1]!.via).toEqual([]);
    expect(v.paths[0]!.hops[2]!.via).toEqual(['Brésil colonial']);
  });
});

describe('citations shown for statements', () => {
  it('KG statement citation: statement link, revision, community badge, references count', () => {
    const st = KgNeighboursResponse.parse(F.NEIGHBOURS_Q155).data.edges[0]!;
    const c = citeKgStatement(st);
    expect(c.href).toMatch(/^https:\/\/www\.wikidata\.org\/wiki\/Q155#Q155\$FIXTURE-/);
    expect(c.revision).toBe('2250000000');
    expect(c.community).toBe(true);
    expect(c.details.join(' ')).toMatch(/référence/);
  });

  it('no Wikidata URL for provider-scoped keys', () => {
    expect(wikidataStatementUrl('ucdp:conflict:333$x', 'ucdp:conflict:333')).toBeNull();
  });
});

describe('time', () => {
  it('parses astronomical years and formats BCE historically', () => {
    expect(yearOf('1494-06-07T00:00:00Z')).toBe(1494);
    expect(yearOf('-0043-03-15T00:00:00Z')).toBe(-43);
    expect(yearOf('0044-03-15 BC')).toBe(-43);
    expect(formatYear(-43)).toBe('44 av. J.-C.');
    expect(formatKgTime({ time: '1494-06-07T00:00:00Z', precision: 11 })).toBe('7 juin 1494');
    expect(formatKgTime({ time: '1450-01-01T00:00:00Z', precision: 7 })).toBe('XVe siècle');
  });

  it('"as of" semantics: intervals contain the year, events have happened, undated only when included', () => {
    expect(inForceAt({ kind: 'interval', from: 1945, to: null }, 1950, false)).toBe(true);
    expect(inForceAt({ kind: 'interval', from: 1945, to: null }, 1900, false)).toBe(false);
    expect(inForceAt({ kind: 'point', at: 1822 }, 1900, false)).toBe(true);
    expect(inForceAt({ kind: 'undated' }, 1900, false)).toBe(false);
    expect(inForceAt({ kind: 'undated' }, 1900, true)).toBe(true);
    expect(temporalOf({ start: null, end: null, point_in_time: null, valid_from: null, valid_to: null }).kind).toBe('undated');
  });
});

describe('graph model', () => {
  it('merges neighbours, keeps provenance, and dims edges outside the year', () => {
    const n = KgNeighboursResponse.parse(F.NEIGHBOURS_Q155);
    const r = mergeNeighbours(emptyGraph(), { qid: 'Q155', label: 'Brazil' }, n.data.edges, null);
    expect(r.graph.nodes.size).toBe(5);
    expect(r.graph.edges.size).toBe(4);
    const again = mergeNeighbours(r.graph, { qid: 'Q155', label: 'Brazil' }, n.data.edges, null);
    expect(again.addedEdges).toHaveLength(0);
    const v1900 = visibleAt(r.graph, 1900, false, 'Q155');
    // UN membership starts 1945 -> hidden in 1900; independence (1822) visible; undated hidden.
    expect([...v1900.edges].length).toBe(1);
    expect(v1900.nodes.has('Q1065')).toBe(false);
    expect(visibleAt(r.graph, null, false, 'Q155').edges.size).toBe(4);
    expect(yearRange(r.graph)).toEqual([1822, 1945]);
  });
});

describe('timeline', () => {
  it('places dated items, keeps open ends, never invents dates', () => {
    const t = buildTimeline('Q155', KgTimelineResponse.parse(F.TIMELINE_Q155).data.events, 2026);
    expect(t.items.length).toBe(5);
    const head = t.items.find((i) => i.lane === 'chef de l’État')!;
    expect(head.openEnd).toBe(true);
    expect(head.end).toBe(2026);
    expect(ticks(1800, 2000).every((x) => x % 25 === 0)).toBe(true);
    expect(zoomWindow(1800, 2000, 0.5, 1900, 1500, 2026)).toEqual([1850, 1950]);
  });
});

describe('French decimals', () => {
  it('formats exact strings without float rounding', () => {
    expect(frDecimal('57082396439.6', 2)).toBe('57 082 396 439,6');
    expect(frDecimal('-0.0004', 3)).toBe('0');
    expect(frDecimal('1999.9996', 3)).toBe('2 000');
    expect(frDecimal('0', 2)).toBe('0');
  });
});

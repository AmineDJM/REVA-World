import { describe, expect, it } from 'vitest';
import { PairLinksData } from '../api/links';
import * as F from '../test/fixtures';
import { NODE_RE, parseList, parseSteps, PROP_RE } from './pathOptions';
import { buildRelation, laneMarks, physicalDetail, themeOf } from './relation';

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

describe('physical networks (LINKS3)', () => {
  const ev = (pid: string, extra: Record<string, unknown> = {}) => ({ kind: 'atlas_row', source: 'wikidata', raw_sha256: 'a'.repeat(64), selector: '{"aggregate":"x"}', pid, origin: 'test_fixture', ...extra });
  const meta = (fr: string, directed: boolean) => ({ labels: { fr, en: fr }, inverse_labels: { fr, en: fr }, honesty: { fr: `relié via : ${fr}`, en: fr }, directed, status: 'derivable', temporal: 'snapshot', weight: { kind: 'source_quantity', unit: null }, sources: [] });
  const CAVEAT_C = { fr: 'Contribution communautaire Wikidata, non vérifiée.', en: 'Community-edited.' };
  const CAVEAT_A = { fr: 'Données figées : juin 2014. Licence ODbL 1.0.', en: 'Frozen: June 2014.' };
  const d = PairLinksData.parse({
    a: 'GBR',
    b: 'NLD',
    store: { materialisation_id: 3 },
    kinds: {
      submarine_cable: {
        ...meta('câbles sous-marins listant les deux pays (Wikidata)', false),
        count: 1,
        first_year: 2026,
        last_year: 2026,
        links: [
          {
            subject: 'Q145',
            object: 'Q29999',
            link_type: 'submarine_cable',
            directed: false,
            temporal: { kind: 'snapshot', year: 2026 },
            evidence: [
              ev('CABLES_SHARED|partner=GBR', { note: 'Wikidata cable items: Concerto 1 (Q2490425, rev 1)' }),
              { kind: 'kg_statement', source: 'wikidata', statement_id: 'q55$3F58B793-57C8-4C66-87ED-403C7E298EBF', subject: 'Q55', pid: 'P17', object: 'Q29999', origin: 'test_fixture' },
            ],
            weight: '2',
            unit: 'cables',
            iso3: ['GBR', 'NLD'],
            basis: {
              first_service_year: 1999,
              caveat: CAVEAT_C,
              licence: 'CC0-1.0 (Wikidata)',
              source_flags: { cables: [{ qid: 'Q2490425', label: 'Concerto 1' }, { qid: 'Q429472', label: 'REMBRANDT-2' }], cables_total: 2 },
              mapping_hop: { provider_code: 'Q55', provider_label: { fr: 'Pays-Bas (pays constitutif, Q55)' }, iso3: 'NLD', target_qid: 'Q29999', statement_id: 'q55$3F58B793-57C8-4C66-87ED-403C7E298EBF' },
            },
          },
        ],
      },
      air_routes_2014: {
        ...meta('liaisons aériennes directes recensées en juin 2014 (OpenFlights, données figées)', true),
        count: 1,
        first_year: 2014,
        last_year: 2014,
        links: [
          { subject: 'Q145', object: 'Q29999', link_type: 'air_routes_2014', directed: true, temporal: { kind: 'snapshot', year: 2014 }, evidence: [ev('ROUTES_OPERATED|partner=NLD', { source: 'openflights' })], weight: '41', unit: 'routes', iso3: ['GBR', 'NLD'], basis: { airport_pairs: 30, airlines: 6, caveat: CAVEAT_A, licence: 'ODbL-1.0' } },
        ],
      },
    },
    kinds_without_links: ['launch_service'],
    predecessor_links: [],
    labels: { Q145: { fr: 'Royaume-Uni' }, Q29999: { fr: 'Pays-Bas' } },
    truncated: [],
  });
  const v = buildRelation(d);

  it('groups cables, 2014 air routes and launches in one theme, with caveats and licence shown once per kind', () => {
    expect(themeOf('submarine_cable')).toBe('infrastructure');
    expect(themeOf('launch_service')).toBe('infrastructure');
    const t = v.themes.find((x) => x.id === 'infrastructure')!;
    const cab = t.kinds.find((k) => k.kind === 'submarine_cable')!;
    expect(cab.notes).toEqual([CAVEAT_C.fr, 'Licence : CC0-1.0 (Wikidata)']);
    expect(t.kinds.find((k) => k.kind === 'air_routes_2014')!.notes[0]).toContain('juin 2014');
    expect(v.without.map((w) => w.kind)).toEqual(['launch_service']);
  });

  it('names the cables, their earliest service year and the cited Q55 hop; the lane starts at that year', () => {
    const cab = v.themes.find((x) => x.id === 'infrastructure')!.kinds.find((k) => k.kind === 'submarine_cable')!;
    const it0 = cab.items[0]!;
    expect(it0.sentence).toContain('Royaume-Uni — câbles sous-marins listant les deux pays (Wikidata) — Pays-Bas : Concerto 1, REMBRANDT-2');
    expect(it0.sentence).toContain('mise en service la plus ancienne : 1999');
    expect(it0.sentence).toContain('via Pays-Bas (pays constitutif, Q55) — pays (P17) → Q29999 [q55$3F58B793');
    expect([it0.yearFrom, it0.yearTo]).toEqual([1999, 2026]);
    expect(it0.citations).toHaveLength(2);
    expect(it0.value).toContain('2');
  });

  it('air routes say June 2014 and their counts, as a directed item', () => {
    const air = v.themes.find((x) => x.id === 'infrastructure')!.kinds.find((k) => k.kind === 'air_routes_2014')!;
    expect(air.series).toEqual([]);
    expect(air.items[0]!.sentence).toBe('Royaume-Uni — liaisons aériennes directes recensées en juin 2014 (OpenFlights, données figées) → Pays-Bas : 30 paire(s) d’aéroports ; 6 compagnie(s) ; réseau de juin 2014, données figées');
    expect(physicalDetail(d.kinds.air_routes_2014!.links[0]!).since).toBeNull();
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

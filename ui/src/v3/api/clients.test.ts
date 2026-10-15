import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFetch } from '../../test/fetchMock';
import * as F from '../test/fixtures';
import { getAtlasConcepts, getAtlasCoverage, getAtlasDossier, getAtlasMap } from './atlas';
import { postAsk } from './ask';
import { classifyV3Error, ContractError } from './common';
import { getKgEntity, getKgNeighbours, getKgPath, getKgTimeline, searchKg } from './kg';
import { getCountryLinks, getLinksPath } from './links';

afterEach(() => vi.unstubAllGlobals());

async function failure(p: Promise<unknown>): Promise<unknown> {
  try {
    await p;
  } catch (e) {
    return e;
  }
  throw new Error('expected a failure');
}

describe('V3 API clients parse the real route shapes', () => {
  it('knowledge graph: entity, neighbours, timeline, search, path', async () => {
    const { calls } = mockFetch({
      'GET /v1/kg/entities/Q155/neighbours': { body: F.NEIGHBOURS_Q155 },
      'GET /v1/kg/entities/Q155/timeline': { body: F.TIMELINE_Q155 },
      'GET /v1/kg/entities/Q155': { body: F.envelope(F.ENTITIES.Q155) },
      'GET /v1/kg/search': { body: F.SEARCH_BRESIL },
      'GET /v1/kg/path': { body: F.KG_PATH_COL_BRA },
    });
    const e = await getKgEntity('Q155');
    expect(e.data.labels.fr).toBe('Brésil');
    expect(e.data.provenance?.verification).toBe('community_edited_not_verified');
    const n = await getKgNeighbours('Q155');
    expect(n.data.edges).toHaveLength(4);
    expect(n.data.edges[0]!.provenance.statement_id).toMatch(/^Q155\$/);
    expect(n.warnings[0]!.code).toBe('community_edited');
    const t = await getKgTimeline('Q155');
    expect(t.data.events.length).toBeGreaterThan(3);
    const s = await searchKg('Brés');
    expect(s.data.results[0]!.qid).toBe('Q155');
    const p = await getKgPath({ from: 'Q7322', to: 'Q155', max_hops: 4 });
    expect(p.data.paths[0]!.hops[1]!.causal_assertion).toEqual(expect.objectContaining({ asserted_by: 'wikidata' }));
    expect(calls.find((c) => c.url.startsWith('/v1/kg/entities/Q155/neighbours'))!.url).toContain('direction=both');
    expect(calls.find((c) => c.url.startsWith('/v1/kg/search'))!.url).toContain('lang=fr&lang=en');
  });

  it('atlas: concepts, dossier, map, coverage', async () => {
    mockFetch({
      'GET /v1/atlas/concepts': { body: F.CONCEPTS },
      'GET /v1/atlas/countries/FRA': { body: F.DOSSIER_FRA },
      'GET /v1/atlas/map': { body: F.mapResponse(2022) },
      'GET /v1/atlas/coverage': { body: F.COVERAGE_UNEMP },
    });
    expect((await getAtlasConcepts()).data.map((c) => c.id)).toContain('labour.unemployment_rate');
    const d = await getAtlasDossier('fra');
    expect(d.data.domains[0]!.without_data).toEqual(['labour.minimum_wage_monthly']);
    const m = await getAtlasMap('labour.unemployment_rate', 2022);
    expect(m.data.values[0]!.value).toBe('7.9');
    expect(m.data.countries_without_value).toEqual(['PRK', 'ERI', 'SOM']);
    expect((await getAtlasCoverage({ concept: 'labour.unemployment_rate' })).data[0]!.year_max).toBe(2022);
  });

  it('links and ask', async () => {
    const { calls } = mockFetch({
      'GET /v1/links/path': { body: F.LINKS_PATH_COL_BRA },
      'GET /v1/links/FRA': { body: F.LINKS_FRA },
      'POST /v1/ask': { body: F.ASK_BRESIL },
    });
    const p = await getLinksPath({ from: 'Q7322', to: 'Q155', k: 3, as_of: 1500 });
    expect(p.data.paths).toHaveLength(2);
    expect(p.warnings[0]!.code).toBe('path_not_causal');
    expect((await getCountryLinks('FRA')).data.links).toHaveLength(2);
    const a = await postAsk('Quelle est la langue officielle du Brésil ?');
    expect(a.sentences[0]!.citations).toEqual(['E1']);
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.body).toEqual({ question: 'Quelle est la langue officielle du Brésil ?', lang: 'fr' });
    expect(calls.find((c) => c.url.startsWith('/v1/links/path'))!.url).toContain('as_of=1500');
  });
});

describe('honest failures', () => {
  it('an atlas value served as a JSON number is refused (a filled zero cannot sneak in)', async () => {
    const bad = F.mapResponse(2022);
    (bad.data.values[0] as Record<string, unknown>).value = 0;
    mockFetch({ 'GET /v1/atlas/map': { body: bad } });
    const e = await failure(getAtlasMap('labour.unemployment_rate', 2022));
    expect(e).toBeInstanceOf(ContractError);
    expect((e as ContractError).issues.join(' ')).toContain('data.values.0.value');
    expect(classifyV3Error(e).kind).toBe('contract');
  });

  it('a KG statement without provenance is refused', async () => {
    const bad = structuredClone(F.NEIGHBOURS_Q155);
    delete (bad.data.edges[0] as Record<string, unknown>).provenance;
    mockFetch({ 'GET /v1/kg/entities/Q155/neighbours': { body: bad } });
    expect(await failure(getKgNeighbours('Q155'))).toBeInstanceOf(ContractError);
  });

  it('classifies 404 route / 501 as "pas encore disponible", unknown entity as not_found, no snapshot as no_data', async () => {
    mockFetch({
      'GET /v1/kg/search': { status: 404, body: F.NOT_FOUND_ROUTE },
      'GET /v1/kg/entities/Q999': { status: 404, body: { title: 'Not Found', status: 404, detail: 'entity not found in the knowledge graph', code: 'not_found' } },
      'GET /v1/atlas/map': { status: 404, body: { title: 'Not Found', status: 404, detail: 'no atlas snapshot recorded', code: 'atlas_snapshot_unavailable' } },
      'GET /v1/links/path': { status: 501, body: { title: 'Not Implemented', status: 501, detail: 'later' } },
      'POST /v1/ask': { status: 429, body: { title: 'Too Many Requests', status: 429, detail: 'retry later', code: 'rate_limited' } },
    });
    expect(classifyV3Error(await failure(searchKg('x'))).kind).toBe('unavailable');
    expect(classifyV3Error(await failure(getKgEntity('Q999'))).kind).toBe('not_found');
    expect(classifyV3Error(await failure(getAtlasMap('x', 2020))).kind).toBe('no_data');
    expect(classifyV3Error(await failure(getLinksPath({ from: 'Q1', to: 'Q2' }))).kind).toBe('unavailable');
    expect(classifyV3Error(await failure(postAsk('q'))).kind).toBe('rate_limited');
  });
});

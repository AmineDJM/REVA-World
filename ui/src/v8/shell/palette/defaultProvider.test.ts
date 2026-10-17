import { envelope, mockFetch } from '../../../test/fetchMock';
import { defaultSearch, resetSearchBackend, searchBackend } from './defaultProvider';

const kg = envelope({ query: 'fra', results: [{ qid: 'Q142', kind: 'country', labels: { fr: 'France', en: 'France' }, matched: [] }, { qid: 'Q90', kind: null, labels: { fr: 'Paris' }, matched: [] }] });
const concepts = {
  data: [
    { id: 'gdp_current_usd', labels: { fr: 'PIB (US$ courants)', en: 'GDP' }, unit: 'US$', domain: 'economie', source_order: ['worldbank_wdi.NY.GDP.MKTP.CD'] },
    { id: 'fraction_x', labels: { fr: 'Fraction rurale' }, unit: '%', domain: 'population', source_order: [] },
  ],
  total: 2,
};

describe('default palette provider', () => {
  beforeEach(() => resetSearchBackend());

  it('uses /v1/v8/search when present', async () => {
    const { calls } = mockFetch({
      'GET /v1/v8/search': { body: { data: { hits: [{ id: 'FRA', kind: 'country', label: 'France', sublabel: 'Pays · FRA', href: '/v8/fiche/country/FRA', group: 'Pays & régions' }] } } },
    });
    const hits = await defaultSearch('fra', new AbortController().signal);
    expect(hits).toEqual([{ id: 'country:FRA', label: 'France', sublabel: 'Pays · FRA', href: '/v8/fiche/country/FRA', group: 'Pays & régions' }]);
    expect(calls[0]!.url).toMatch(/\/v1\/v8\/search\?q=fra/);
    expect(searchBackend()).toBe('v8');
  });

  it('falls back silently to /v1/kg/search + atlas catalogue when the route is absent (404), and stays there', async () => {
    const { calls } = mockFetch({
      'GET /v1/v8/search': { status: 404, body: { title: 'Not Found', status: 404, detail: 'Not Found' } },
      'GET /v1/kg/search': { body: kg },
      'GET /v1/atlas/concepts': { body: concepts },
    });
    const hits = await defaultSearch('fra', new AbortController().signal);
    expect(searchBackend()).toBe('fallback');
    expect(hits.find((h) => h.label === 'France')).toMatchObject({ href: '/v8/fiche/country/Q142', group: 'Pays & régions' });
    expect(hits.find((h) => h.label === 'Paris')).toMatchObject({ href: '/v8/fiche/entity/Q90', group: 'Entités' });
    expect(hits.find((h) => h.group === 'Indicateurs')!.label).toBe('Fraction rurale');
    await defaultSearch('pib', new AbortController().signal);
    expect(calls.filter((c) => c.url.startsWith('/v1/v8/search')).length).toBe(1);
  });

  it('propagates aborts', async () => {
    mockFetch({ 'GET /v1/v8/search': { body: { data: { hits: [] } } } });
    const c = new AbortController();
    c.abort();
    globalThis.fetch = vi.fn(async () => {
      throw new DOMException('aborted', 'AbortError');
    }) as typeof fetch;
    await expect(defaultSearch('x', c.signal)).rejects.toThrow(/aborted/);
  });
});

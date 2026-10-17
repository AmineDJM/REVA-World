/**
 * UICHECK (docs/dev/v3_uicheck.md): the causality screen on responses recorded from the dev database (origin
 * live, real_samples.fixture.json) — attribution (P3680), entities dated by inception, coded outcomes, licences,
 * duplicated chain paths, large neighbourhoods.
 */
import { configure, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFetch } from '../../test/fetchMock';
import { PID_INVERSE_LABEL_FR, PID_LABEL_FR } from '../lib/paths';
import V3App from '../V3App';
import { CausalClaim, getCausalChain, notFoundReasonFr } from './api';
import { attributedTo, buildGraph, groupWhy, ticks } from './model';
import { normaliseRights } from './rights';
import REAL from './real_samples.fixture.json';

vi.mock('./CausalGraph', () => ({
  default: ({ label, model }: { label: string; model: { edges: { id: string }[]; nodes: { id: string; side: string }[] } }) => (
    <div role="img" aria-label={label} data-testid="causal-canvas" data-edges={model.edges.length} data-outcomes={model.nodes.filter((n) => n.side === 'outcome').length} />
  ),
}));

configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });
afterEach(() => vi.unstubAllGlobals());

const env = (data: unknown) => ({ data, context_mode: 'evidence', origin: 'live', modality: 'reported_observation', knowledge_cutoff: null, warnings: [], next_cursor: null });
type Raw = Record<string, unknown> & { cause: { id: string; label: string }; effect: { id: string; label: string } };
const A = REAL.attributed_claim as unknown as Raw;
const INC = REAL.inception_claim as unknown as Raw;
const NAT = REAL.nature_claim as unknown as Raw;
const OUT = REAL.outcome_claim as unknown as Raw;

function at(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/v3/*" element={<V3App />} />
      </Routes>
    </MemoryRouter>,
  );
}

function routesFor(id: string, causes: unknown[], effects: unknown[]) {
  return {
    [`GET /v1/causal/events/${encodeURIComponent(id)}/causes`]: { body: env({ id, claims: causes, truncated: false }) },
    [`GET /v1/causal/events/${encodeURIComponent(id)}/effects`]: { body: env({ id, claims: effects, truncated: false }) },
    [`GET /v1/causal/events/${encodeURIComponent(id)}/why`]: { body: env({ id, lanes: [{ lane: 'general', claims: causes }] }) },
    [`GET /v1/causal/events/${encodeURIComponent(id)}`]: {
      body: env({ id, event: { id, label: id, label_fr: null, kind: null }, dates: { start: null, end: null }, causes: {}, effects: {}, context: [], candidates: 0 }),
    },
    'GET /v1/sources': { body: REAL.sources },
  };
}

describe('real claim JSON (dev DB, grading v2)', () => {
  it('P3680 attribution: Wikidata records that a party asserts it — never "selon Wikidata" alone', () => {
    const c = CausalClaim.parse(A);
    expect(c.attribution?.supportedBy.map((p) => p.id)).toEqual(['Q7747', 'Q1140115']);
    expect(attributedTo(c)).toBe('Wikidata indique que Q7747 et Q1140115 affirment ce lien ; Wikidata ne l’affirme pas lui-même');
    expect(c.gradeRules).toContain('grade.D.attributed');
  });

  it('P5102 nature of statement is kept verbatim (no attribution to a party)', () => {
    const c = CausalClaim.parse(NAT);
    expect(c.attribution?.supportedBy).toEqual([]);
    expect(c.attribution?.nature.map((p) => p.id)).toEqual(['Q20820099']);
  });

  it('a source’s own doubtful coding (scope.source_validity, e.g. NOAA validity) is read; none is loaded in the dev DB yet', () => {
    const c = CausalClaim.parse({ ...OUT, scope: { source_validity: [{ field: 'validity', code: '1', label: 'very doubtful tsunami', certainty: 'doubtful' }] } });
    expect(c.attribution?.doubtful).toEqual([{ field: 'validity', code: '1', label: 'very doubtful tsunami' }]);
    expect(CausalClaim.parse(OUT).attribution).toBeNull();
  });

  it('an effect dated by its inception is compared by its existence span, undetermined, with the reason in French', () => {
    const c = CausalClaim.parse(INC);
    expect(c.temporal.status).toBe('undetermined');
    expect(c.temporal.entityDated).toEqual(['effect']);
    expect(c.temporal.detail).toMatch(/^entité datée par sa création \(P571\), pas un événement/);
    expect(c.temporal.effectTime).toMatchObject({ kind: 'entity_existence', inception: '1871-01-01', end: '1918-11-09' });
    expect(c.assertedOn).toBe('cause');
    expect(c.source.property).toBe('P1542');
  });

  it('coded_outcome is an outcome: never in a causal lane of "Pourquoi ?", drawn apart in the graph', () => {
    const c = CausalClaim.parse(OUT);
    const g = groupWhy([c]);
    expect(g.coded).toEqual([]);
    expect(g.outcome.map((x) => x.relation)).toEqual(['coded_outcome']);
    const graph = buildGraph({ id: c.cause.id, label: c.cause.label }, [], [c]);
    expect(graph.nodes.find((n) => n.id === c.effect.id)?.side).toBe('outcome');
  });

  it('licence terms from the source registry (same rule as the atlas)', () => {
    const r = (k: string) => normaliseRights(k, (REAL.sources.data.find((s) => s.source_key === k) as { rights: unknown }).rights)!;
    expect(r('idmc.gidd')).toMatchObject({ non_commercial: true, restricted: true, commercial_use: 'not_permitted' });
    expect(r('wikidata.wbapi')).toMatchObject({ non_commercial: false, restricted: false });
    expect(r('cow.data')).toMatchObject({ commercial_use: 'restricted', restricted: true });
    expect(r('icb.data')).toMatchObject({ open_licence: false, restricted: true });
    expect(r('noaa.ncei_hazards').restricted).toBe(false);
  });

  it('a chain path the server lists twice is shown once, and the merge is counted', async () => {
    mockFetch({ 'GET /v1/causal/chain': { body: REAL.chain_duplicated_paths } });
    const r = await getCausalChain('ties:case:1990122101', 'eusanct:policy_result:1990122101');
    expect(r.chains).toHaveLength(1);
    expect(r.duplicatesMerged).toBe(1);
    expect(r.maxHops).toBe(4);
    const id = r.chains[0]!.hops[0]!.identity![0]!;
    expect(id.rule).toBe('eusanct_ties_case');
    expect(id.note).toMatch(/TIES case id/);
  });

  it('server not-found reasons are given in French', () => {
    expect(notFoundReasonFr('no causal chain within max_hops among claims graded at least D')).toBe(
      'aucune chaîne d’affirmations causales dans la limite de maillons parmi les affirmations de grade D ou mieux',
    );
    expect(notFoundReasonFr('something new')).toBe('something new');
  });

  it('time axis ticks are whole years; Wikidata causal property labels read in their own direction', () => {
    expect(ticks(1961, 1963).every(Number.isInteger)).toBe(true);
    expect(PID_LABEL_FR.P1536).toBe('cause immédiate de');
    expect(PID_LABEL_FR.P1537).toBe('facteur contributif de');
    expect(PID_LABEL_FR.P1479).toBe('a pour facteur contributif');
    // a P1542 statement "WWI has effect Austria-Hungary" traversed backwards reads "Austria-Hungary is an effect of WWI"
    expect(PID_INVERSE_LABEL_FR.P1542).toBe('est un effet de');
  });
});

describe('Causes & conséquences on real claims', () => {
  it('an attributed claim says who asserts it, and that Wikidata does not', async () => {
    mockFetch(routesFor('Q15860072', [A], []));
    at('/v3/causes?event=Q15860072');
    const list = await screen.findByTestId('causes-list');
    const line = within(list).getByTestId('claim-line');
    expect(within(line).getByTestId('selon')).toHaveTextContent('selon Wikidata (T4)');
    expect(within(line).getByTestId('claim-attributed')).toHaveTextContent('attribuée à Q7747 et Q1140115 — Wikidata ne l’affirme pas lui-même');
    // the focal event is named from the claims when the KG has no entity for it
    expect(await screen.findByTestId('event-card')).toHaveTextContent('guerre russo-ukrainienne');
    await userEvent.click(within(line).getByRole('button', { name: /Détails/ }));
    const d = await screen.findByTestId('claim-detail');
    expect(within(d).getByTestId('claim-attribution')).toHaveTextContent('Selon Wikidata (T4), affirmation attribuée à Q7747 et Q1140115');
    expect(within(d).getByTestId('claim-attribution')).toHaveTextContent('Wikidata ne l’affirme pas lui-même');
    expect(within(d).getByTestId('claim-wording')).toHaveTextContent('indique que Q7747 et Q1140115 affirment que');
    expect(within(d).getByTestId('claim-grade')).toHaveTextContent('grade.D.attributed');
    await waitFor(() => expect(within(d).getByTestId('claim-licence')).toHaveTextContent('CC0-1.0'));
  });

  it('a coded outcome is listed as an outcome, not as a consequence; the source licence is visible', async () => {
    mockFetch(routesFor('icb:crisis:196', [], [OUT]));
    at('/v3/causes?event=icb%3Acrisis%3A196');
    const effects = await screen.findByTestId('effects-list');
    expect(effects).toHaveTextContent('Aucune source chargée n’affirme de conséquence de cet événement.');
    const outcomes = screen.getByTestId('outcomes-list');
    expect(outcomes).toHaveTextContent('Issues codées (comment l’événement s’est terminé)');
    expect(outcomes).toHaveTextContent('issue, pas une cause');
    expect(screen.getByTestId('causal-canvas')).toHaveAttribute('data-outcomes', '1');
    expect(screen.getByTestId('event-counts')).toHaveTextContent('0 cause(s) · 0 conséquence(s) affirmées · 1 issue(s) codée(s)');
    expect(await within(outcomes).findByTestId('licence-badge')).toHaveTextContent('licence non ouverte');
    expect(screen.getByTestId('licence-list')).toHaveTextContent('icb.data');
    // "Pourquoi ?" has no cause for it: the absence of a claim is said as such
    expect(screen.getByTestId('why-empty')).toHaveTextContent('Aucune source chargée n’affirme de cause pour cet événement');
  });

  it('the outcome record itself: "Pourquoi ?" shows what it is the outcome of, never as a cause', async () => {
    mockFetch(routesFor('icb:crisis:196:outcome', [OUT], []));
    at('/v3/causes?event=icb%3Acrisis%3A196%3Aoutcome');
    const why = await screen.findByTestId('why-panel');
    expect(within(why).getByTestId('why-outcome')).toHaveTextContent('pas une cause');
    expect(within(why).queryByTestId('why-coded')).toBeNull();
    expect(within(why).getByTestId('why-empty')).toBeInTheDocument();
    expect(screen.getByTestId('outcome-of-list')).toHaveTextContent('CUBAN MISSILES');
    expect(screen.getByTestId('causes-list')).toHaveTextContent('Aucune source chargée n’affirme de cause');
  });

  it('a large neighbourhood is paged (lists) and bounded (graph, frise); nothing is dropped silently', async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({
      ...OUT,
      claim_key: `${String(OUT.claim_key)}:copy${i}`,
      relation: 'has_effect',
      relation_label: { en: 'has effect', fr: 'a pour effet' },
      effect: { id: `fixture:effect:${i}`, label: `Effet ${i}`, label_fr: null, kind: null },
    }));
    mockFetch(routesFor('icb:crisis:196', [], many));
    at('/v3/causes?event=icb%3Acrisis%3A196');
    const list = await screen.findByTestId('effects-list');
    expect(within(list).getAllByTestId('claim-line')).toHaveLength(25);
    await userEvent.click(within(list).getByTestId('claims-more'));
    expect(within(list).getAllByTestId('claim-line')).toHaveLength(50);
    expect(within(list).getByTestId('claims-more')).toHaveTextContent('afficher 10 de plus (10 restantes)');
    expect(screen.getByTestId('graph-hidden')).toHaveTextContent('20 affirmation(s) non dessinée(s)');
    expect(screen.getByTestId('timeline-not-drawn')).toHaveTextContent('30 conséquence(s) ou issue(s) non dessinée(s)');
  });

  it('chain: duplicated paths merged and counted, visible "composition par REVA" wording through identity', async () => {
    mockFetch({ ...routesFor('ties:case:1990122101', [], []), 'GET /v1/causal/chain': { body: REAL.chain_duplicated_paths } });
    at('/v3/causes?event=ties%3Acase%3A1990122101&vers=eusanct%3Apolicy_result%3A1990122101');
    const finder = await screen.findByTestId('chain-finder');
    const chains = await within(finder).findAllByTestId('causal-chain');
    expect(chains).toHaveLength(1);
    expect(within(finder).getByTestId('chain-duplicates')).toHaveTextContent('1 chemin(s) identique(s)');
    expect(within(chains[0]!).getByTestId('chain-wording')).toHaveTextContent(
      'Composition par REVA de 1 affirmation(s) énoncée(s) séparément, à travers 1 lien(s) d’identité (même événement dans deux sources) ; aucune source n’affirme cette chaîne.',
    );
    expect(within(chains[0]!).getByTestId('chain-identity-hop')).toHaveTextContent('règle eusanct_ties_case');
  });
});

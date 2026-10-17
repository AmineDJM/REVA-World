import { configure, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFetch } from '../../test/fetchMock';
import V3App from '../V3App';
import * as F from './fixtures';
import ASK from './ask_causal.fixture.json';

// Cytoscape is not exercised in jsdom; its honest text alternative and the edge-tap callback are.
vi.mock('./CausalGraph', () => ({
  default: ({ label, model, onSelectClaim }: { label: string; model: { edges: { id: string }[] }; onSelectClaim: (id: string) => void }) => (
    <div role="img" aria-label={label} data-testid="causal-canvas" data-edges={model.edges.length}>
      {model.edges.map((e) => (
        <button key={e.id} type="button" onClick={() => onSelectClaim(e.id)}>
          edge-{e.id}
        </button>
      ))}
    </div>
  ),
}));

configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });
afterEach(() => vi.unstubAllGlobals());

function at(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/v3/*" element={<V3App />} />
      </Routes>
    </MemoryRouter>,
  );
}

const ROUTES = {
  'GET /v1/causal/events/Q361/causes': { body: F.CAUSES_WW1 },
  'GET /v1/causal/events/Q361/effects': { body: F.EFFECTS_WW1 },
  'GET /v1/causal/events/Q361/why': { body: F.WHY_WW1 },
  'GET /v1/causal/events/Q361': { body: F.EVENT_WW1 },
  'GET /v1/causal/chain': { body: F.CHAIN_SARAJEVO_VERSAILLES },
  'GET /v1/causal/candidates': { body: F.CANDIDATES_WW1 },
};

describe('Causes & conséquences', () => {
  it('shows the event card, the graded graph (denials never drawn) and every claim attributed to its source', async () => {
    mockFetch(ROUTES);
    at('/v3/causes?event=Q361');
    const card = await screen.findByTestId('event-card');
    expect(within(card).getByRole('heading', { name: 'Première Guerre mondiale' })).toBeInTheDocument();
    expect(within(card).getByTestId('event-type')).toHaveTextContent('guerre');
    expect(within(card).getByTestId('event-dates')).toHaveTextContent('28 juil. 1914 – 11 nov. 1918');
    expect(within(card).getByTestId('event-dates')).toHaveTextContent('début au jour');
    expect(card).toHaveTextContent('Empire allemand — participant');
    expect(card).toHaveTextContent('Europe');
    expect(card).toHaveTextContent('8 cause(s) · 3 conséquence(s) affirmées · 2 candidat(s) en quarantaine');
    expect(within(card).getByTestId('event-context')).toHaveTextContent('relation de contexte, pas une cause');
    const canvas = await screen.findByTestId('causal-canvas');
    // 8 causes (one is a denial → not an edge) + 3 effects
    expect(canvas).toHaveAttribute('data-edges', '10');
    expect(canvas).toHaveAttribute('aria-label', expect.stringMatching(/8 cause\(s\) et 3 conséquence\(s\) affirmées par des sources/));
    expect(screen.getByTestId('causal-claim-banner')).toHaveTextContent('Un lien causal est une affirmation, jamais un fait établi par REVA.');
    expect(screen.getByTestId('causal-legend')).toHaveTextContent('cause immédiate (déclencheur)');
    expect(screen.getByTestId('causal-legend')).toHaveTextContent('Un grade n’est pas une probabilité.');
    const causes = screen.getByTestId('causes-list');
    for (const line of within(causes).getAllByTestId('claim-line')) expect(within(line).getByTestId('selon')).toHaveTextContent(/^selon .+ \(T[1-4]\)$/);
    expect(within(causes).getByText(/lien nié/)).toBeInTheDocument();
    expect(within(causes).getAllByTestId('grade-badge').map((b) => b.getAttribute('data-grade'))).toContain('none');
  });

  it('the event card prefers the EVENTS card (type, dated interval, places and participants with roles) when deployed', async () => {
    mockFetch({ ...ROUTES, 'GET /v1/events/Q361': { body: F.EVENTS_CARD_WW1 } });
    at('/v3/causes?event=Q361');
    const card = await screen.findByTestId('event-card');
    await waitFor(() => expect(within(card).getByTestId('event-type')).toHaveTextContent('guerre mondiale'));
    expect(within(card).getByTestId('event-dates')).toHaveTextContent('28 juil. 1914 – 11 nov. 1918');
    expect(within(card).getByTestId('event-places')).toHaveTextContent('Europe (location)');
    expect(within(card).getByTestId('event-participants')).toHaveTextContent('France — belligérant, camp Alliés');
    expect(card).toHaveTextContent('8 cause(s) · 3 conséquence(s) affirmées');
  });

  it('a clicked edge opens its claim: tier, source, statement + revision, verbatim mechanism, temporal check, corroboration, contested-by', async () => {
    mockFetch(ROUTES);
    at('/v3/causes?event=Q361');
    await userEvent.click(await screen.findByRole('button', { name: 'edge-ds:icb.data:icb2:2:trigger' }));
    const d = await screen.findByTestId('claim-detail');
    expect(within(d).getByTestId('claim-attribution')).toHaveTextContent('Affirmé selon icb.data (T1)');
    expect(within(d).getByTestId('tier-badge')).toHaveTextContent('T1');
    expect(d).toHaveTextContent('T1 — jeu de données codé par des experts (codebook)');
    expect(within(d).getByTestId('claim-statement')).toHaveTextContent('file: icb2-fixture.csv · row: 2 · columns: TRIGGR');
    expect(within(d).getByTestId('claim-statement')).toHaveTextContent('révision v16-FIXTURE');
    expect(within(d).getByTestId('claim-mechanism')).toHaveTextContent('« FIXTURE codebook definition: trigger = the specific act');
    expect(within(d).getByTestId('claim-mechanism')).toHaveTextContent('cité tel quel depuis la source');
    expect(within(d).getByTestId('temporal-badge')).toHaveAttribute('data-temporal', 'consistent');
    expect(within(d).getByTestId('claim-corroboration')).toHaveTextContent('2 groupes de sources indépendants');
    expect(within(d).getByTestId('claim-grade')).toHaveTextContent('grade.A.t1t2_consistent_corroborated');
    expect(d).toHaveTextContent('International Crisis Behavior, actor-level codebook (fixture), version 16');

    // a Wikidata claim: statement link and revision permalink; a contested claim lists its contester
    await userEvent.click(screen.getByRole('button', { name: 'edge-kg:wikidata:Q361$FIXTURE-imperialism' }));
    const d2 = await screen.findByTestId('claim-detail');
    expect(within(d2).getByTestId('claim-attribution')).toHaveTextContent('Affirmé selon Wikidata (T3)');
    expect(within(d2).getByRole('link', { name: 'révision' })).toHaveAttribute('href', 'https://www.wikidata.org/w/index.php?oldid=2250000000');
    expect(within(d2).getByTestId('claim-contested')).toHaveTextContent('lien nié');
    expect(within(d2).getByTestId('claim-contested')).toHaveTextContent('selon fixture.scholarly (T2)');
    expect(within(d2).getByTestId('temporal-badge')).toHaveAttribute('data-temporal', 'undetermined');
  });

  it('"Pourquoi ?" separates immediate / underlying / contributing / coded, each "selon <source>", denials excluded', async () => {
    mockFetch(ROUTES);
    at('/v3/causes?event=Q361');
    const why = await screen.findByTestId('why-panel');
    const imm = within(why).getByTestId('why-immediate');
    expect(imm).toHaveTextContent('Attentat de Sarajevo');
    expect(imm).toHaveTextContent('selon Wikidata (T3)');
    const und = within(why).getByTestId('why-underlying');
    expect(und).toHaveTextContent('Système d’alliances européen');
    expect(und).toHaveTextContent('selon fixture.scholarly (T2)');
    expect(within(und).queryByText(/lien nié/)).toBeNull();
    expect(within(why).getByTestId('why-contributing')).toHaveTextContent('selon Wikidata (T4)');
    expect(within(why).getByTestId('why-coded')).toHaveTextContent('selon icb.data (T1)');
  });

  it('dissent panel puts the contested claim and its contester side by side; timeline aligns causes and effects', async () => {
    mockFetch(ROUTES);
    at('/v3/causes?event=Q361');
    const pairs = await screen.findAllByTestId('dissent-pair');
    expect(pairs).toHaveLength(1);
    expect(pairs[0]).toHaveTextContent('Rivalités impériales');
    expect(within(pairs[0]!).getAllByTestId('claim-line')).toHaveLength(2);
    const tl = await screen.findByTestId('causal-timeline');
    expect(within(tl).getAllByTestId('timeline-item').filter((i) => i.getAttribute('data-lane') === 'cause').length).toBe(5);
    expect(within(tl).getAllByTestId('timeline-item').filter((i) => i.getAttribute('data-lane') === 'effect').length).toBe(3);
    expect(within(tl).getByTestId('timeline-undated')).toHaveTextContent('Rivalités impériales (cause)');
  });

  it('candidate space is OFF by default (not requested), labelled and separate when switched on', async () => {
    const { calls } = mockFetch(ROUTES);
    at('/v3/causes?event=Q361');
    const toggle = await screen.findByTestId('candidates-toggle');
    expect(toggle).not.toBeChecked();
    await screen.findByTestId('causal-canvas');
    expect(calls.some((c) => c.url.startsWith('/v1/causal/candidates'))).toBe(false);
    expect(screen.queryByTestId('candidate')).toBeNull();
    await userEvent.click(toggle);
    const panel = await screen.findByTestId('candidates-panel');
    expect(panel).toHaveTextContent('candidats extraits de textes — non sourcés, non vérifiés');
    const cands = await within(panel).findAllByTestId('candidate');
    expect(cands).toHaveLength(2);
    expect(cands[0]).toHaveTextContent('aucun grade');
    expect(within(panel).queryByTestId('grade-badge')).toBeNull();
    // never mixed into the graded graph
    expect(screen.getByTestId('causal-canvas')).toHaveAttribute('data-edges', '10');
  });

  it('chain finder: banner, each hop with its sourced claims, chain grade = weakest hop', async () => {
    const { calls } = mockFetch(ROUTES);
    at('/v3/causes?event=Q361&de=fixture%3Asarajevo_1914&vers=fixture%3Aversailles');
    const finder = await screen.findByTestId('chain-finder');
    expect(within(finder).getByTestId('chain-banner')).toHaveTextContent('Chaîne composée par REVA à partir d’affirmations distinctes — force = maillon le plus faible');
    const chain = await within(finder).findByTestId('causal-chain');
    expect(within(chain).getAllByTestId('chain-hop')).toHaveLength(2);
    expect(within(chain).getAllByTestId('grade-badge')[0]).toHaveAttribute('data-grade', 'C');
    for (const hop of within(chain).getAllByTestId('chain-hop')) expect(within(hop).getByTestId('selon')).toHaveTextContent(/selon Wikidata \(T3\)/);
    const call = calls.find((c) => c.url.startsWith('/v1/causal/chain'))!;
    expect(call.url).toContain('from=fixture%3Asarajevo_1914');
    expect(call.url).toContain('to=fixture%3Aversailles');
  });

  it('chain finder: an identity hop (same_as) is shown with its evidence, not counted as a causal hop', async () => {
    mockFetch({ ...ROUTES, 'GET /v1/causal/chain': { body: F.CHAIN_WITH_IDENTITY } });
    at('/v3/causes?event=Q361&de=fixture%3Asarajevo_1914&vers=fixture%3Aversailles');
    const chain = await within(await screen.findByTestId('chain-finder')).findByTestId('causal-chain');
    expect(within(chain).getAllByTestId('chain-hop')).toHaveLength(2);
    const id = within(chain).getByTestId('chain-identity-hop');
    expect(id).toHaveTextContent('même événement enregistré par deux sources');
    expect(id).toHaveTextContent('ce n’est pas une affirmation causale');
    expect(id).toHaveTextContent('fixture_rule');
    expect(id).toHaveTextContent('fixture:war_number = 106');
    expect(chain).toHaveTextContent('2 maillons · 1 lien(s) d’identité');
    expect(within(chain).getAllByTestId('grade-badge')[0]).toHaveAttribute('data-grade', 'C');
  });

  it('no chain is not "no causal link"', async () => {
    mockFetch({ ...ROUTES, 'GET /v1/causal/chain': { body: F.CHAIN_NONE } });
    at('/v3/causes?event=Q361&vers=fixture%3Aunrelated');
    expect(await screen.findByTestId('chain-none')).toHaveTextContent('Cela ne prouve ni l’absence de lien causal, ni son existence.');
  });

  it('says "pas encore disponible" when the causal API is not deployed, and draws nothing', async () => {
    mockFetch({ 'GET /v1/causal/': { status: 404, body: F.NOT_FOUND_ROUTE } });
    at('/v3/causes?event=Q361');
    expect(await screen.findByText('Pas encore disponible')).toBeInTheDocument();
    expect(screen.queryByTestId('causal-canvas')).toBeNull();
    expect(screen.queryByTestId('why-panel')).toBeNull();
    expect(screen.queryByTestId('claim-line')).toBeNull();
  });

  it('501 is "pas encore disponible" too', async () => {
    mockFetch({ 'GET /v1/causal/': { status: 501, body: { title: 'Not Implemented', status: 501, detail: 'not implemented' } } });
    at('/v3/causes?event=Q361');
    expect(await screen.findByText('Pas encore disponible')).toBeInTheDocument();
  });

  it('a claim without an asserting source is a contract error: nothing is drawn', async () => {
    const bad = F.envelope({ claims: [{ ...F.C_SARAJEVO, source: null }] });
    mockFetch({ ...ROUTES, 'GET /v1/causal/events/Q361/causes': { body: bad } });
    at('/v3/causes?event=Q361');
    expect(await screen.findByText('Réponse non conforme')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId('causes-list')).toBeNull());
  });

  it('without an event: a picker and examples, no request', async () => {
    const { calls } = mockFetch(ROUTES);
    at('/v3/causes');
    expect(await screen.findByText('Choisissez un événement.')).toBeInTheDocument();
    expect(calls.filter((c) => c.url.startsWith('/v1/causal'))).toHaveLength(0);
  });
});

describe('Demander — causal answers (real NLQA pipeline output over test fixtures)', () => {
  it('every causal sentence names its source and tier; chain answers carry the composition caveat; claims are cited', async () => {
    mockFetch({ 'POST /v1/ask': { body: ASK.why } });
    at('/v3/demander');
    await userEvent.type(await screen.findByLabelText('Votre question'), 'Pourquoi la Première Guerre mondiale a-t-elle éclaté ?');
    await userEvent.click(screen.getByRole('button', { name: 'Demander' }));
    const answer = await screen.findByTestId('ask-answer');
    const causal = within(answer)
      .getAllByTestId('answer-sentence')
      .filter((s) => s.getAttribute('data-kind') === 'causal');
    expect(causal.length).toBeGreaterThan(3);
    for (const s of causal) expect(s).toHaveTextContent(/^Selon .+ \(T[1-4], /);
    const items = within(answer).getAllByTestId('evidence-item');
    expect(items.some((i) => /affirmation causale \(de la source\)/.test(i.textContent ?? '') && /selon icb\.data \(T1\)/.test(i.textContent ?? ''))).toBe(true);
  });

  it('chain answer', async () => {
    mockFetch({ 'POST /v1/ask': { body: ASK.chain } });
    at('/v3/demander');
    await userEvent.type(await screen.findByLabelText('Votre question'), 'Chaîne causale ?');
    await userEvent.click(screen.getByRole('button', { name: 'Demander' }));
    const answer = await screen.findByTestId('ask-answer');
    expect(answer).toHaveTextContent('Chaîne composée par REVA à partir de 2 affirmation(s) distincte(s)');
    expect(answer).toHaveTextContent('force = maillon le plus faible (preuve C)');
  });
});

/**
 * FIXA (docs/dev/v3_fixa.md; second causal audit): the causal API carries the asserting source's licence
 * (`source.rights`), files class-level statements in a `generic` lane of /why, names unloaded items
 * "Q7747 (libellé non chargé)", and words Wikidata's P1480 hedges.
 */
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFetch } from '../../test/fetchMock';
import { CausalClaim, getCausalChain, getCausalWhy } from './api';
import { claim, envelope } from './fixtures';
import { partiesFr } from './model';
import { ClaimDetail, WhyPanel } from './parts';

afterEach(() => vi.unstubAllGlobals());

const IDMC_RIGHTS = {
  licence: 'CC BY-NC 4.0 (stated in the workbook README sheet)',
  licence_url: null,
  attribution: null,
  commercial_use: 'not_permitted',
  non_commercial: true,
  share_alike: false,
  no_derivatives: false,
  open_licence: false,
  restricted: true,
  basis: 'source_registry',
};

function idmcClaim() {
  const c = claim({
    key: 'ds:idmc.gidd:disasters:FIXA-1',
    cause: { id: 'idmc:event:FIXA', label: 'Storm (fixture)' },
    effect: { id: 'idmc:displacement:FIXA', label: 'Internal displacements (fixture)' },
    relation: 'has_effect',
    labelFr: 'a pour effet',
    lane: 'general',
    tier: 'T1',
    grade: 'B',
    dataset: { source_key: 'idmc.gidd', dataset: 'disasters', revision: 'fixture', selector: { row: 1 } },
  });
  return { ...c, source: { ...c.source, rights: IDMC_RIGHTS } };
}

function genericClaim() {
  const c = claim({
    key: 'kg:wikidata:FIXA-GEN',
    cause: { id: 'Q5', label: 'human' },
    effect: { id: 'Q361', label: 'Première Guerre mondiale' },
    relation: 'has_cause',
    labelFr: 'a pour cause',
    lane: 'general',
    tier: 'T4',
    grade: 'D',
  });
  return { ...c, generic: { class_endpoints: ['cause'], rule: 'classes' } };
}

describe('FIXA server shapes', () => {
  it('reads the licence the causal API serves with each claim', () => {
    const c = CausalClaim.parse(idmcClaim());
    expect(c.source.rights?.licence).toMatch(/BY-NC/);
    expect(c.source.rights?.restricted).toBe(true);
    expect(c.source.rights?.source).toBe('idmc.gidd');
  });

  it('shows the API licence on the claim card without the source registry', () => {
    const c = CausalClaim.parse(idmcClaim());
    render(
      <MemoryRouter>
        <ClaimDetail c={c} all={new Map([[c.id, c]])} onOpen={() => {}} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('claim-licence').textContent).toMatch(/CC BY-NC 4\.0/);
  });

  it('files class-level statements apart in /why, never among the causes', async () => {
    const gen = genericClaim();
    mockFetch({
      'GET /v1/causal/events/Q361/why': {
        body: envelope({
          id: 'Q361',
          label: 'World War I',
          lanes: [{ lane: 'generic', label: { fr: 'énoncés génériques (classe)' }, claims: [gen] }],
          mechanisms: [],
          dissent: [],
          truncated: false,
          causal_claims: 0,
          generic_claims: 1,
        }),
      },
    });
    const w = await getCausalWhy('Q361');
    expect(w.groups.generic.map((c) => c.id)).toEqual(['kg:wikidata:FIXA-GEN']);
    expect(w.groups.general).toEqual([]);
    render(
      <MemoryRouter>
        <WhyPanel groups={w.groups} derived={false} onOpen={() => {}} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('why-generic').textContent).toMatch(/Énoncés génériques \(classe\)/);
    expect(screen.getByTestId('why-empty')).toBeTruthy(); // no cause of this event is asserted
  });

  it('counts the entities a chain search did not traverse because the KG has not loaded them', async () => {
    mockFetch({
      'GET /v1/causal/chain': {
        body: envelope({
          from: { id: 'Q201684', label: 'krach de 1929' },
          to: { id: 'Q186356', label: 'New Deal' },
          found: false,
          paths: [],
          paths_found: 0,
          not_found_reason: null,
          search: { max_hops: 4, unloaded_nodes_skipped: 1, unloaded_nodes_sample: ['Q8698'] },
        }),
      },
    });
    const c = await getCausalChain('Q201684', 'Q186356');
    expect(c.skipped.unloaded).toBe(1);
    expect(c.skipped.unloadedSample).toEqual(['Q8698']);
  });

  it('names an unloaded party honestly and words a P1480 hedge', () => {
    expect(partiesFr([{ id: 'Q7747', label: 'Q7747 (libellé non chargé)' }])).toBe('Q7747 (libellé non chargé)');
    const base = claim({
      key: 'kg:wikidata:FIXA-H',
      cause: { id: 'Q109905701', label: 'Q109905701 (libellé non chargé)' },
      effect: { id: 'Q114912414', label: 'bousculade (fixture)' },
      relation: 'has_cause',
      labelFr: 'a pour cause',
      lane: 'general',
      tier: 'T3',
      grade: 'D',
    });
    const c = CausalClaim.parse({
      ...base,
      attribution: {
        supported_by: [],
        nature_of_statement: [],
        sourcing_circumstances: [{ id: 'Q18122778', label: 'Q18122778 (label not loaded)', label_fr: 'Q18122778 (libellé non chargé)' }],
        scope_qualifiers: [],
        source_validity: [],
        note_fr: 'Wikidata qualifie : Q18122778 (libellé non chargé) (circonstances de la source, P1480)',
      },
    });
    expect(c.attribution?.hedges.map((h) => h.label)).toEqual(['Q18122778 (libellé non chargé)']);
    expect(c.attribution?.note).toMatch(/^Wikidata qualifie : /);
  });
});

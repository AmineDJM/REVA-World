/**
 * V6 INTEG (for V4LINK): identity steps (family same_as) are worded as identity, never as a relation; concordance
 * steps (corresponds_to) show the table's own flags. TEST DATA ONLY (synthetic statement ids).
 */
import { describe, expect, it } from 'vitest';
import { flagsText, specialProperties } from '../api/families';
import { LinksPathResponse } from '../api/links';
import * as F from '../test/fixtures';
import { connectedSentence, fromLinks, identitySentence } from './paths';

const base = F.LINKS_PATH_COL_BRA.data;
const hop = (o: Record<string, unknown>) => ({
  kind: 'statement', forward: true, causal: false, temporal: { kind: 'undated' }, sources: [], alternatives: 0,
  evidence: [], text: { fr: '', en: '' }, ...o,
});

describe('identity and concordance steps', () => {
  const d = LinksPathResponse.parse({
    ...F.LINKS_PATH_COL_BRA,
    data: {
      ...base,
      labels: { 'nuts:DE2': { fr: 'Bayern' }, Q980: { fr: 'Bavière' }, 'cpc21:12010': { fr: 'Pétrole brut' } },
      paths: [
        {
          nodes: ['nuts:DE2', 'Q980', 'cpc21:12010'],
          hop_count: 2,
          cost: 2,
          text: { fr: [], en: [] },
          hops: [
            hop({ from: 'nuts:DE2', to: 'Q980', type: 'v4link:wikidata_item', family: 'same_as', identity: true, text: { fr: 'Bayern — est la même entité que (identité) → Bavière', en: '' } }),
            hop({ from: 'Q980', to: 'cpc21:12010', type: 'v4link:corresponds_to', family: 'corresponds_to', identity: false, flags: { table: 'CPC21-HS2017', hs_partial: '0' }, text: { fr: 'x — correspond à (table de concordance) → y', en: '' } }),
          ],
        },
      ],
    },
  }).data;

  it('words identity steps as identity and keeps concordance flags', () => {
    const [idHop, cHop] = fromLinks(d).paths[0]!.hops;
    expect(idHop!.identity).toBe(true);
    expect(idHop!.sentence).toBe(identitySentence('est la même entité que (identité)'));
    expect(idHop!.sentence).not.toBe(connectedSentence('est la même entité que (identité)'));
    expect(cHop!.identity).toBe(false);
    expect(cHop!.flags).toBe('hs partial 0 · table CPC21-HS2017');
  });

  it('reads the family registry into identity / concordance property sets', () => {
    const s = specialProperties({ data: { mappings: [{ property: 'v4link:wikidata_item', family: 'same_as' }, { property: 'v4link:corresponds_to', family: 'corresponds_to' }, { property: 'P361', family: 'part_of' }] } });
    expect([...s.identity]).toEqual(['v4link:wikidata_item']);
    expect([...s.concordance]).toEqual(['v4link:corresponds_to']);
    expect(flagsText({ redistribution: 'x', relationship: '1:n' })).toBe('relationship 1:n');
  });
});

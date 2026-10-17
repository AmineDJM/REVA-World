/**
 * APIFIX (docs/dev/v3_apifix.md): the server now counts coded outcomes apart (bucket "outcome"), merges identical
 * chain paths itself (search.duplicate_paths_merged) and says when a chain end is a coded outcome record.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mockFetch } from '../../test/fetchMock';
import { CausalEvent, getCausalChain, notFoundReasonFr } from './api';

afterEach(() => vi.unstubAllGlobals());

const env = (data: unknown) => ({ data, context_mode: 'evidence', origin: 'live', modality: 'reported_observation', knowledge_cutoff: null, warnings: [], next_cursor: null });

describe('APIFIX server shapes', () => {
  it('coded outcomes are counted apart from causes and consequences', () => {
    // GET /v1/causal/events/icb:crisis:196 on the dev DB after APIFIX
    const e = CausalEvent.parse({
      id: 'icb:crisis:196',
      event: { id: 'icb:crisis:196', label: 'CUBAN MISSILES', label_fr: null, kind: 'event' },
      dates: { start: null, end: null },
      causes: { cause: { B: 1 } },
      effects: { outcome: { B: 1 } },
      context: [],
      candidates: 0,
    });
    expect(e.nCauses).toBe(1);
    expect(e.nEffects).toBe(0);
    expect(e.nOutcomes).toBe(1);
  });

  it('a chain to a coded outcome record: the reason says outcomes are never composed, in French', () => {
    const r =
      'icb:crisis:196:outcome is a coded outcome record (how an event ended according to a dataset): outcomes are never ' +
      'composed into causal chains (registry/causal/relations.yaml chain_excluded); no causal chain within max_hops among claims graded at least D';
    expect(notFoundReasonFr(r)).toBe(
      'icb:crisis:196:outcome : issue codée (comment un événement s’est terminé selon un jeu de données) — une issue n’est jamais composée dans une chaîne causale',
    );
  });

  it('the paths the server merged are counted', async () => {
    mockFetch({
      'GET /v1/causal/chain': {
        body: env({
          from: { id: 'a', label: 'a' },
          to: { id: 'b', label: 'b' },
          found: false,
          paths: [],
          paths_found: 0,
          not_found_reason: null,
          search: { max_hops: 4, duplicate_paths_merged: 1 },
        }),
      },
    });
    const c = await getCausalChain('a', 'b');
    expect(c.duplicatesMerged).toBe(1);
  });
});

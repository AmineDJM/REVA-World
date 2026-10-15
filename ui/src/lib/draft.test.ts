import { describe, expect, it } from 'vitest';
import { diffJson, normalizeDraft, pinToRelease, reviewSpec, setIn } from './draft';
import { READY_RESPONSE } from '../test/v2fixtures';

describe('normalizeDraft', () => {
  it('reads the ScenarioDraftResponse fields as served', () => {
    const d = normalizeDraft(READY_RESPONSE);
    expect(d.status).toBe('draft_ready');
    expect(d.executed).toBe(false);
    expect(d.draft_spec?.countries).toEqual(['FRA', 'USA']);
    expect(d.unsupported[0]).toMatchObject({ capability_key: 'outputs.macroeconomic', kind: 'capability_gap', source: 'llm' });
    expect(d.numbers_to_confirm[0]).toMatchObject({ path: 'intervention.import_multiplier', value: 1.2, provenance: 'converted_from_stated' });
    expect(d.assumptions_added[0]!.text).toBe('export multiplier 1 (unchanged)');
    expect(d.provider).toBe('openai');
    expect(d.model).toBe('gpt-4.1-2025-04-14');
    expect(reviewSpec(d)).toBe(d.draft_spec);
  });

  it('accepts an envelope and leaves absent fields empty, never invented', () => {
    const d = normalizeDraft({ data: { status: 'nothing_draftable', candidate_spec: { title: 'x' } } });
    expect(d.status).toBe('nothing_draftable');
    expect(d.draft_spec).toBeNull();
    expect(reviewSpec(d)).toEqual({ title: 'x' });
    expect(d.provider).toBeNull();
    expect(d.unsupported).toEqual([]);
    expect(d.confirmation_required).toBe(true);
    expect(normalizeDraft(null).status).toBe('unknown');
  });
});

describe('diffJson', () => {
  it('lists added, removed and changed paths in sorted order', () => {
    const a = { b: 1, a: { x: [1, 2] }, c: 'k' };
    const b = { b: 2, a: { x: [1, 3, 4] }, d: true };
    expect(diffJson(a, b)).toEqual([
      { path: 'a.x[1]', kind: 'changed', before: 2, after: 3 },
      { path: 'a.x[2]', kind: 'added', after: 4 },
      { path: 'b', kind: 'changed', before: 1, after: 2 },
      { path: 'c', kind: 'removed', before: 'k' },
      { path: 'd', kind: 'added', after: true },
    ]);
    expect(diffJson({ a: 1 }, { a: 1 })).toEqual([]);
  });
});

describe('setIn / pinToRelease', () => {
  it('sets nested values without mutating the original', () => {
    const o = { a: { b: [1, 2] } };
    const n = setIn(o, ['a', 'b', 1], 5) as typeof o;
    expect(n.a.b).toEqual([1, 5]);
    expect(o.a.b).toEqual([1, 2]);
  });
  it('pins the release id and cutoff from the chosen release, never from the draft', () => {
    expect(pinToRelease({ baseline_release_id: 'llm', knowledge_cutoff: 'x', t: 1 }, { id: 'r2', knowledge_cutoff: '2026-10-15T01:00:00Z' })).toEqual({
      baseline_release_id: 'r2',
      knowledge_cutoff: '2026-10-15T01:00:00Z',
      t: 1,
    });
  });
});

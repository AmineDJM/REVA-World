import { describe, expect, it } from 'vitest';
import { CausalCandidate, CausalClaim, type Grade } from './api';
import * as F from './fixtures';
import { bestGrade, buildGraph, chainGrade, dissentPairs, groupWhy, hopGrade, selon, weakestGrade } from './model';

const parse = (o: unknown) => CausalClaim.parse(o);
const causes = F.CAUSES.map(parse);
const effects = F.EFFECTS.map(parse);

describe('causal claims (zod)', () => {
  it('normalises the CAUSCORE claim JSON: source, tier, grade object, temporal windows', () => {
    const c = parse(F.C_SARAJEVO);
    expect(c.id).toBe('kg:wikidata:Q361$FIXTURE-sarajevo');
    expect(c.tier).toBe('T3');
    expect(c.grade).toBe('B');
    expect(c.gradeRules).toContain('grade.B.t3_corroborated');
    expect(c.temporal.status).toBe('consistent');
    expect(c.cause.start).toEqual({ time: '1914-06-28', precision: 11 });
    expect(c.corroboration.count).toBe(2);
    expect(c.source.statementId).toBe('Q361$FIXTURE-41');
    expect(selon(c)).toBe('selon Wikidata (T3)');
  });

  it('a claim must name its source and a tier T1–T4; a grade is A–E or null (not yet graded)', () => {
    expect(CausalClaim.safeParse({ ...F.C_SARAJEVO, source: null }).success).toBe(false);
    expect(CausalClaim.safeParse({ ...F.C_SARAJEVO, source: { ...F.C_SARAJEVO.source, tier: 'T5' } }).success).toBe(false);
    expect(CausalClaim.safeParse({ ...F.C_SARAJEVO, grade: { ...F.C_SARAJEVO.grade, grade: 'Z' } }).success).toBe(false);
    expect(parse(F.C_UNGRADED).grade).toBeNull();
  });

  it('a candidate is T5/T6 and never carries a grade; a claim never parses as a candidate', () => {
    const cand = F.CANDIDATES_WW1.data.candidates[0]!;
    expect(CausalCandidate.parse(cand).tier).toBe('T5');
    expect(CausalCandidate.safeParse({ ...cand, grade: 'A' }).success).toBe(false);
    expect(CausalCandidate.safeParse(F.C_SARAJEVO).success).toBe(false);
  });
});

describe('grades', () => {
  it('weakest / best', () => {
    expect(weakestGrade(['A', 'C', 'B'])).toBe('C');
    expect(bestGrade(['D', 'B', 'E'])).toBe('B');
    expect(weakestGrade([])).toBeNull();
  });

  it('hop = best of its claims; chain = weakest hop; a server summary can only weaken it', () => {
    const h = (gs: (Grade | null)[], serverGrade: Grade | null = null) => ({ claims: gs.map((grade) => ({ grade })), serverGrade });
    expect(hopGrade(h(['C', 'A']))).toBe('A');
    expect(chainGrade({ hops: [h(['B']), h(['C', 'D'])], serverGrade: null })).toBe('C');
    expect(chainGrade({ hops: [h(['B']), h(['B'])], serverGrade: 'A' })).toBe('B');
    expect(chainGrade({ hops: [h(['B']), h(['B'])], serverGrade: 'D' })).toBe('D');
    expect(chainGrade({ hops: [h(['B']), h([null])], serverGrade: null })).toBeNull();
  });
});

describe('views', () => {
  it('why lanes follow the declared relation; a denial is never a cause', () => {
    const g = groupWhy(causes);
    expect(g.immediate.map((c) => c.cause.label)).toEqual(['Attentat de Sarajevo']);
    expect(g.coded.map((c) => c.relation)).toEqual(['coded_trigger']);
    expect(g.underlying.every((c) => c.stance === 'asserts')).toBe(true);
    expect(g.underlying.map((c) => c.grade)).toEqual(['B', 'E']); // ordered by grade
  });

  it('dissent pairs a contested claim with its contester (explicit or denial)', () => {
    const p = dissentPairs(causes);
    expect(p).toHaveLength(1);
    expect(p[0]!.claim.cause.label).toBe('Rivalités impériales');
    expect(p[0]!.against.map((a) => a.claim?.stance)).toEqual(['denies']);
  });

  it('graph: causes left, effects right, one edge per asserting claim, denials not drawn', () => {
    const g = buildGraph({ id: 'Q361', label: 'WW1' }, causes, effects);
    expect(g.edges).toHaveLength(10);
    expect(g.edges.some((e) => e.claim.stance === 'denies')).toBe(false);
    const n = new Map(g.nodes.map((x) => [x.id, x]));
    expect(n.get('fixture:sarajevo_1914')!.x).toBeLessThan(0);
    expect(n.get('fixture:versailles')!.side).toBe('both'); // asserted as an effect and as an (inconsistent) cause: middle column
    // a coded outcome (how the war ended) is not a consequence: drawn under the event, apart from the effects
    expect(n.get('fixture:armistice_1918')!.side).toBe('outcome');
    expect(n.get('fixture:armistice_1918')!.y).toBeGreaterThan(0);
    expect(n.get('fixture:russian_revolution')!.x).toBeGreaterThan(0);
    expect(g.hidden).toBe(0);
    expect(g.edges.find((e) => e.id === F.C_UNGRADED.claim_key)!.grade).toBeNull();
  });
});

describe('timeline', () => {
  it('a far outlier is listed outside the window, never dropped', async () => {
    const { buildTimeline } = await import('./model');
    const outlier = parse({ ...F.E_VERSAILLES, claim_key: 'x', effect: { id: 'fixture:ottoman', label: 'Empire ottoman' }, grade: { ...F.E_VERSAILLES.grade!, temporal: { cause_start: null, effect_start: { from: '1299-08-05', precision: 11 } }, temporal_check: 'inconsistent' } });
    const m = buildTimeline({ id: 'Q361', label: 'WW1', start: { time: '1914-07-28', precision: 11 }, end: { time: '1918-11-11', precision: 11 } }, causes, [...effects, outlier]);
    expect(m.outside.map((i) => i.label)).toEqual(['Empire ottoman']);
    expect(m.items.some((i) => i.label === 'Empire ottoman')).toBe(false);
    expect(m.min).toBeGreaterThan(1800);
  });
});

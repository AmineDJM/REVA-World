import { describe, expect, it } from 'vitest';
import { fold, rankItems, scoreItem, type PaletteItem } from './palette';

const items: PaletteItem[] = [
  { id: 'civ', kind: 'country', title: "Côte d'Ivoire", href: '/map?focus=civ' },
  { id: 'fra', kind: 'country', title: 'France', keywords: ['FRA'], href: '/country/FRA' },
  { id: 'gab', kind: 'country', title: 'Gabon', href: '/map?focus=gab' },
  { id: '/world', kind: 'screen', title: 'World', href: '/world' },
  { id: 'c1', kind: 'concept', title: 'Population (World Bank)', keywords: ['demography.population.wdi_midyear_total'], href: '/evidence' },
  { id: 'r1', kind: 'run', title: 'France crude stress', keywords: ['economic.crude_exposure'], href: '/runs/r1' },
  { id: 'rel', kind: 'release', title: 'Release #3', keywords: ['3'], href: '/simulation?release=rel' },
];

describe('command palette search', () => {
  it('folds case and diacritics', () => {
    expect(fold('  Côte  d’Ivoire ')).toBe("cote d'ivoire");
    expect(rankItems(items, 'cote')[0]!.id).toBe('civ');
  });

  it('ranks exact > prefix > word prefix > substring, then kind order and title', () => {
    expect(rankItems(items, 'france').map((i) => i.id)).toEqual(['fra', 'r1']);
    expect(rankItems(items, 'FRA')[0]!.id).toBe('fra');
    expect(rankItems(items, 'world').map((i) => i.id)).toEqual(['/world', 'c1']);
  });

  it('requires every term to match and searches keywords', () => {
    expect(rankItems(items, 'crude france').map((i) => i.id)).toEqual(['r1']);
    expect(rankItems(items, 'midyear').map((i) => i.id)).toEqual(['c1']);
    expect(rankItems(items, 'zzz')).toEqual([]);
  });

  it('allows an in-order subsequence on titles only (lowest score)', () => {
    expect(scoreItem(items[2]!, 'gbn')).toBe(10);
    expect(scoreItem(items[4]!, 'dmg')).toBe(0);
  });

  it('is deterministic: same input, same order, regardless of input order', () => {
    const a = rankItems(items, 'r').map((i) => i.id);
    const b = rankItems([...items].reverse(), 'r').map((i) => i.id);
    expect(a).toEqual(b);
  });

  it('returns everything in kind order for an empty query', () => {
    expect(rankItems(items, '')[0]!.kind).toBe('screen');
  });
});

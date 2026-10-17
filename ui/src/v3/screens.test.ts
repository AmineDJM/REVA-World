import { describe, expect, it } from 'vitest';
import { V3_NAV, V3_PALETTE, V3_SCREENS, uniqueByRoute } from './screens';

describe('V3 ⌘K palette entries', () => {
  it('has one entry per route (the palette keys its options by route)', () => {
    const keys = V3_PALETTE.map((e) => e.to);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).not.toContain(V3_NAV.to); // the app nav already lists /v3
  });

  it('lists every V3 screen and the Brazil ↔ Portugal relation once', () => {
    for (const s of V3_SCREENS) expect(V3_PALETTE.filter((e) => e.to === s.to)).toHaveLength(1);
    expect(V3_PALETTE.filter((e) => e.to === '/v3/relation?a=BRA&b=PRT')).toHaveLength(1);
  });

  it('uniqueByRoute keeps the first entry of a repeated route, in order', () => {
    const out = uniqueByRoute([
      { to: '/a', label: 'A1' },
      { to: '/b', label: 'B' },
      { to: '/a', label: 'A2' },
    ]);
    expect(out).toEqual([
      { to: '/a', label: 'A1' },
      { to: '/b', label: 'B' },
    ]);
  });
});

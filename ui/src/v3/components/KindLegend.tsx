import { KIND_STYLES, type KindGroup } from '../lib/kinds';

const SHAPES: Record<string, string> = {
  ellipse: 'M8 1 A7 7 0 1 1 7.99 1 Z',
  'round-rectangle': 'M3 2 H13 Q15 2 15 4 V12 Q15 14 13 14 H3 Q1 14 1 12 V4 Q1 2 3 2 Z',
  rectangle: 'M1 2 H15 V14 H1 Z',
  octagon: 'M5 1 H11 L15 5 V11 L11 15 H5 L1 11 V5 Z',
  diamond: 'M8 0 L16 8 L8 16 L0 8 Z',
  triangle: 'M8 1 L15 15 H1 Z',
  star: 'M8 0 L10 6 L16 6 L11 10 L13 16 L8 12 L3 16 L5 10 L0 6 L6 6 Z',
  tag: 'M1 3 H11 L15 8 L11 13 H1 Z',
  barrel: 'M3 1 H13 Q16 8 13 15 H3 Q0 8 3 1 Z',
};

export function KindGlyph({ group, size = 14 }: { group: KindGroup; size?: number }) {
  const s = KIND_STYLES[group];
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true" className="v3-kind-glyph">
      <path d={SHAPES[s.shape] ?? SHAPES.ellipse} fill={s.color} stroke="rgba(0,0,0,0.35)" strokeWidth="0.8" />
    </svg>
  );
}

export function KindLegend({ counts }: { counts?: Partial<Record<KindGroup, number>> }) {
  return (
    <ul className="v3-kind-legend" aria-label="Légende des types d’entité">
      {(Object.keys(KIND_STYLES) as KindGroup[]).map((g) => (
        <li key={g}>
          <KindGlyph group={g} />
          {KIND_STYLES[g].label}
          {counts && counts[g] ? <span className="muted"> {counts[g]}</span> : null}
        </li>
      ))}
    </ul>
  );
}

import { useId } from 'react';
import type { TypedValue } from '../api/types';
import { describeUncertainty, type AnyUncertainty } from './UncertaintyPanel';

/**
 * One visual vocabulary for "how sure is this value", used by every badge, missing value and
 * legend in the UI:
 *   solid line   = exact by definition
 *   dotted line  = uncertainty not quantified (NOT exact)
 *   ribbon       = assumption sensitivity range (no probability)
 *   hatched box  = missing value (a gap, never zero)
 * plus distinct glyphs for probabilistic intervals, bounds and mixed rows.
 */
export type GlyphKind = 'exact' | 'unquantified' | 'sensitivity' | 'missing' | 'interval' | 'bounds' | 'mixed';

export const CORE_GLYPHS: GlyphKind[] = ['exact', 'unquantified', 'sensitivity', 'missing'];

export const GLYPH_LABEL: Record<GlyphKind, { label: string; meaning: string }> = {
  exact: { label: 'Solid line', meaning: 'exact by definition (a definition or transcription, not exact knowledge of reality)' },
  unquantified: { label: 'Dotted line', meaning: 'uncertainty not quantified (this does not mean exact)' },
  sensitivity: { label: 'Ribbon', meaning: 'assumption sensitivity range (spread over assumed parameters; no probability)' },
  missing: { label: 'Hatched box', meaning: 'missing value (a data gap, never zero)' },
  interval: { label: 'Shaded band with centre line', meaning: 'probabilistic interval at its stated nominal level' },
  bounds: { label: 'Brackets', meaning: 'bounds (feasible range; no probability attached)' },
  mixed: { label: 'Dash-dot line', meaning: 'mixed uncertainty status across rows (see each row)' },
};

/**
 * Glyph for a value from the API's uncertainty status/kind. A missing value wins (hatched), and an
 * L2 sensitivity-range value is a ribbon whatever its uncertainty record says.
 */
export function glyphKindFor(uncertainty: AnyUncertainty | null | undefined, value?: TypedValue | null): GlyphKind {
  if (value && value.kind === 'missing') return 'missing';
  if (value && value.kind === 'sensitivity_range') return 'sensitivity';
  const t = describeUncertainty(uncertainty).treatment;
  switch (t) {
    case 'definition':
      return 'exact';
    case 'sensitivity':
      return 'sensitivity';
    case 'prediction':
    case 'confidence':
    case 'credible':
    case 'distribution':
      return 'interval';
    case 'bounds':
      return 'bounds';
    case 'mixed':
      return 'mixed';
    default:
      // unquantified, not applicable or not reported: never drawn as exact.
      return 'unquantified';
  }
}

const STROKE = '#374151';
const SENS = '#9a3412';
const MISS = '#6b4e16';
const INT = '#1f4e79';

/** A 22×12 decorative glyph (aria-hidden); the text next to it carries the meaning. */
export function UncertaintyGlyph({ kind, className }: { kind: GlyphKind; className?: string }) {
  const id = `uhatch-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  return (
    <svg
      className={`uglyph uglyph-${kind}${className ? ` ${className}` : ''}`}
      data-glyph={kind}
      width="22"
      height="12"
      viewBox="0 0 22 12"
      aria-hidden="true"
      focusable="false"
    >
      {kind === 'exact' && <line x1="1" y1="6" x2="21" y2="6" stroke={STROKE} strokeWidth="2.5" />}
      {kind === 'unquantified' && (
        <line x1="2" y1="6" x2="21" y2="6" stroke={STROKE} strokeWidth="2.5" strokeDasharray="0.1 4" strokeLinecap="round" />
      )}
      {kind === 'sensitivity' && (
        <>
          <rect x="1" y="2" width="20" height="8" fill={SENS} fillOpacity="0.25" />
          <line x1="1" y1="2" x2="21" y2="2" stroke={SENS} strokeWidth="1.2" />
          <line x1="1" y1="10" x2="21" y2="10" stroke={SENS} strokeWidth="1.2" />
        </>
      )}
      {kind === 'missing' && (
        <>
          <defs>
            <pattern id={id} width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <line x1="0" y1="0" x2="0" y2="4" stroke={MISS} strokeWidth="1.5" />
            </pattern>
          </defs>
          <rect x="1" y="1" width="20" height="10" fill={`url(#${id})`} stroke={MISS} strokeWidth="1" />
        </>
      )}
      {kind === 'interval' && (
        <>
          <rect x="1" y="2" width="20" height="8" fill={INT} fillOpacity="0.3" />
          <line x1="1" y1="6" x2="21" y2="6" stroke={INT} strokeWidth="1.5" />
        </>
      )}
      {kind === 'bounds' && <path d="M5 1 H2 V11 H5 M17 1 H20 V11 H17" fill="none" stroke={STROKE} strokeWidth="1.5" />}
      {kind === 'mixed' && <line x1="1" y1="6" x2="21" y2="6" stroke={STROKE} strokeWidth="2" strokeDasharray="5 2 1 2" />}
    </svg>
  );
}

export interface UncertaintyLegendProps {
  /** Glyph kinds present in the listed values; extra (non-core) kinds are appended. */
  present?: Iterable<GlyphKind>;
  className?: string;
}

/**
 * Legend for the uncertainty glyphs. Always lists the four core glyphs so the vocabulary is the
 * same on every screen; glyphs actually used by the listed values are marked "shown here".
 */
export function UncertaintyLegend({ present = [], className }: UncertaintyLegendProps) {
  const used = new Set(present);
  const kinds = [...CORE_GLYPHS, ...(['interval', 'bounds', 'mixed'] as GlyphKind[]).filter((k) => used.has(k))];
  return (
    <div className={`uncertainty-legend${className ? ` ${className}` : ''}`} role="group" aria-label="Uncertainty glyph legend" data-testid="uncertainty-legend">
      <span className="uncertainty-legend-title">Uncertainty key:</span>
      <ul>
        {kinds.map((k) => (
          <li key={k} data-glyph-kind={k} data-present={used.has(k) ? 'true' : 'false'} className={used.has(k) ? 'present' : undefined}>
            <UncertaintyGlyph kind={k} /> <span className="uglyph-label">{GLYPH_LABEL[k].label}</span>{' '}
            <span className="uglyph-meaning">= {GLYPH_LABEL[k].meaning}</span>
            {used.has(k) && <span className="muted"> (shown here)</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Glyph kinds for a list of rows (value + uncertainty). */
export function glyphKindsOf(rows: Iterable<{ value?: TypedValue | null; uncertainty?: AnyUncertainty | null }>): Set<GlyphKind> {
  const s = new Set<GlyphKind>();
  for (const r of rows) s.add(glyphKindFor(r.uncertainty, r.value));
  return s;
}

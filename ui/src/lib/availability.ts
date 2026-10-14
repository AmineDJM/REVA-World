import type { Coverage, Gap } from '../api/types';
import type { AvailabilityClass } from '../components/MapView';

const MISSING_GAP_KINDS = new Set(['typed_missing', 'absent', 'no_release']);

/**
 * Availability class per entity id from catalogue coverage and open gaps.
 * Counts required (entity, period) cells and subtracts cells with a missing-type
 * gap. Describes data availability only, never accuracy.
 */
export function computeAvailability(
  coverage: Coverage | undefined,
  gaps: Gap[] | undefined,
  entityIdsByKey: Record<string, string> | undefined,
): Record<string, AvailabilityClass> {
  const out: Record<string, AvailabilityClass> = {};
  if (!coverage || !entityIdsByKey) return out;
  const missingByKey = new Map<string, number>();
  for (const g of gaps ?? []) {
    if (MISSING_GAP_KINDS.has(g.gap_kind)) missingByKey.set(g.entity_key, (missingByKey.get(g.entity_key) ?? 0) + 1);
  }
  for (const [key, id] of Object.entries(entityIdsByKey)) {
    let required = 0;
    for (const r of coverage.requirements) {
      if (!r.geography.includes(key)) continue;
      const [a, b] = r.period;
      required += a !== undefined && b !== undefined ? b - a + 1 : r.period.length;
    }
    const missing = Math.min(required, missingByKey.get(key) ?? 0);
    const available = required - missing;
    out[id] =
      required === 0 ? 'not_in_catalogue' : available === required ? 'complete' : available > 0 ? 'partial' : 'none_available';
  }
  return out;
}

export function catalogueKeys(coverage: Coverage | undefined): string[] {
  const keys = new Set<string>();
  coverage?.requirements.forEach((r) => r.geography.forEach((g) => keys.add(g)));
  return [...keys].sort().slice(0, 20);
}

/** Year of a support/time label ("2023", "2023-01-01", ...). */
export function yearOf(s: string | null | undefined): number | null {
  if (!s) return null;
  const m = /^(-?\d{4})/.exec(s);
  return m ? Number(m[1]) : null;
}

/**
 * Number/text formatting for V8 (French). Atlas values are exact decimal strings: they are formatted
 * without float conversion (frDecimal). A missing value has no number form: render <MissingValue/>.
 */
export { frDecimal, frValue, frInt, toNumber, digitsForUnit, missingReasonFr, MISSING_REASON_FR } from '../../../v3/lib/format';
import { frDecimal } from '../../../v3/lib/format';

/** French display of a JS number (for computed counts, ranks, shares). */
export function frNumber(n: number, maxFractionDigits = 2): string {
  if (!Number.isFinite(n)) return String(n);
  return n.toLocaleString('fr-FR', { maximumFractionDigits: maxFractionDigits }).replace('-', '−');
}

/** Compact French display: 1,2 k / 3,4 M / 5,6 Md. */
export function frCompact(n: number): string {
  if (!Number.isFinite(n)) return String(n);
  const a = Math.abs(n);
  const s = n < 0 ? '−' : '';
  const f = (v: number, u: string) => `${s}${v.toLocaleString('fr-FR', { maximumFractionDigits: v < 10 ? 1 : 0 })}${u}`;
  if (a >= 1e12) return f(a / 1e12, ' T');
  if (a >= 1e9) return f(a / 1e9, ' Md');
  if (a >= 1e6) return f(a / 1e6, ' M');
  if (a >= 1e4) return f(a / 1e3, ' k');
  return frNumber(n);
}

/** Display any value given to a primitive: exact decimal strings stay exact, numbers use fr-FR. */
export function displayValue(v: string | number, maxFractionDigits = 3): string {
  if (typeof v === 'number') return frNumber(v, maxFractionDigits);
  return frDecimal(v, maxFractionDigits);
}

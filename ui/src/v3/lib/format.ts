/**
 * French display of exact decimal strings (no float conversion: "57082396439.6" stays exact;
 * only the grouping/decimal separators and display rounding change). A missing value has no
 * number form at all: callers render <MissingCell/> instead (never 0, never a dash).
 */

const NNBSP = ' ';

export function frDecimal(value: string, maxFractionDigits = 3): string {
  const t = value.trim();
  const m = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(t);
  if (!m) return t;
  if (m[4] !== undefined) {
    const n = Number(t);
    return Number.isFinite(n) ? n.toLocaleString('fr-FR', { maximumSignificantDigits: 6 }) : t;
  }
  let int = (m[2] || '0').replace(/^0+(?=\d)/, '');
  let frac = m[3] ?? '';
  if (frac.length > maxFractionDigits) {
    const keep = frac.slice(0, maxFractionDigits);
    const next = frac.charCodeAt(maxFractionDigits) - 48;
    if (next >= 5) {
      const digits = (int + keep).split('').map(Number);
      let i = digits.length - 1;
      for (; i >= 0; i--) {
        if (digits[i]! < 9) {
          digits[i] = digits[i]! + 1;
          break;
        }
        digits[i] = 0;
      }
      if (i < 0) digits.unshift(1);
      const s = digits.join('');
      int = s.slice(0, s.length - keep.length) || '0';
      frac = s.slice(s.length - keep.length);
    } else frac = keep;
  }
  frac = frac.replace(/0+$/, '');
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, NNBSP);
  const zero = /^0*$/.test(int) && !/[1-9]/.test(frac);
  const sign = m[1] === '-' && !zero ? '−' : '';
  return `${sign}${grouped}${frac ? `,${frac}` : ''}`;
}

/** Fraction digits for display by unit: big counts are shown whole, shares to 2 decimals. */
export function digitsForUnit(unit: string | null | undefined): number {
  const u = (unit ?? '').toLowerCase();
  if (/persons|people|personnes|us\$|usd|\$|km2|km²|number|count/.test(u)) return 0;
  if (u.includes('%') || u.includes('percent')) return 2;
  return 3;
}

export function frValue(value: string, unit?: string | null): string {
  return frDecimal(value, digitsForUnit(unit));
}

/** Numeric form for colour classes / sorting only (never displayed). */
export function toNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function frInt(n: number): string {
  return n.toLocaleString('fr-FR');
}

/** French labels of the provider "missing" reasons used by atlas flags. */
export const MISSING_REASON_FR: Record<string, string> = {
  not_reported: 'non rapporté par la source',
  confidential: 'confidentiel',
  not_applicable: 'sans objet',
  not_available: 'non disponible chez la source',
  unreliable: 'jugé non fiable par la source',
  provider_missing_code: 'code « manquant » du fournisseur',
  no_declared_source: 'aucune source déclarée ne rapporte cette cellule',
};

export function missingReasonFr(reason: string | null | undefined): string {
  if (!reason) return 'raison non précisée';
  return MISSING_REASON_FR[reason] ?? reason.replace(/_/g, ' ');
}

/** Human-readable provider flags ({"estimated": true, "note": "…"}) — shown, never hidden. */
export function flagLines(flags: Record<string, unknown> | null | undefined): string[] {
  if (!flags) return [];
  return Object.entries(flags)
    .filter(([k]) => k !== 'missing')
    .map(([k, v]) => (v === true ? k.replace(/_/g, ' ') : `${k.replace(/_/g, ' ')} : ${typeof v === 'string' ? v : JSON.stringify(v)}`));
}

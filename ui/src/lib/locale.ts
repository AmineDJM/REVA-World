/**
 * One sanitised display locale for the whole UI.
 *
 * Browsers can report locale tags that `Intl` rejects (e.g. Chromium reports "en-US@posix"
 * when the process runs under a POSIX locale; "en_US.UTF-8" also occurs). `new
 * Intl.NumberFormat(badTag)` throws a RangeError, which used to break every chart (uPlot builds
 * a NumberFormat from `navigator.language` when its module loads). Every number formatter in
 * the UI goes through `appLocale()` / `numberFormat()`, so an invalid tag can never throw.
 *
 * Dates are deliberately not localised: they are shown as ISO-8601 UTC (see format.ts) so that
 * knowledge cutoffs and valid times are unambiguous across readers.
 */

export const FALLBACK_LOCALE = 'en-US';

/** True when `Intl.NumberFormat` accepts the tag and the runtime supports it. */
function usable(tag: string): boolean {
  try {
    new Intl.NumberFormat(tag);
    return Intl.NumberFormat.supportedLocalesOf([tag]).length > 0;
  } catch {
    return false;
  }
}

/**
 * Turn whatever the browser reports into a valid BCP 47 tag the runtime supports:
 * strip POSIX modifiers/charsets ("@posix", ".UTF-8"), use "-" instead of "_", drop extension
 * and private-use sequences ("-u-nu-arab", "-x-…"), canonicalise with `Intl.getCanonicalLocales`
 * (in try/catch), and fall back to "en-US".
 */
export function sanitizeLocale(tag: unknown): string {
  if (typeof tag !== 'string') return FALLBACK_LOCALE;
  let t = tag.trim();
  if (!t) return FALLBACK_LOCALE;
  t = t.split('@')[0]!.split('.')[0]!.replace(/_/g, '-');
  // Cut at the first singleton subtag (extension "-u-", "-t-", … or private use "-x-").
  const subtags = t.split('-').filter((s) => s.length > 0);
  const cut = subtags.findIndex((s, i) => i > 0 && s.length === 1);
  t = (cut === -1 ? subtags : subtags.slice(0, cut)).join('-');
  if (!t) return FALLBACK_LOCALE;
  try {
    const [canonical] = Intl.getCanonicalLocales(t);
    if (canonical && usable(canonical)) return canonical;
  } catch {
    // invalid tag: fall through
  }
  return FALLBACK_LOCALE;
}

function browserTag(): unknown {
  if (typeof navigator === 'undefined') return undefined;
  try {
    return navigator.languages?.[0] ?? navigator.language;
  } catch {
    return undefined;
  }
}

let cached: string | null = null;

/** The sanitised display locale (computed once). */
export function appLocale(): string {
  if (cached === null) cached = sanitizeLocale(browserTag());
  return cached;
}

/** Test hook: recompute the locale from `tag` (or from the browser when omitted). */
export function resetAppLocale(tag?: unknown): string {
  formatters.clear();
  cached = sanitizeLocale(tag === undefined ? browserTag() : tag);
  return cached;
}

const formatters = new Map<string, Intl.NumberFormat>();

/** A cached `Intl.NumberFormat` for the app locale; never throws on a bad browser locale. */
export function numberFormat(options: Intl.NumberFormatOptions = {}): Intl.NumberFormat {
  const key = JSON.stringify(options);
  let f = formatters.get(key);
  if (!f) {
    try {
      f = new Intl.NumberFormat(appLocale(), options);
    } catch {
      f = new Intl.NumberFormat(FALLBACK_LOCALE, options);
    }
    formatters.set(key, f);
  }
  return f;
}

/** Format a JS number with the app locale. */
export function formatNumber(n: number, options: Intl.NumberFormatOptions = {}): string {
  return numberFormat(options).format(n);
}

/** Group and decimal separators of the app locale (used to format exact decimal strings). */
export function separators(): { group: string; decimal: string } {
  const parts = numberFormat({ useGrouping: true }).formatToParts(1234567.5);
  return {
    group: parts.find((p) => p.type === 'group')?.value ?? ',',
    decimal: parts.find((p) => p.type === 'decimal')?.value ?? '.',
  };
}

/**
 * Third-party code (uPlot) builds `Intl.NumberFormat(navigator.language)` at import time. If the
 * browser's tag is rejected by `Intl`, shadow `navigator.language`/`languages` with the sanitised
 * locale so such libraries load. A valid browser locale is left untouched. Idempotent.
 */
export function installLocaleGuard(): string {
  const locale = appLocale();
  if (typeof navigator === 'undefined') return locale;
  let bad = false;
  try {
    new Intl.NumberFormat(navigator.language);
    for (const l of navigator.languages ?? []) new Intl.NumberFormat(l);
  } catch {
    bad = true;
  }
  if (bad) {
    try {
      Object.defineProperty(navigator, 'language', { configurable: true, get: () => locale });
      Object.defineProperty(navigator, 'languages', { configurable: true, get: () => Object.freeze([locale]) });
    } catch {
      // Non-configurable navigator: the chart's own error path (table alternative) still applies.
    }
  }
  return locale;
}

import { afterEach, describe, expect, it, vi } from 'vitest';
import { FALLBACK_LOCALE, appLocale, formatNumber, installLocaleGuard, resetAppLocale, sanitizeLocale } from './locale';
import { formatDecimalString } from './format';
import { compact } from '../components/TimeSeriesChart';

function setBrowserLanguage(tag: string) {
  Object.defineProperty(navigator, 'language', { configurable: true, get: () => tag });
  Object.defineProperty(navigator, 'languages', { configurable: true, get: () => [tag] });
}

afterEach(() => {
  // Drop the own-property shadows so jsdom's prototype getters (en-US) apply again.
  delete (navigator as unknown as Record<string, unknown>).language;
  delete (navigator as unknown as Record<string, unknown>).languages;
  resetAppLocale();
});

describe('sanitizeLocale', () => {
  it('turns invalid browser tags into valid, supported locales', () => {
    expect(() => new Intl.NumberFormat('en-US@posix')).toThrow(RangeError); // the original failure
    expect(sanitizeLocale('en-US@posix')).toBe('en-US');
    expect(sanitizeLocale('en_US.UTF-8')).toBe('en-US');
    expect(sanitizeLocale('fr_FR.UTF-8@euro')).toBe('fr-FR');
    expect(sanitizeLocale('de-DE-u-nu-arab')).toBe('de-DE');
    expect(sanitizeLocale('en-x-private')).toBe('en');
    expect(sanitizeLocale('EN-us')).toBe('en-US');
  });

  it('falls back to en-US for unusable tags', () => {
    for (const bad of ['', '   ', 'C', 'POSIX', '!!', '@posix', '_', '12-34', null, undefined, 42]) {
      expect(sanitizeLocale(bad)).toBe(FALLBACK_LOCALE);
    }
  });
});

describe('app locale with an invalid browser tag', () => {
  it('formats numbers without throwing and with the sanitised locale', () => {
    setBrowserLanguage('en-US@posix');
    expect(resetAppLocale()).toBe('en-US');
    expect(appLocale()).toBe('en-US');
    expect(formatNumber(1234567.891, { maximumFractionDigits: 2 })).toBe('1,234,567.89');
    expect(formatDecimalString('57082396439.600717', 2)).toBe('57,082,396,439.6');
    expect(compact(57_082_396_439.6)).toBe('57.1B');
  });

  it('shadows navigator.language so uPlot can load (it builds Intl.NumberFormat(navigator.language) on import)', async () => {
    setBrowserLanguage('en-US@posix');
    // jsdom has no matchMedia (uPlot reads devicePixelRatio media queries on load).
    vi.stubGlobal('matchMedia', () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
    // Without the guard uPlot's import-time `new Intl.NumberFormat(navigator.language)` throws:
    expect(() => new Intl.NumberFormat(navigator.language)).toThrow(RangeError);
    resetAppLocale();
    expect(installLocaleGuard()).toBe('en-US');
    expect(navigator.language).toBe('en-US');
    expect(() => new Intl.NumberFormat(navigator.language)).not.toThrow();
    const mod = await import('uplot');
    expect(typeof mod.default).toBe('function');
    vi.unstubAllGlobals();
  });

  it('leaves a valid browser locale untouched and uses its separators', () => {
    setBrowserLanguage('de-DE');
    resetAppLocale();
    installLocaleGuard();
    expect(navigator.language).toBe('de-DE');
    expect(formatDecimalString('1234567.5')).toBe('1.234.567,5');
    expect(formatNumber(0.5)).toBe('0,5');
  });
});

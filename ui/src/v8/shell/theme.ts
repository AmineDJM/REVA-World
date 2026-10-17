import { useCallback, useEffect, useState } from 'react';

export type ThemePref = 'system' | 'light' | 'dark';
export type Theme = 'light' | 'dark';
export const THEME_KEY = 'v8.theme';
export const THEME_EVENT = 'v8:theme';

export function readThemePref(): ThemePref {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch {
    return 'system';
  }
}

export function systemTheme(): Theme {
  try {
    return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

export function effectiveTheme(pref: ThemePref): Theme {
  return pref === 'system' ? systemTheme() : pref;
}

/** Apply a preference: <html data-theme> (absent = system), persisted best-effort, `v8:theme` event. */
export function applyThemePref(pref: ThemePref, root: HTMLElement = document.documentElement): void {
  if (pref === 'system') delete root.dataset.theme;
  else root.dataset.theme = pref;
  try {
    if (pref === 'system') localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, pref);
  } catch {
    /* storage unavailable: the choice lasts for this page view */
  }
  window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: { pref, theme: effectiveTheme(pref) } }));
}

/** The toggle flips the *effective* theme (light ↔ dark); "système" is offered in the palette. */
export function toggledPref(pref: ThemePref): ThemePref {
  return effectiveTheme(pref) === 'dark' ? 'light' : 'dark';
}

export const THEME_LABEL: Record<ThemePref, string> = { system: 'système', light: 'clair', dark: 'sombre' };

export function useTheme(): { pref: ThemePref; theme: Theme; setPref: (p: ThemePref) => void } {
  const [pref, setPrefState] = useState<ThemePref>(readThemePref);
  const [theme, setTheme] = useState<Theme>(() => effectiveTheme(readThemePref()));
  useEffect(() => {
    applyThemePref(pref);
    setTheme(effectiveTheme(pref));
    if (pref !== 'system' || !window.matchMedia) return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const on = () => setTheme(mq.matches ? 'dark' : 'light');
    mq.addEventListener?.('change', on);
    return () => mq.removeEventListener?.('change', on);
  }, [pref]);
  const setPref = useCallback((p: ThemePref) => setPrefState(p), []);
  return { pref, theme, setPref };
}
